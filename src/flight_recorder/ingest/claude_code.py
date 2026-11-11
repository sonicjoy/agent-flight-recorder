"""Claude Code hook events -> one run per session, with a span tree.

    session (agent)                      SessionStart ... SessionEnd: first prompt, last reply
      turn (step)                        UserPromptSubmit ... Stop: prompt in, reply out
        command: /review (other)         UserPromptExpansion: a slash command expanded
        claude-opus-5-5 (llm)            each model response, from the transcript at Stop:
                                         request sent ... last block; what it said and called
        Read / Bash / ... (tool)         PreToolUse ... PostToolUse / PostToolUseFailure
          permission: Bash (step)        PermissionRequest ... the answer (waiting for you)
        Explore (agent)                  a subagent: SubagentStart ... SubagentStop
          Grep (tool), ... (llm)
        notification: idle_prompt        Notification
      compaction (auto) (step)           PreCompact ... PostCompact, with the summary

A span whose parent hasn't arrived (its event was lost or is late) gets a placeholder
parent, so the tree never has loose ends; the parent's own event fills it in later.

Async hooks can arrive in any order (a tool's end before its start), and a span's fields
come from several events, so each event merges into what is stored. Events of one session
are applied one at a time under a Postgres advisory lock. Writes go through record_run(),
which redacts again (the hook already did once) and prices the model responses. The counts
of what the hook redacted are stored on the event's span, since the server's own pass finds
nothing left to count.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from flight_recorder.redact import PATTERNS, Redactor
from flight_recorder.store.models import Run, Span
from flight_recorder.store.write import Kind, RunIn, SpanIn, SpanStatus, record_run

SOURCE = "claude_code"
AGENT = "claude-code"
ROOT = "session"
NAME_CHARS = 80


class EventError(ValueError):
    """An event that can't be applied; the message holds no event data."""


def _time(value: Any) -> datetime | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _str(hook: Mapping[str, Any], key: str) -> str | None:
    value = hook.get(key)
    return value if isinstance(value, str) and value else None


def _turn(hook: Mapping[str, Any]) -> str | None:
    prompt = _str(hook, "prompt_id")
    return f"turn:{prompt}" if prompt else None


def _parent(hook: Mapping[str, Any]) -> str:
    """Inside a subagent, its span; else the current turn; else the session."""
    agent = _str(hook, "agent_id")
    return f"agent:{agent}" if agent else _turn(hook) or ROOT


def _tool_id(hook: Mapping[str, Any]) -> str:
    """Pre/PostToolUse pair by tool_use_id; without one, by tool name and input."""
    if tool_use := _str(hook, "tool_use_id"):
        return f"tool:{tool_use}"
    key = json.dumps([hook.get("tool_name"), hook.get("tool_input")], sort_keys=True, default=str)
    return f"tool:{hashlib.sha256(key.encode()).hexdigest()[:16]}"


def _stored(session: Session, session_id: str, external_id: str) -> SpanIn | None:
    """A span as stored (already redacted), back in the shape record_run takes."""
    span = session.scalars(
        select(Span)
        .join(Run, Run.id == Span.run_id)
        .where(Run.source == SOURCE, Run.external_id == session_id, Span.external_id == external_id)
    ).one_or_none()
    if span is None:
        return None
    attributes = dict(span.attributes)
    usage = cast("dict[str, Any]", attributes.pop("usage", {}))
    return SpanIn(
        external_id=span.external_id,
        parent_external_id=span.parent_external_id,
        kind=cast("Kind", span.kind),  # the database checks it is one of these
        name=span.name,
        model=span.model,
        status=cast("SpanStatus", span.status),
        error=span.error,
        started_at=span.started_at,
        ended_at=span.ended_at,
        input_tokens=span.input_tokens,
        output_tokens=span.output_tokens,
        cache_read_tokens=usage.get("cache_read_tokens"),
        cache_write_5m_tokens=usage.get("cache_write_5m_tokens"),
        cache_write_1h_tokens=usage.get("cache_write_1h_tokens"),
        fast=bool(usage.get("fast")),
        input=span.input,
        output=span.output,
        attributes=attributes,
        redactions=dict(span.redactions),
    )


