"""Token usage from a Claude Code session transcript, read a little at a time.

Hooks don't carry token counts; the transcript does. Its format is internal to Claude Code
and "changes between versions" (Claude Code docs), so this reads defensively: lines it
doesn't recognise are skipped, and nothing here may fail the hook.

What it relies on, checked against Claude Code's transcripts on 11 Nov 2026:
- JSON Lines; model responses have "type": "assistant" and a "message" with "id", "model"
  and "usage" (input_tokens, output_tokens, cache_read_input_tokens, and cache_creation
  with ephemeral_5m_input_tokens / ephemeral_1h_input_tokens, or cache_creation_input_tokens).
- One response can span several lines (one per content block) that repeat its usage, so
  responses are keyed by message id and the last line wins.
- Subagent turns in the main transcript are marked "isSidechain"; they're read from the
  subagent's own transcript instead.
- Messages Claude Code writes itself, such as a login error, have model "<synthetic>" and
  are skipped: no model was called. (Seen in a real session on 11 Nov 2026.)

Where reading stopped is kept per transcript in <AFR_HOME>/hook-state/, so each turn reads
only new lines. Reading a line twice is harmless: the recorder keys responses by id.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

MAX_READ_BYTES = 50 * 1024 * 1024  # a turn that wrote more than this is read in later turns
SYNTHETIC = "<synthetic>"  # the "model" of messages Claude Code writes itself, such as errors


def _state_file(state_dir: Path, transcript: Path) -> Path:
    digest = hashlib.sha256(str(transcript.resolve()).encode()).hexdigest()[:16]
    return state_dir / f"{digest}.json"


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


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
    return {
        "message_id": message_id,
        "model": model if isinstance(model, str) else None,
        "timestamp": line.get("timestamp"),
        # input_tokens here means all input, as in OTel: Anthropic reports uncached input alone.
        "input_tokens": _int(usage.get("input_tokens")) + read + write_5m + write_1h,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        "output_tokens": _int(usage.get("output_tokens")),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        "cache_read_tokens": read,
        "cache_write_5m_tokens": write_5m,
        "cache_write_1h_tokens": write_1h,
        "fast": usage.get("speed") == "fast",  # pyright: ignore[reportUnknownMemberType]
    }


def new_usage(
    transcript: Path, state_dir: Path, *, include_sidechain: bool = False
) -> list[dict[str, Any]]:
    """Usage of the model responses written since the last call, one entry per response."""
    if not transcript.is_file():
        return []
    state = _state_file(state_dir, transcript)
    try:
        offset = int(json.loads(state.read_text(encoding="utf-8"))["offset"])
    except (OSError, ValueError, KeyError, TypeError):
        offset = 0
    size = transcript.stat().st_size
    if offset > size:  # rewritten or replaced: start again
        offset = 0
    with transcript.open("rb") as file:
        file.seek(offset)
        chunk = file.read(MAX_READ_BYTES)
    complete = chunk[: chunk.rfind(b"\n") + 1]  # a half-written last line waits for next time
    responses: dict[str, dict[str, Any]] = {}
    for raw in complete.splitlines():
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(line, dict) or (line.get("isSidechain") and not include_sidechain):  # pyright: ignore[reportUnknownMemberType]
            continue
        if (usage := response_usage(line)) is not None:  # pyright: ignore[reportUnknownArgumentType]
            responses[usage["message_id"]] = usage
    state_dir.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"offset": offset + len(complete)}), encoding="utf-8")
    return list(responses.values())
