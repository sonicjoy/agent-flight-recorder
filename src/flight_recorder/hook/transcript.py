"""Token usage from a Claude Code session transcript, read a little at a time.

Hooks don't carry token counts; the transcript does. Its format is internal to Claude Code
and "changes between versions" (Claude Code docs), so this reads defensively: lines it
doesn't recognise are skipped, and nothing here may fail the hook.

What it relies on, checked against Claude Code's transcripts on 11 Nov 2026:
- JSON Lines; model responses have "type": "assistant" and a "message" with "id", "model"
  and "usage" (input_tokens, output_tokens, cache_read_input_tokens, and cache_creation
  with ephemeral_5m_input_tokens / ephemeral_1h_input_tokens, or cache_creation_input_tokens).
- One response can span several lines (one per content block: "text", "thinking" or
  "tool_use") that repeat its usage, so responses are keyed by message id, their blocks are
  joined, and the last line's usage and "stop_reason" win.
- Before each model request Claude Code writes an "api-request" line with an "id" and a
  "timestamp"; the response's lines point back to it with "requestRef". That's when the
  request was sent, so the span starts there. Recent request times are kept with the read
  position, since a request line can be read a turn before its response (a queued prompt).
  Without one, the span starts at the response's first line.
- Subagent turns in the main transcript are marked "isSidechain"; they're read from the
  subagent's own transcript instead.
- Messages Claude Code writes itself, such as a login error, have model "<synthetic>" and
  are skipped: no model was called. (Seen in a real session on 11 Nov 2026.)
- A tool's result line ("type": "user", with "tool_result" blocks naming a "tool_use_id")
  carries "permissionDecision": "decision" ("accept" or "reject"), "source" (such as
  "config", "user_temporary", "user_permanent") and "reasonType" / "reasonCode" (such as
  "classifier", "rule", "mode", "safetyCheck"). Hooks don't say whether a call was
  rejected, so decisions are read from here.

Where reading stopped is kept per transcript in <AFR_HOME>/hook-state/, so each turn reads
only new lines. The position is saved only once the events carrying the usage have been
sent or spooled (TranscriptRead.commit): a hook killed before that leaves the lines for the
next turn end or SessionEnd. Reading a line twice is harmless: the recorder keys responses
by id.

One read takes at most max_items responses and decisions, and stops after the line that
reaches it; the position is saved there, and the next turn end carries on. Without a saved
position (a session that predates the hooks) the first read starts at the beginning, so
such a session's history arrives a page per turn end instead of all at once.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

MAX_READ_BYTES = 50 * 1024 * 1024  # a turn that wrote more than this is read in later turns
KEPT_REQUESTS = 50  # request times kept between reads, newest
SYNTHETIC = "<synthetic>"  # the "model" of messages Claude Code writes itself, such as errors


def _state_file(state_dir: Path, transcript: Path) -> Path:
    digest = hashlib.sha256(str(transcript.resolve()).encode()).hexdigest()[:16]
    return state_dir / f"{digest}.json"


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _blocks(content: Any) -> dict[str, Any]:
    """What a response said: its text, any thinking, and the tools it called."""
    said: dict[str, Any] = {"text": "", "thinking": "", "tool_calls": []}
    blocks = cast("list[Any]", content) if isinstance(content, list) else []
    for raw in blocks:
        if not isinstance(raw, dict):
            continue
        block = cast("dict[str, Any]", raw)
        kind, text, thought = block.get("type"), block.get("text"), block.get("thinking")
        if kind == "text" and isinstance(text, str):
            said["text"] += text
        elif kind == "thinking" and isinstance(thought, str):
            said["thinking"] += thought
        elif kind == "tool_use":
            said["tool_calls"].append({k: block.get(k) for k in ("id", "name", "input")})
    return said


def _joined(earlier: dict[str, Any], later: dict[str, Any]) -> dict[str, Any]:
    return {
        "text": earlier["text"] + later["text"],
        "thinking": earlier["thinking"] + later["thinking"],
        "tool_calls": earlier["tool_calls"] + later["tool_calls"],
    }


def _said(output: dict[str, Any]) -> dict[str, Any]:
    """Only the parts a response has: no empty text or empty tool list."""
    return {k: v for k, v in output.items() if v}


def response_usage(line: dict[str, Any]) -> dict[str, Any] | None:
    """The usage of one model response, or None if this line isn't one."""
    message = line.get("message")
    if line.get("type") != "assistant" or not isinstance(message, dict):
        return None
    usage = message.get("usage")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    message_id = message.get("id")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(usage, dict) or not isinstance(message_id, str):
        return None
    creation = usage.get("cache_creation")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if isinstance(creation, dict):
        write_5m = _int(creation.get("ephemeral_5m_input_tokens"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        write_1h = _int(creation.get("ephemeral_1h_input_tokens"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    else:
        write_5m, write_1h = _int(usage.get("cache_creation_input_tokens")), 0  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    read = _int(usage.get("cache_read_input_tokens"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    model = message.get("model")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if model == SYNTHETIC:  # Claude Code's own message (an error, say): no model call happened
        return None
    stop_reason = message.get("stop_reason")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return {
        "message_id": message_id,
        "model": model if isinstance(model, str) else None,
        "started_at": line.get("timestamp"),  # the first line's; the response may span several
        "timestamp": line.get("timestamp"),  # the last line's: when the response finished
        "request_ref": line.get("requestRef"),
        "stop_reason": stop_reason if isinstance(stop_reason, str) else None,
        "output": _blocks(message.get("content")),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        # input_tokens here means all input, as in OTel: Anthropic reports uncached input alone.
        "input_tokens": _int(usage.get("input_tokens")) + read + write_5m + write_1h,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        "output_tokens": _int(usage.get("output_tokens")),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        "cache_read_tokens": read,
        "cache_write_5m_tokens": write_5m,
        "cache_write_1h_tokens": write_1h,
        "fast": usage.get("speed") == "fast",  # pyright: ignore[reportUnknownMemberType]
    }


def permission_decisions(line: dict[str, Any]) -> list[dict[str, Any]]:
    """Who allowed or refused the tool calls whose results this line holds."""
    decision = line.get("permissionDecision")
    message = line.get("message")
    if line.get("type") != "user" or not isinstance(decision, dict):
        return []
    content = cast("dict[str, Any]", message).get("content") if isinstance(message, dict) else None
    blocks = cast("list[Any]", content) if isinstance(content, list) else []
    made = cast("dict[str, Any]", decision)
    return [
        {
            "tool_use_id": block["tool_use_id"],
            "decision": made.get("decision"),
            "source": made.get("source"),
            "reason_type": made.get("reasonType"),
            "reason_code": made.get("reasonCode"),
            "at": line.get("timestamp"),
        }
        for block in (cast("dict[str, Any]", b) for b in blocks if isinstance(b, dict))
        if block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str)
    ]


@dataclass
class TranscriptRead:
    """New model responses, and where to resume once they're safely sent or spooled."""

    usage: list[dict[str, Any]]
    state_file: Path | None = None
    offset: int = 0
    requests: dict[str, Any] = field(default_factory=dict[str, Any])
    permissions: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    more: bool = False
    """Stopped before the end of what was written: the rest waits for the next read."""

    def commit(self) -> None:
        if self.state_file is not None:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            state = {"offset": self.offset, "requests": self.requests}
            # Written whole, then renamed: a hook reading at the same moment never sees it empty.
            partial = self.state_file.with_suffix(f".{os.getpid()}.tmp")
            partial.write_text(json.dumps(state), encoding="utf-8")
            os.replace(partial, self.state_file)


def read_new(
    transcript: Path,
    state_dir: Path,
    *,
    include_sidechain: bool = False,
    max_items: int | None = None,
) -> TranscriptRead:
    """Usage of the model responses written since the last commit, one entry per response,
    and the permission decisions; at most about max_items of the two together."""
    if not transcript.is_file():
        return TranscriptRead([])
    state = _state_file(state_dir, transcript)
    try:
        saved = json.loads(state.read_text(encoding="utf-8"))
        offset = int(saved["offset"])
    except (OSError, ValueError, KeyError, TypeError):
        saved, offset = {}, 0
    size = transcript.stat().st_size
    if not 0 <= offset <= size:  # rewritten, replaced or damaged: start again
        saved, offset = {}, 0
    kept: Any = cast("dict[str, Any]", saved).get("requests") if isinstance(saved, dict) else None
    with transcript.open("rb") as file:
        file.seek(offset)
        chunk = file.read(MAX_READ_BYTES)
    complete = chunk[: chunk.rfind(b"\n") + 1]  # a half-written last line waits for next time
    responses: dict[str, dict[str, Any]] = {}
    permissions: list[dict[str, Any]] = []
    # api-request id -> when it was sent, including requests read in earlier turns
    requests: dict[str, Any] = dict(cast("dict[str, Any]", kept)) if isinstance(kept, dict) else {}
    consumed, paged = 0, False
    for raw in complete.splitlines(keepends=True):
        if max_items is not None and len(responses) + len(permissions) >= max_items:
            paged = True  # a page: the rest is read next time
            break
        consumed += len(raw)
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(line, dict):
            continue
        line = cast("dict[str, Any]", line)
        if line.get("type") == "api-request" and isinstance(line.get("id"), str):
            requests[line["id"]] = line.get("timestamp")
            continue
        if line.get("isSidechain") and not include_sidechain:
            continue
        permissions += permission_decisions(line)
        if (usage := response_usage(line)) is not None:
            earlier = responses.get(usage["message_id"])
            if earlier is not None:  # a later line of the same response: keep when it began
                usage["started_at"] = earlier["started_at"]
                usage["output"] = _joined(earlier["output"], usage["output"])
            responses[usage["message_id"]] = usage
    for usage in responses.values():
        usage["requested_at"] = requests.get(str(usage.pop("request_ref")))
        usage["output"] = _said(usage["output"])
    recent = dict(list(requests.items())[-KEPT_REQUESTS:])
    more = paged or offset + len(chunk) < size  # not a half-written last line
    return TranscriptRead(
        list(responses.values()), state, offset + consumed, recent, permissions, more=more
    )


def new_usage(
    transcript: Path, state_dir: Path, *, include_sidechain: bool = False
) -> list[dict[str, Any]]:
    """read_new, committed straight away."""
    read = read_new(transcript, state_dir, include_sidechain=include_sidechain)
    read.commit()
    return read.usage
