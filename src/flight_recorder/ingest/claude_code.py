"""Claude Code hook events -> one run per session, with a span tree.

    session (agent)                      SessionStart ... SessionEnd
      turn (step)                        UserPromptSubmit ... Stop: prompt in, reply out
        Read / Bash / ... (tool)         PreToolUse ... PostToolUse / PostToolUseFailure
        claude-opus-5-5 (llm)            each model response, from the transcript at Stop
        Explore (agent)                  a subagent, ended by SubagentStop
          Grep (tool), ... (llm)

Async hooks can arrive in any order (a tool's end before its start), and a span's fields
come from several events, so each event merges into what is stored. Events of one session
are applied one at a time under a Postgres advisory lock. Writes go through record_run(),
which redacts again (the hook already did once) and prices the model responses.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from flight_recorder.redact import Redactor
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
    )


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
    return stored.model_copy(update=known)


def _responses(usage: Any, parent: str, at: datetime) -> list[SpanIn]:
    """One llm span per model response read from the transcript."""
    spans: list[SpanIn] = []
    for raw in cast("list[Any]", usage) if isinstance(usage, list) else []:
        if not isinstance(raw, dict):
            continue
        item = cast("dict[str, Any]", raw)
        if not isinstance(item.get("message_id"), str):
            continue
        model = item.get("model")
        when = _time(item.get("timestamp")) or at
        spans.append(
            SpanIn(
                external_id=f"llm:{item['message_id']}",
                parent_external_id=parent,
                kind="llm",
                name=str(model or "model response"),
                model=f"anthropic:{model}" if isinstance(model, str) else None,
                status="ok",
                started_at=when,
                ended_at=when,
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

    run: dict[str, Any] = {"started_at": at, "attributes": _session_attributes(hook)}
    spans = [
        _merge(
            session,
            session_id,
            at,
            external_id=ROOT,
            kind="agent",
            name="Claude Code session",
            started_at=at,
        )
    ]
    status: Status = "error" if name == "PostToolUseFailure" else "ok"

    if name == "UserPromptSubmit" and (turn := _turn(hook)):
        prompt = _str(hook, "prompt") or ""
        spans.append(
            _merge(
                session,
                session_id,
                at,
                external_id=turn,
                parent_external_id=ROOT,
                kind="step",
                name="turn",
                input=prompt,
                started_at=at,
            )
        )
        if not session.scalar(
            select(Run.name).where(Run.source == SOURCE, Run.external_id == session_id)
        ):
            run["name"] = prompt.strip().splitlines()[0][:NAME_CHARS] if prompt.strip() else None
    elif name == "PreToolUse":
        spans.append(
            _merge(
                session,
                session_id,
                at,
                external_id=_tool_id(hook),
                parent_external_id=_parent(hook),
                kind="tool",
                name=_str(hook, "tool_name") or "tool",
                input=hook.get("tool_input"),
                started_at=at,
            )
        )
    elif name in ("PostToolUse", "PostToolUseFailure"):
        error = _str(hook, "error") if status == "error" else None
        spans.append(
            _merge(
                session,
                session_id,
                at,
                external_id=_tool_id(hook),
                parent_external_id=_parent(hook),
                kind="tool",
                name=_str(hook, "tool_name") or "tool",
                input=hook.get("tool_input"),
                output=hook.get("tool_response"),
                ended_at=at,
                status=status,
                error=error,
                attributes={"interrupted": True} if hook.get("is_interrupt") else None,
            )
        )
    elif name == "SubagentStop" and (agent := _str(hook, "agent_id")):
        span_id = f"agent:{agent}"
        spans.append(
            _merge(
                session,
                session_id,
                at,
                external_id=span_id,
                parent_external_id=_turn(hook) or ROOT,
                kind="agent",
                name=_str(hook, "agent_type") or "subagent",
                output=hook.get("last_assistant_message"),
                ended_at=at,
                status="ok",
            )
        )
        spans += _responses(event.get("usage"), span_id, at)
    elif name in ("Stop", "StopFailure"):
        turn = _turn(hook) or ROOT
        failed = name == "StopFailure"
        if turn != ROOT:
            spans.append(
                _merge(
                    session,
                    session_id,
                    at,
                    external_id=turn,
                    parent_external_id=ROOT,
                    kind="step",
                    name="turn",
                    output=hook.get("last_assistant_message"),
                    ended_at=at,
                    status="error" if failed else "ok",
                    error=(_str(hook, "error") or "the turn ended with an API error")
                    if failed
                    else None,
                )
            )
        spans += _responses(event.get("usage"), turn, at)
    elif name == "SessionEnd":
        spans[0] = _merge(
            session,
            session_id,
            at,
            external_id=ROOT,
            kind="agent",
            name="Claude Code session",
            ended_at=at,
            status="ok",
        )
        spans += _responses(event.get("usage"), ROOT, at)
        run |= {"ended_at": at, "status": "ok"}

    record_run(
        session,
        RunIn(source=SOURCE, external_id=session_id, agent=AGENT, **run),
        spans,
        redactor=redactor,
    )