def max_counts(*counts: dict[str, int]) -> dict[str, int]:
    """Per pattern, the largest count: the same secret seen by two events isn't two secrets."""
    merged: dict[str, int] = {}
    for item in counts:
        for pattern, n in item.items():
            merged[pattern] = max(merged.get(pattern, 0), n)
    return merged


def _merge(session: Session, session_id: str, at: datetime, **fields: Any) -> SpanIn:
    """Merge what this event knows into the stored span (or start one)."""
    stored = _stored(session, session_id, fields["external_id"])
    known = {k: v for k, v in fields.items() if v is not None}
    if "attributes" in known:
        known["attributes"] = {k: v for k, v in known["attributes"].items() if v is not None}
    if stored is None:
        values: dict[str, Any] = {"started_at": at, **known}
        return SpanIn(**values)
    if "started_at" in known:
        known["started_at"] = min(known["started_at"], stored.started_at)
    if "ended_at" in known and stored.ended_at:
        known["ended_at"] = max(known["ended_at"], stored.ended_at)
    if "attributes" in known:
        known["attributes"] = stored.attributes | known["attributes"]
    if "redactions" in known:
        known["redactions"] = max_counts(stored.redactions, known["redactions"])
    return stored.model_copy(update=known)


def _better_parent(stored: str | None, new: str) -> str:
    """A response read twice (by Stop and by SessionEnd, in either order) belongs to its turn
    or subagent rather than the session."""
    if stored and stored != ROOT:
        return stored
    return new


def _union(old: Any, new: Any) -> dict[str, Any] | None:
    """What a response said, from two reads of it: a split read adds the rest, a repeated
    read adds nothing. Tool calls are kept once each, by id."""
    if not isinstance(old, dict):
        return cast("dict[str, Any]", new) or None if isinstance(new, dict) else None
    if not isinstance(new, dict):
        return cast("dict[str, Any]", old) or None
    before, after = cast("dict[str, Any]", old), cast("dict[str, Any]", new)
    said: dict[str, Any] = {}
    for key in ("thinking", "text"):
        a, b = str(before.get(key) or ""), str(after.get(key) or "")
        said[key] = a if b in a else b if a in b else a + b
    calls: list[dict[str, Any]] = []
    seen: set[str] = set()
    both: list[Any] = [*(before.get("tool_calls") or []), *(after.get("tool_calls") or [])]
    for raw in both:
        if (
            isinstance(raw, dict)
            and (call_id := str(cast("dict[str, Any]", raw).get("id"))) not in seen
        ):
            seen.add(call_id)
            calls.append(cast("dict[str, Any]", raw))
    said["tool_calls"] = calls
    return {k: v for k, v in said.items() if v} or None


def _responses(
    session: Session, session_id: str, usage: Any, parent: str, at: datetime
) -> list[SpanIn]:
    """One llm span per model response read from the transcript: from when the request was
    sent to its last block, with what it said, any thinking, and the tools it called.

    A response read twice keeps the more specific parent (see _better_parent), and a
    response read in two parts keeps both: the earliest start, the latest end, and what
    each part said.
    """
    spans: list[SpanIn] = []
    for item in _items(usage):
        if not isinstance(item.get("message_id"), str):
            continue
        external_id = f"llm:{item['message_id']}"
        stored = _stored(session, session_id, external_id)
        model = item.get("model")
        ended = _time(item.get("timestamp")) or at
        first = _time(item.get("started_at")) or ended
        started = _time(item.get("requested_at")) or first
        output = item.get("output")
        if stored is not None:
            # stored.started_at is its request time, or its first output if none was known
            earlier = _time(stored.attributes.get("first_output_at")) or stored.started_at
            started, first = min(started, stored.started_at), min(first, earlier)
            ended = max(ended, stored.ended_at or ended)
            output = _union(stored.output, output)
        attributes = {
            "stop_reason": item.get("stop_reason"),
            "first_output_at": first.isoformat() if started != first else None,
        }
        spans.append(
            SpanIn(
                external_id=external_id,
                parent_external_id=_better_parent(
                    stored.parent_external_id if stored else None, parent
                ),
                kind="llm",
                name=str(model or "model response"),
                model=f"anthropic:{model}" if isinstance(model, str) else None,
                status="ok",
                started_at=min(started, ended),
                ended_at=ended,
                output=output or None,
                attributes={k: v for k, v in attributes.items() if v is not None},
                input_tokens=item.get("input_tokens"),
                output_tokens=item.get("output_tokens"),
                cache_read_tokens=item.get("cache_read_tokens"),
                cache_write_5m_tokens=item.get("cache_write_5m_tokens"),
                cache_write_1h_tokens=item.get("cache_write_1h_tokens"),
                fast=bool(item.get("fast")),
            )
        )
    return spans


