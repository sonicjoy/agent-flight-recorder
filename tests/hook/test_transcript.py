"""Token usage from a Claude Code transcript: deduplicated, incremental, defensive."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flight_recorder.hook.transcript import new_usage


def response(
    message_id: str, output: int, *, sidechain: bool = False, **usage: Any
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "timestamp": "2026-11-11T09:00:00.000Z",
        "message": {
            "id": message_id,
            "model": "claude-opus-5-5",
            "usage": {"input_tokens": 10, "output_tokens": output} | usage,
        },
    }


CACHE: dict[str, Any] = {
    "cache_read_input_tokens": 1000,
    "cache_creation": {"ephemeral_5m_input_tokens": 200, "ephemeral_1h_input_tokens": 30},
}


def write(path: Path, *lines: Any, end: str = "\n") -> None:
    with path.open("a", encoding="utf-8") as file:
        for line in lines:
            file.write((line if isinstance(line, str) else json.dumps(line)) + end)


def test_one_entry_per_response_with_cache_tokens(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    write(
        transcript,
        {"type": "user", "message": {"content": "hi"}},
        response("msg_1", 5, **CACHE),
        response("msg_1", 40, **CACHE),
        response("msg_2", 7, speed="fast"),
        response("msg_sub", 9, sidechain=True),
        "not json",
    )  # fmt: skip

    usage = {u["message_id"]: u for u in new_usage(transcript, tmp_path / "state")}

    assert set(usage) == {"msg_1", "msg_2"}  # one per response; subagent lines skipped
    first = usage["msg_1"]
    assert first["output_tokens"] == 40  # the last line of a response wins
    assert first["input_tokens"] == 10 + 1000 + 200 + 30  # all input, as OTel counts it
    assert (
        first["cache_read_tokens"],
        first["cache_write_5m_tokens"],
        first["cache_write_1h_tokens"],
    ) == (1000, 200, 30)
    assert first["model"] == "claude-opus-5-5" and not first["fast"]
    assert usage["msg_2"]["fast"]


def test_only_new_lines_are_read_and_a_half_written_line_waits(tmp_path: Path) -> None:
    transcript, state = tmp_path / "session.jsonl", tmp_path / "state"
    write(transcript, response("msg_1", 5))
    write(transcript, json.dumps(response("msg_2", 6))[:30], end="")  # still being written

    assert [u["message_id"] for u in new_usage(transcript, state)] == ["msg_1"]

    write(transcript, json.dumps(response("msg_2", 6))[30:])
    assert [u["message_id"] for u in new_usage(transcript, state)] == ["msg_2"]
    assert new_usage(transcript, state) == []


def test_old_style_cache_creation_and_subagent_transcripts(tmp_path: Path) -> None:
    transcript = tmp_path / "agent.jsonl"
    write(transcript, response("msg_s", 3, sidechain=True, cache_creation_input_tokens=50))

    [usage] = new_usage(transcript, tmp_path / "state", include_sidechain=True)

    assert usage["cache_write_5m_tokens"] == 50 and usage["cache_write_1h_tokens"] == 0


def test_a_rewritten_transcript_is_read_again(tmp_path: Path) -> None:
    transcript, state = tmp_path / "session.jsonl", tmp_path / "state"
    write(transcript, response("msg_1", 5), response("msg_2", 5))
    new_usage(transcript, state)

    transcript.write_text(json.dumps(response("msg_3", 1)) + "\n", encoding="utf-8")

    assert [u["message_id"] for u in new_usage(transcript, state)] == ["msg_3"]


def test_unrecognised_or_missing_transcripts_give_nothing(tmp_path: Path) -> None:
    transcript = tmp_path / "odd.jsonl"
    write(
        transcript, {"type": "assistant", "message": "a string now"}, {"kind": "new-format"}, [1, 2]
    )

    assert new_usage(transcript, tmp_path / "state") == []
    assert new_usage(tmp_path / "missing.jsonl", tmp_path / "state") == []


def test_claude_codes_own_messages_are_not_model_calls(tmp_path: Path) -> None:
    """A real session's login error was written as an assistant message from "<synthetic>"."""
    transcript = tmp_path / "session.jsonl"
    synthetic = response("msg_err", 0)
    synthetic["message"]["model"] = "<synthetic>"
    write(transcript, synthetic, response("msg_real", 5))

    assert [u["message_id"] for u in new_usage(transcript, tmp_path / "state")] == ["msg_real"]
