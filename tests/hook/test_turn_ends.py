"""A turn end is sent in bounded parts: a long turn, a huge response or a first read of an
old session's transcript can neither stall the backlog nor be cut or refused whole."""

from __future__ import annotations

import json
import threading
import urllib.error
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from flight_recorder.hook import client
from flight_recorder.hook.client import (
    CHUNK_SECONDS,
    EVENT_BYTES,
    EVENT_ITEMS,
    READ_ITEMS,
    HookConfig,
    estimated_seconds,
    run,
)
from flight_recorder.ingest.otlp import MAX_BODY_BYTES

KEY = "test-key-" + "0123456789abcdef" * 2
DOWN = "http://127.0.0.1:9/v1/claude-code/events"


class Recorder:
    """A stand-in for afr serve: refuses bodies over the real limit, answers with `reply`."""

    def __init__(self, reply: dict[str, Any] | None = None) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self.sizes: list[int] = []
        recorder = self
        body = json.dumps(reply or {"accepted": 0, "rejected": 0, "errors": []}).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                raw = self.rfile.read(length)
                recorder.sizes.append(length)
                if length > MAX_BODY_BYTES:
                    self.send_response(413)
                    self.end_headers()
                    return
                recorder.batches.append(json.loads(raw)["events"])
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/claude-code/events"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def events(self) -> list[dict[str, Any]]:
        return [e for batch in self.batches for e in batch]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def down_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    real = client._post  # pyright: ignore[reportPrivateUsage]

    def post(url: str, events: list[dict[str, Any]], timeout: float) -> dict[str, Any]:
        if url == DOWN:
            raise urllib.error.URLError("connection refused")
        return real(url, events, timeout)

    monkeypatch.setattr(client, "_post", post)


@pytest.fixture
def recorder() -> Generator[Recorder]:
    server = Recorder()
    yield server
    server.close()


def config(home: Path, url: str = DOWN) -> HookConfig:
    return HookConfig(home=home, url=url, key=KEY)