def _session_attributes(hook: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("cwd", "permission_mode", "source", "model", "reason")
    return {f"claude_code.{k}": hook[k] for k in keys if hook.get(k) not in (None, "")}


def _instant(at: datetime, prefix: str) -> str:
    """An id for a one-off event: the same event re-sent gets the same id."""
    return f"{prefix}:{at.timestamp():.6f}"


def _compaction(session: Session, session_id: str, at: datetime) -> str | None:
    """The compaction a PostCompact closes: the latest one started by then and still open
    (or closed by this same event, re-sent)."""
    return session.scalar(
        select(Span.external_id)
        .join(Run, Run.id == Span.run_id)
        .where(
            Run.source == SOURCE,
            Run.external_id == session_id,
            Span.external_id.like("compact:%"),
            Span.started_at <= at,
            (Span.ended_at.is_(None)) | (Span.ended_at == at),
        )
        .order_by(Span.started_at.desc())
        .limit(1)
    )


def _compaction_started(session: Session, session_id: str, at: datetime) -> str | None:
    """The compaction a PreCompact opens: its own (re-sent), or one a PostCompact that
    arrived first already made (it starts and ends at the PostCompact, after this)."""
    spans = (
        select(Span.external_id)
        .join(Run, Run.id == Span.run_id)
        .where(
            Run.source == SOURCE,
            Run.external_id == session_id,
            Span.external_id.like("compact:%"),
        )
    )
    own = session.scalar(spans.where(Span.started_at == at).limit(1))
    if own:
        return own
    return session.scalar(
        spans.where(Span.started_at > at, Span.ended_at == Span.started_at)
        .order_by(Span.started_at)
        .limit(1)
    )


def _same_call(hook: Mapping[str, Any], prefix: str = "") -> Any:
    """Spans for a call of this tool with this input (named with prefix). The hook redacts
    with the same key as the server, so the stored input matches."""
    return (Span.name == f"{prefix}{_str(hook, 'tool_name') or 'tool'}") & (
        Span.input == cast("Any", hook.get("tool_input"))
    )


def _waiting_tool(
    session: Session, session_id: str, hook: Mapping[str, Any], at: datetime
) -> Span | None:
    """The tool call a PermissionRequest is about. It carries no tool_use_id, so it's the
    latest call of that tool with that input that was still running at the request: open,
    or ended after it (its PostToolUse arrived first)."""
    return session.scalars(
        select(Span)
        .join(Run, Run.id == Span.run_id)
        .where(
            Run.source == SOURCE,
            Run.external_id == session_id,
            Span.kind == "tool",
            _same_call(hook),
            Span.ended_at.is_(None) | (Span.ended_at >= at),
        )
        .order_by(Span.started_at.desc())
        .limit(1)
    ).first()


def _open_wait(
    session: Session, session_id: str, hook: Mapping[str, Any], tool_id: str
) -> SpanIn | None:
    """The permission wait a tool's end closes: one already under the tool, or one whose
    request arrived before the tool's start, waiting under the turn with the same call."""
    found = session.scalar(
        select(Span.external_id)
        .join(Run, Run.id == Span.run_id)
        .where(
            Run.source == SOURCE,
            Run.external_id == session_id,
            Span.external_id.like("permission:%"),
            Span.ended_at.is_(None),
            (Span.parent_external_id == tool_id)
            | ((Span.parent_external_id == _parent(hook)) & _same_call(hook, "permission: ")),
        )
        .order_by(Span.started_at)
        .limit(1)
    )
    return _stored(session, session_id, found) if found else None


def _wait_ended(wait_started: datetime, tool_ended: datetime, ran_ms: Any) -> datetime:
    """The wait ended when the tool began: its end minus its run time, but never before the
    wait began (each hook stamps its own time, a little late)."""
    ran = ran_ms if isinstance(ran_ms, int | float) and not isinstance(ran_ms, bool) else 0
    return max(tool_ended - timedelta(milliseconds=ran), wait_started)


def _placeholders(
    session: Session, session_id: str, spans: list[SpanIn], turn: str | None
) -> list[SpanIn]:
    """Stand-ins for parents that haven't arrived, so no span is left dangling. A turn or
    subagent's own event fills its placeholder in later (the earliest start wins). A parent
    already stored starts no later than its children."""
    known = {span.external_id for span in spans} | {ROOT}
    added: list[SpanIn] = []
    i = 0
    while i < len(spans):
        span, i = spans[i], i + 1
        parent = span.parent_external_id
        if parent is None or parent in known:
            continue
        known.add(parent)
        if stored := _stored(session, session_id, parent):
            if stored.started_at > span.started_at:
                extended = stored.model_copy(update={"started_at": span.started_at})
                added.append(extended)
                spans.append(extended)  # and its own parent, in turn
            continue
        if parent.startswith("turn:"):
            kind, name, grandparent = "step", "turn", ROOT
        elif parent.startswith("agent:"):
            name = f"subagent {parent.removeprefix('agent:')[:8]}"
            kind, grandparent = "agent", turn or ROOT
        else:
            continue
        placeholder = SpanIn(
            external_id=parent,
            parent_external_id=grandparent,
            kind=kind,
            name=name,
            started_at=span.started_at,
        )
        added.append(placeholder)
        spans.append(placeholder)  # its own parent (a turn) may be missing too
    return added


MAX_ITEMS = 1_000  # usage or decisions per event: far more than one turn has


def _items(value: Any) -> list[dict[str, Any]]:
    """The dict entries of a list the hook sent beside its payload, capped."""
    entries = cast("list[Any]", value) if isinstance(value, list) else []
    return [cast("dict[str, Any]", e) for e in entries[:MAX_ITEMS] if isinstance(e, dict)]


WHO_BY_REASON = {
    "classifier": "auto mode",
    "rule": "a permission rule",
    "mode": "the permission mode",
    "safetyCheck": "a safety check",
}


def _who(source: Any, reason_type: Any) -> str:
    """Who made a permission decision, in words."""
    if isinstance(source, str) and source.startswith("user"):
        return "you"
    return WHO_BY_REASON.get(str(reason_type), "Claude Code")


def _wait_outcome(tool: SpanIn | Span | None) -> tuple[SpanStatus, str | None]:
    """How a wait for this tool ended: rejected with the tool, else ok."""
    if tool is not None and tool.status == "rejected":
        return "rejected", tool.error
    return "ok", None


def _decisions(session: Session, session_id: str, permissions: Any) -> list[SpanIn]:
    """Each tool call's permission decision, from the transcript: who allowed or refused it.

    A refused call is `rejected` (and ends, since it never ran), keeping any more detailed
    reason already stored (PermissionDenied's, or your feedback in PostToolUseFailure).
    Its wait is `rejected` too; an allowed call's wait ends when the tool starts, as usual.
    """
    spans: list[SpanIn] = []
    for item in _items(permissions):
        tool_use = item.get("tool_use_id")
        if not isinstance(tool_use, str) or item.get("decision") not in ("accept", "reject"):
            continue
        spans += _decision(session, session_id, f"tool:{tool_use}", item)
    return spans


def _decision(
    session: Session, session_id: str, tool_id: str, item: dict[str, Any]
) -> list[SpanIn]:
    rejected = item["decision"] == "reject"
    who = _who(item.get("source"), item.get("reason_type"))
    outcome = f"{'rejected' if rejected else 'allowed'} by {who}"
    at = _time(item.get("at")) or datetime.now(UTC)
    tool = _stored(session, session_id, tool_id)
    fields: dict[str, Any] = {
        "external_id": tool_id,
        "attributes": {
            "permission": {
                "decision": item["decision"],
                "by": who,
                "source": item.get("source"),
                "reason_type": item.get("reason_type"),
                "reason_code": item.get("reason_code"),
            }
        },
    }
    if tool is None:  # its hook events were lost: the transcript still tells
        fields |= {"parent_external_id": ROOT, "kind": "tool", "name": "tool", "started_at": at}
    if rejected:
        detail = tool.error if tool is not None else None
        if detail and not detail.startswith("rejected"):
            detail = f"{outcome}: {detail}"  # your feedback, from PostToolUseFailure
        fields |= {"status": "rejected", "error": detail or outcome}
        if tool is None or tool.ended_at is None:
            fields["ended_at"] = at  # it never ran
    spans = [_merge(session, session_id, at, **fields)]
    if rejected:
        error = spans[0].error
        for wait in _waits_of(session, session_id, tool_id):
            ended = wait.ended_at or max(at, wait.started_at)
            update = {"status": "rejected", "output": error, "ended_at": ended}
            spans.append(wait.model_copy(update=update))
    return spans


def _waits_of(session: Session, session_id: str, tool_id: str) -> list[SpanIn]:
    """The permission waits already under a tool call."""
    ids = session.scalars(
        select(Span.external_id)
        .join(Run, Run.id == Span.run_id)
        .where(
            Run.source == SOURCE,
            Run.external_id == session_id,
            Span.external_id.like("permission:%"),
            Span.parent_external_id == tool_id,
        )
    ).all()
    return [s for i in ids if (s := _stored(session, session_id, i))]


PATTERN_NAMES = frozenset(p.name for p in PATTERNS)
MAX_COUNT = 1_000_000


def _client_counts(event: Mapping[str, Any]) -> dict[str, int]:
    """What the hook redacted before sending, per pattern.

    Only known pattern names are kept, and counts are clamped: the keys are stored as they
    come (they aren't redacted), and anything local can post to the API until #25.
    """
    counts = event.get("client_redactions")
    if not isinstance(counts, dict):
        return {}
    return {
        k: min(v, MAX_COUNT)
        for k, v in cast("dict[Any, Any]", counts).items()
        if k in PATTERN_NAMES and isinstance(v, int) and not isinstance(v, bool) and v > 0
    }


def apply_event(session: Session, redactor: Redactor, event: Mapping[str, Any]) -> None:
    raw = event.get("hook")
    if not isinstance(raw, dict):
        raise EventError("event has no hook payload")
    hook = cast("dict[str, Any]", raw)
    session_id, name = _str(hook, "session_id"), _str(hook, "hook_event_name")
    if not session_id or not name:
        raise EventError("hook payload needs session_id and hook_event_name")
    at = _time(event.get("received_at")) or datetime.now(UTC)
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"{SOURCE}:{session_id}"}
    )
    client = _client_counts(event)

    def merge(**fields: Any) -> SpanIn:
        return _merge(session, session_id, at, **fields)

    run: dict[str, Any] = {"started_at": at, "attributes": _session_attributes(hook)}
    root: dict[str, Any] = {
        "external_id": ROOT,
        "kind": "agent",
        "name": "Claude Code session",
        "started_at": at,
    }
    spans: list[SpanIn] = []
    turn = _turn(hook)
    tool: dict[str, Any] = {
        "external_id": _tool_id(hook),
        "parent_external_id": _parent(hook),
        "kind": "tool",
        "name": _str(hook, "tool_name") or "tool",
        "input": hook.get("tool_input"),
        "redactions": client,
    }
    turn_fields: dict[str, Any] = {
        "external_id": turn,
        "parent_external_id": ROOT,
        "kind": "step",
        "name": "turn",
        "redactions": client,
    }

    if name == "UserPromptSubmit" and turn:
        prompt = _str(hook, "prompt") or ""
        spans.append(merge(**turn_fields, input=prompt, started_at=at))
        stored_root = _stored(session, session_id, ROOT)
        first = _time(stored_root.attributes.get("first_prompt_at")) if stored_root else None
        if prompt and (first is None or at < first):  # the session's first prompt
            root |= {"input": prompt, "attributes": {"first_prompt_at": at.isoformat()}}
        named = session.scalar(
            select(Run.name).where(Run.source == SOURCE, Run.external_id == session_id)
        )
        if not named and prompt.strip():
            run["name"] = prompt.strip().splitlines()[0][:NAME_CHARS]
    elif name == "PreToolUse":
        spans.append(merge(**tool, started_at=at))
    elif name in ("PostToolUse", "PostToolUseFailure"):
        failed = name == "PostToolUseFailure"
        done = _stored(session, session_id, tool["external_id"])
        rejected = done is not None and done.status == "rejected"
        if wait := _open_wait(session, session_id, hook, tool["external_id"]):
            ended = _wait_ended(wait.started_at, at, hook.get("duration_ms"))
            status, output = _wait_outcome(done)
            spans.append(
                merge(
                    external_id=wait.external_id,
                    parent_external_id=tool["external_id"],
                    kind="step",
                    ended_at=ended,
                    status=status,
                    output=output,
                )
            )
        spans.append(
            merge(
                **tool,
                output=hook.get("tool_response"),
                ended_at=at,
                status=None if rejected else ("error" if failed else "ok"),
                error=_str(hook, "error") if failed and not rejected else None,
                attributes={
                    "interrupted": True if hook.get("is_interrupt") else None,
                    "duration_ms": hook.get("duration_ms"),
                },
            )
        )
    elif name in ("SubagentStart", "SubagentStop") and (agent := _str(hook, "agent_id")):
        span_id = f"agent:{agent}"
        known_agent = _stored(session, session_id, span_id)
        agent_type = _str(hook, "agent_type")
        agent_fields: dict[str, Any] = {
            "external_id": span_id,
            "parent_external_id": turn or ROOT,
            "kind": "agent",
            # without agent_type, keep a name already known; else name it by its id
            "name": agent_type or (None if known_agent else f"subagent {agent[:8]}"),
            "redactions": client,
        }
        if name == "SubagentStart":
            spans.append(merge(**agent_fields, started_at=at))
        else:
            output = hook.get("last_assistant_message")
            spans.append(merge(**agent_fields, output=output, ended_at=at, status="ok"))
            spans += _responses(session, session_id, event.get("usage"), span_id, at)
            spans += _decisions(session, session_id, event.get("permissions"))
    elif name in ("Stop", "StopFailure"):
        failed = name == "StopFailure"
        if turn:
            error = (_str(hook, "error") or "the turn ended with an API error") if failed else None
            spans.append(
                merge(
                    **turn_fields,
                    output=hook.get("last_assistant_message"),
                    ended_at=at,
                    status="error" if failed else "ok",
                    error=error,
                )
            )
        spans += _responses(session, session_id, event.get("usage"), turn or ROOT, at)
        spans += _decisions(session, session_id, event.get("permissions"))
        stored_root = _stored(session, session_id, ROOT)
        latest = _time(stored_root.attributes.get("latest_reply_at")) if stored_root else None
        reply = hook.get("last_assistant_message")
        if isinstance(reply, str) and reply and (latest is None or at >= latest):
            root |= {"output": reply, "attributes": {"latest_reply_at": at.isoformat()}}
    elif name == "PermissionRequest":
        # Its own id, from its own time: a re-send finds the same span.
        wait_id = _instant(at, "permission")
        known_wait = _stored(session, session_id, wait_id)
        waiting = _waiting_tool(session, session_id, hook, at)
        if waiting is not None:
            parent: str | None = waiting.external_id
        else:  # its tool hasn't started yet: wait under the turn; the tool's end adopts it
            parent = None if known_wait else _parent(hook)
        ended, status, output = None, None, None
        if waiting is not None and waiting.ended_at is not None:  # the tool already finished
            ran = waiting.attributes.get("duration_ms")
            ended = _wait_ended(at, waiting.ended_at, ran)
            status, output = _wait_outcome(waiting)
        spans.append(
            merge(
                external_id=wait_id,
                parent_external_id=parent,
                kind="step",
                name=f"permission: {_str(hook, 'tool_name') or 'tool'}",
                input=hook.get("tool_input"),
                started_at=at,
                ended_at=ended,
                status=status,
                output=output,
                attributes={"suggestions": hook.get("permission_suggestions")},
                redactions=client,
            )
        )
    elif name == "PermissionDenied":
        reason = f"rejected by auto mode: {_str(hook, 'reason') or 'no reason given'}"
        spans.append(merge(**tool, ended_at=at, status="rejected", error=reason))
        if wait := _open_wait(session, session_id, hook, tool["external_id"]):
            spans.append(
                merge(
                    external_id=wait.external_id,
                    parent_external_id=tool["external_id"],
                    kind="step",
                    ended_at=max(at, wait.started_at),
                    status="rejected",
                    output=reason,
                )
            )
    elif name in ("PreCompact", "PostCompact"):
        compaction: dict[str, Any] = {
            "parent_external_id": ROOT,
            "kind": "step",
            "name": f"compaction ({_str(hook, 'trigger') or 'unknown'})",
            "redactions": client,
        }
        if name == "PreCompact":
            spans.append(
                merge(
                    **compaction,
                    external_id=_compaction_started(session, session_id, at)
                    or _instant(at, "compact"),
                    input=hook.get("custom_instructions"),
                    started_at=at,
                )
            )
        else:
            spans.append(
                merge(
                    **compaction,
                    external_id=_compaction(session, session_id, at) or _instant(at, "compact"),
                    output=hook.get("compact_summary"),
                    ended_at=at,
                    status="ok",
                )
            )
    elif name == "Notification":
        spans.append(
            merge(
                external_id=_instant(at, "notification"),
                parent_external_id=turn or ROOT,
                kind="other",
                name=f"notification: {_str(hook, 'notification_type') or 'notice'}",
                output=hook.get("message"),
                attributes={"title": _str(hook, "title")},
                started_at=at,
                ended_at=at,
                status="ok",
                redactions=client,
            )
        )
    elif name == "UserPromptExpansion":
        command = (_str(hook, "command_name") or "command").lstrip("/")
        spans.append(
            merge(
                external_id=_instant(at, "command"),
                parent_external_id=turn or ROOT,
                kind="other",
                name=f"command: /{command}",
                input=hook.get("command_args"),
                output=hook.get("prompt"),
                attributes={
                    "source": _str(hook, "command_source"),
                    "expansion_type": _str(hook, "expansion_type"),
                },
                started_at=at,
                ended_at=at,
                status="ok",
                redactions=client,
            )
        )
    elif name == "SessionEnd":
        root |= {"ended_at": at, "status": "ok", "redactions": client}
        spans += _responses(session, session_id, event.get("usage"), ROOT, at)
        spans += _decisions(session, session_id, event.get("permissions"))
        run |= {"ended_at": at, "status": "ok"}
    else:  # SessionStart, and events this version doesn't map
        root["redactions"] = client

    spans.insert(0, merge(**root))
    spans += _placeholders(session, session_id, spans, turn)
    run_in = RunIn(source=SOURCE, external_id=session_id, agent=AGENT, **run)
    record_run(session, run_in, spans, redactor=redactor)
