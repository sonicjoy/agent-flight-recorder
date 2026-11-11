"""Token usage from a Claude Code transcript: deduplicated, incremental, defensive."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flight_recorder.hook.transcript import new_usage, read_new


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


def test_a_response_has_its_request_time_and_what_it_said(tmp_path: Path) -> None:
    """Blocks of one response arrive on separate lines; the api-request line says when it
    was sent."""
    transcript = tmp_path / "session.jsonl"

    def block(at: str, content: dict[str, Any], stop: str | None = None) -> dict[str, Any]:
        line: dict[str, Any] = response("msg_1", 5) | {"timestamp": at, "requestRef": "req-1"}
        line["message"] = line["message"] | {"content": [content], "stop_reason": stop}
        return line

    write(
        transcript,
        {"type": "api-request", "id": "req-1", "timestamp": "2026-11-11T09:00:00.000Z"},
        block("2026-11-11T09:00:02.000Z", {"type": "thinking", "thinking": "Look first."}),
        block("2026-11-11T09:00:02.500Z", {"type": "text", "text": "Let me look."}),
        block("2026-11-11T09:00:03.000Z", {"type": "tool_use", "id": "t1", "name": "Glob",
                                          "input": {"pattern": "*"}}, "tool_use"),
        response("msg_2", 1),  # no request line seen, no content
    )  # fmt: skip

    first, second = new_usage(transcript, tmp_path / "state")

    assert (first["requested_at"], first["started_at"], first["timestamp"]) == (
        "2026-11-11T09:00:00.000Z",
        "2026-11-11T09:00:02.000Z",
        "2026-11-11T09:00:03.000Z",
    )
    assert first["stop_reason"] == "tool_use"
    assert first["output"] == {
        "thinking": "Look first.",
        "text": "Let me look.",
        "tool_calls": [{"id": "t1", "name": "Glob", "input": {"pattern": "*"}}],
    }
    assert (second["requested_at"], second["output"]) == (None, {})


def test_permission_decisions_are_read_from_tool_results(tmp_path: Path) -> None:
    transcript, state = tmp_path / "session.jsonl", tmp_path / "state"

    def result(tool_use_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "user",
            "timestamp": "2026-11-11T09:00:04Z",
            "permissionDecision": decision,
            "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id}]},
        }

    write(
        transcript,
        result("t1", {"decision": "accept", "source": "user_temporary", "grantScope": "once"}),
        result("t2", {"decision": "reject", "source": "config", "reasonType": "classifier",
                      "reasonCode": "classifier:auto-mode"}),
        {"type": "user", "message": {"content": "no decision here"}},
    )  # fmt: skip

    read = read_new(transcript, state)

    assert read.permissions == [
        {"tool_use_id": "t1", "decision": "accept", "source": "user_temporary",
         "reason_type": None, "reason_code": None, "at": "2026-11-11T09:00:04Z"},
        {"tool_use_id": "t2", "decision": "reject", "source": "config",
         "reason_type": "classifier", "reason_code": "classifier:auto-mode",
         "at": "2026-11-11T09:00:04Z"},
    ]  # fmt: skip


def test_a_request_read_a_turn_before_its_response_keeps_its_time(tmp_path: Path) -> None:
    """A queued prompt: the previous turn's Stop reads the next request line."""
    transcript, state = tmp_path / "session.jsonl", tmp_path / "state"
    write(transcript, {"type": "api-request", "id": "req-2", "timestamp": "2026-11-11T09:00:05Z"})
    assert new_usage(transcript, state) == []

    write(transcript, response("msg_2", 3) | {"requestRef": "req-2"})
    [later] = new_usage(transcript, state)

    assert later["requested_at"] == "2026-11-11T09:00:05Z"


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