def transcript(path: Path, responses: int, chars: int = 10, start: int = 0) -> None:
    with path.open("a", encoding="utf-8") as file:
        for n in range(start, start + responses):
            line = {
                "type": "assistant",
                "timestamp": "2026-11-13T10:00:00.000Z",
                "message": {
                    "id": f"msg_{n:06d}",
                    "model": "claude-opus-5-5",
                    "content": [{"type": "text", "text": "word " * (chars // 5)}],
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
            }
            file.write(json.dumps(line) + "\n")


def turn_end(path: Path, name: str = "Stop") -> str:
    key = "agent_transcript_path" if name == "SubagentStop" else "transcript_path"
    payload = {"session_id": "s", "hook_event_name": name, "prompt_id": "p1", key: str(path)}
    return json.dumps(payload | ({"agent_id": "a1"} if name == "SubagentStop" else {}))


def sent_ids(events: list[dict[str, Any]]) -> list[str]:
    return [u["message_id"] for e in events for u in e.get("usage", [])]


def log(home: Path) -> str:
    path = home / "hook.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def items(event: dict[str, Any]) -> int:
    return len(event.get("usage", [])) + len(event.get("permissions", []))


@pytest.mark.parametrize("name", ["Stop", "SubagentStop"])
def test_a_long_turn_goes_in_parts_that_each_fit_the_timeout(
    tmp_path: Path, recorder: Recorder, name: str
) -> None:
    path = tmp_path / "t.jsonl"
    transcript(path, READ_ITEMS + 200)

    run(turn_end(path, name), config(tmp_path, recorder.url))  # a page of READ_ITEMS
    run(turn_end(path, name), config(tmp_path, recorder.url))  # the rest

    events = recorder.events()
    assert sorted(sent_ids(events)) == [f"msg_{n:06d}" for n in range(READ_ITEMS + 200)]
    assert all(items(e) <= EVENT_ITEMS for e in events)
    assert all(estimated_seconds(batch) <= CHUNK_SECONDS for batch in recorder.batches)
    assert {e["hook"]["hook_event_name"] for e in events} == {name}  # each part is the event


def test_a_turn_larger_than_the_request_limit_is_sent_whole(
    tmp_path: Path, recorder: Recorder
) -> None:
    """400 responses of 30 KB: 12 MB as one event, which the recorder refused with a 413
    while the transcript position moved on: the turn's model calls were gone."""
    path = tmp_path / "t.jsonl"
    transcript(path, 400, chars=30_000)

    run(turn_end(path), config(tmp_path, recorder.url))

    assert max(recorder.sizes) <= MAX_BODY_BYTES
    assert all(len(json.dumps(e)) <= EVENT_BYTES for e in recorder.events())
    assert sorted(sent_ids(recorder.events())) == [f"msg_{n:06d}" for n in range(400)]
    assert "refused" not in log(tmp_path)


def test_a_response_too_big_for_any_event_keeps_its_tokens_and_says_what_was_cut(
    tmp_path: Path, recorder: Recorder
) -> None:
    path = tmp_path / "t.jsonl"
    huge = {f"field_{n}": "x" * 30_000 for n in range(100)}  # 3 MB of capped strings
    line = {
        "type": "assistant",
        "message": {
            "id": "msg_big",
            "model": "claude-opus-5-5",
            "content": [{"type": "tool_use", "id": "t1", "name": "Write", "input": huge}],
            "usage": {"input_tokens": 7, "output_tokens": 9},
        },
    }
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")

    run(turn_end(path), config(tmp_path, recorder.url))

    [event] = recorder.events()
    [response] = event["usage"]
    assert (response["input_tokens"], response["output_tokens"]) == (7, 9)
    assert len(json.dumps(event)) <= EVENT_BYTES
    assert "too big to send" in json.dumps(response["output"])
    assert "dropped the output of 1 model responses" in log(tmp_path)


def test_parts_of_a_turn_end_are_spooled_with_the_position_saved(
    tmp_path: Path, recorder: Recorder
) -> None:
    """Recorder down: every part goes to the spool, so the lines needn't be read again."""
    path = tmp_path / "t.jsonl"
    transcript(path, 250)

    run(turn_end(path), config(tmp_path))  # down: spooled
    run(turn_end(path), config(tmp_path))  # nothing new to read
    run(
        json.dumps({"session_id": "s", "hook_event_name": "PreToolUse"}),
        config(tmp_path, recorder.url),
    )

    ids = sent_ids(recorder.events())
    assert sorted(ids) == [f"msg_{n:06d}" for n in range(250)]  # each once
    assert all(items(e) <= EVENT_ITEMS for e in recorder.events())


def test_session_end_sends_its_own_event_first_and_spools_the_usage(
    tmp_path: Path, recorder: Recorder
) -> None:
    """SessionEnd has 0.5 s: it closes the run at once, and leaves the responses it read
    to the next async hook rather than time out on them."""
    path = tmp_path / "t.jsonl"
    transcript(path, 150)

    run(turn_end(path, "SessionEnd"), config(tmp_path, recorder.url))

    [[first]] = recorder.batches
    assert (first["hook"]["hook_event_name"], items(first)) == ("SessionEnd", 0)
    spooled = [json.loads(line) for line in (tmp_path / "spool.jsonl").read_text().splitlines()]
    assert sorted(sent_ids(spooled)) == [f"msg_{n:06d}" for n in range(150)]
    assert "left for the next hook" in log(tmp_path)


def test_a_full_spool_drops_its_oldest_event_not_the_session_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "MAX_SPOOL_BYTES", 2_000)
    for n in range(40):
        run(
            json.dumps(
                {"session_id": "s", "hook_event_name": "PreToolUse", "tool_use_id": f"t{n}"}
            ),
            config(tmp_path),
        )

    run(json.dumps({"session_id": "s", "hook_event_name": "SessionEnd"}), config(tmp_path))

    spool = tmp_path / "spool.jsonl"
    kept = [json.loads(line)["hook"] for line in spool.read_text().splitlines()]
    assert kept[-1]["hook_event_name"] == "SessionEnd"
    assert "t0" not in [h.get("tool_use_id") for h in kept]
    assert spool.stat().st_size <= 2_000


def test_why_the_recorder_rejected_an_event_is_logged(tmp_path: Path) -> None:
    rejecting = Recorder({"accepted": 0, "rejected": 1, "errors": ["IntegrityError"]})
    try:
        run(
            json.dumps({"session_id": "s", "hook_event_name": "PreToolUse"}),
            config(tmp_path, rejecting.url),
        )
    finally:
        rejecting.close()

    assert "recorder rejected 1 events: IntegrityError" in log(tmp_path)


def test_items_the_recorder_cut_are_logged(tmp_path: Path) -> None:
    cutting = Recorder({"accepted": 1, "rejected": 0, "errors": [], "dropped_items": 5})
    try:
        run(
            json.dumps({"session_id": "s", "hook_event_name": "PreToolUse"}),
            config(tmp_path, cutting.url),
        )
    finally:
        cutting.close()

    assert "recorder dropped 5 usage or decision items" in log(tmp_path)


def test_a_transcript_that_cannot_be_read_still_sends_the_turn_end(
    tmp_path: Path, recorder: Recorder
) -> None:
    path = tmp_path / "t.jsonl"
    transcript(path, 3)
    run(turn_end(path), config(tmp_path, recorder.url))
    [state] = (tmp_path / "hook-state").glob("*.json")
    state.write_text('{"offset": -1}', encoding="utf-8")  # damaged

    run(turn_end(path), config(tmp_path, recorder.url))

    assert [e["hook"]["hook_event_name"] for e in recorder.events()] == ["Stop", "Stop"]
    assert "skipped event" not in log(tmp_path)
