"""Claude Code hook events -> one run per session, with a span tree.

    session (agent)                      SessionStart ... SessionEnd
      turn (step)                        UserPromptSubmit ... Stop: prompt in, reply out
        Read / Bash / ... (tool)         PreToolUse ... PostToolUse / PostToolUseFailure
        claude-opus-5-5 (llm)            each model response, from the transcript at Stop
        Explore (agent)                  a subagent: SubagentStart ... SubagentStop
          Grep (tool), ... (llm)

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
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from flight_recorder.redact import PATTERNS, Redactor
from flight_recorder.store.models import Run, Span
from flight_recorder.store.write import Kind, RunIn, SpanIn, Status, record_run

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
        status=cast("Status", span.status),
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


def _responses(
    session: Session, session_id: str, usage: Any, parent: str, at: datetime
) -> list[SpanIn]:
    """One llm span per model response read from the transcript.

    A response read twice keeps the more specific parent (see _better_parent).
    """
    spans: list[SpanIn] = []
    for raw in cast("list[Any]", usage) if isinstance(usage, list) else []:
        if not isinstance(raw, dict):
            continue
        item = cast("dict[str, Any]", raw)
        if not isinstance(item.get("message_id"), str):
            continue
        external_id = f"llm:{item['message_id']}"
        stored = _stored(session, session_id, external_id)
        model = item.get("model")
        ended = _time(item.get("timestamp")) or at
        started = _time(item.get("started_at")) or ended
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
        named = session.scalar(
            select(Run.name).where(Run.source == SOURCE, Run.external_id == session_id)
        )
        if not named and prompt.strip():
            run["name"] = prompt.strip().splitlines()[0][:NAME_CHARS]
    elif name == "PreToolUse":
        spans.append(merge(**tool, started_at=at))
    elif name in ("PostToolUse", "PostToolUseFailure"):
        failed = name == "PostToolUseFailure"
        spans.append(
            merge(
                **tool,
                output=hook.get("tool_response"),
                ended_at=at,
                status="error" if failed else "ok",
                error=_str(hook, "error") if failed else None,
                attributes={"interrupted": True} if hook.get("is_interrupt") else None,
            )
        )
    elif name in ("SubagentStart", "SubagentStop") and (agent := _str(hook, "agent_id")):
        span_id = f"agent:{agent}"
        agent_fields: dict[str, Any] = {
            "external_id": span_id,
            "parent_external_id": turn or ROOT,
            "kind": "agent",
            "name": _str(hook, "agent_type") or "subagent",
            "redactions": client,
        }
        if name == "SubagentStart":
            spans.append(merge(**agent_fields, started_at=at))
        else:
            output = hook.get("last_assistant_message")
            spans.append(merge(**agent_fields, output=output, ended_at=at, status="ok"))
            spans += _responses(session, session_id, event.get("usage"), span_id, at)
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
    elif name == "SessionEnd":
        root |= {"ended_at": at, "status": "ok", "redactions": client}
        spans += _responses(session, session_id, event.get("usage"), ROOT, at)
        run |= {"ended_at": at, "status": "ok"}
    else:  # SessionStart, and events this version doesn't map
        root["redactions"] = client

    spans.insert(0, merge(**root))
    run_in = RunIn(source=SOURCE, external_id=session_id, agent=AGENT, **run)
    record_run(session, run_in, spans, redactor=redactor)
