"""Hook delivery under strain: backlogs drain in chunks, refusals don't loop, the spool stays
capped and private, a killed hook loses nothing, and the real process is isolated."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from flight_recorder.hook import client
from flight_recorder.hook.client import CHUNK_EVENTS, HookConfig, run
from flight_recorder.hook.install import hook_command

KEY = "test-key-" + "0123456789abcdef" * 2  # built, so the secret scanner sees no literal
DOWN = "http://127.0.0.1:9/v1/claude-code/events"


class Recorder:
    """A stand-in for afr serve that answers with a chosen status."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.batches: list[list[dict[str, Any]]] = []
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                recorder.batches.append(json.loads(body)["events"])
                self.send_response(recorder.status)
                self.end_headers()
                self.wfile.write(b'{"accepted": 0, "rejected": 0}')

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/claude-code/events"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def down_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused connection takes ~2 s on Windows; these tests only need it to fail."""
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


def event(n: int, name: str = "PreToolUse", **extra: Any) -> str:
    payload = {"session_id": "s", "hook_event_name": name, "tool_use_id": f"t{n}"}
    return json.dumps(payload | extra)


def spooled(home: Path) -> list[dict[str, Any]]:
    spool = home / "spool.jsonl"
    if not spool.exists():
        return []
    return [json.loads(line) for line in spool.read_text(encoding="utf-8").splitlines()]


def test_a_large_backlog_drains_in_chunks_oldest_first(tmp_path: Path, recorder: Recorder) -> None:
    for n in range(120):
        run(event(n), config(tmp_path))  # recorder down: spooled
    assert len(spooled(tmp_path)) == 120

    run(event(120), config(tmp_path, recorder.url))

    assert all(len(batch) <= CHUNK_EVENTS for batch in recorder.batches)
    sent = [e["hook"]["tool_use_id"] for batch in recorder.batches for e in batch]
    assert sent == [f"t{n}" for n in range(121)]
    assert spooled(tmp_path) == []


def test_events_the_recorder_refuses_are_dropped_not_retried_for_ever(tmp_path: Path) -> None:
    refusing = Recorder(status=413)
    try:
        run(event(1), config(tmp_path, refusing.url))
        run(event(2), config(tmp_path, refusing.url))
    finally:
        refusing.close()

    assert spooled(tmp_path) == []
    assert "recorder refused 1 events (HTTP 413); dropped" in (tmp_path / "hook.log").read_text(
        encoding="utf-8"
    )


def test_an_unavailable_recorder_keeps_events(tmp_path: Path) -> None:
    busy = Recorder(status=503)
    try:
        run(event(1), config(tmp_path, busy.url))
    finally:
        busy.close()

    assert [e["hook"]["tool_use_id"] for e in spooled(tmp_path)] == ["t1"]


def test_the_spool_keeps_its_cap_dropping_the_oldest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "MAX_SPOOL_BYTES", 2_000)

    for n in range(40):
        run(event(n), config(tmp_path))

    assert (tmp_path / "spool.jsonl").stat().st_size <= 2_000
    kept = [e["hook"]["tool_use_id"] for e in spooled(tmp_path)]
    assert kept[-1] == "t39" and "t0" not in kept  # newest kept, oldest dropped
    assert "spool full: dropped" in (tmp_path / "hook.log").read_text(encoding="utf-8")


def test_a_batch_left_by_a_killed_hook_is_sent_once_stale(
    tmp_path: Path, recorder: Recorder
) -> None:
    stale = tmp_path / "spool.jsonl.111.1.sending"
    fresh = tmp_path / "spool.jsonl.222.2.sending"
    stale.write_text(json.dumps({"received_at": 1, "hook": {"tool_use_id": "old"}}) + "\n")
    fresh.write_text(json.dumps({"received_at": 2, "hook": {"tool_use_id": "busy"}}) + "\n")
    past = time.time() - 600
    os.utime(stale, (past, past))

    run(event(1), config(tmp_path, recorder.url))

    sent = [e["hook"]["tool_use_id"] for batch in recorder.batches for e in batch]
    assert sent == ["old", "t1"]  # the fresh one may still be in another hook's hands
    assert not stale.exists() and fresh.exists()


def test_session_end_sends_only_its_own_event(tmp_path: Path, recorder: Recorder) -> None:
    run(event(1), config(tmp_path))  # spooled
    run(event(2, name="SessionEnd"), config(tmp_path, recorder.url))

    assert [[e["hook"]["hook_event_name"] for e in b] for b in recorder.batches] == [["SessionEnd"]]
    assert len(spooled(tmp_path)) == 1  # left for an async event, so exit stays quick


def transcript_with(path: Path, *message_ids: str) -> None:
    with path.open("a", encoding="utf-8") as file:
        for message_id in message_ids:
            line = {
                "type": "assistant",
                "message": {
                    "id": message_id,
                    "model": "claude-opus-5-5",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
            file.write(json.dumps(line) + "\n")


def test_transcript_lines_are_skipped_only_once_their_event_is_safe(
    tmp_path: Path, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript_with(transcript, "msg_1")
    stop = json.dumps(
        {"session_id": "s", "hook_event_name": "Stop", "transcript_path": str(transcript)}
    )

    def killed(*args: Any) -> None:
        raise RuntimeError("process killed mid-send")

    monkeypatch.setattr(client, "_deliver", killed)
    run(stop, config(tmp_path, recorder.url))  # lost: the position must not move
    monkeypatch.undo()
    run(stop, config(tmp_path, recorder.url))

    [[event]] = recorder.batches
    assert [u["message_id"] for u in event["usage"]] == ["msg_1"]


def test_the_real_hook_process_is_isolated_silent_and_exits_0(tmp_path: Path) -> None:
    """A hmac.py in the project folder (where Claude Code runs hooks) must not run."""
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "hijacked"
    (project / "hmac.py").write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
    argv = [sys.executable, "-I", "-m", "flight_recorder.hook"]
    assert " ".join(argv[1:]) in hook_command()
    env = {**os.environ, "AFR_HOME": str(tmp_path / "home"), "AFR_HOOK_URL": DOWN}
    env.pop("AFR_REDACTION_KEY", None)

    done = subprocess.run(
        argv, input=event(1).encode(), cwd=project, env=env, capture_output=True, timeout=60
    )

    assert done.returncode == 0
    assert (done.stdout, done.stderr) == (b"", b"")
    assert not marker.exists()
    assert len(spooled(tmp_path / "home")) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no owner-only mode bits")
def test_the_spool_and_its_folder_are_private(tmp_path: Path) -> None:
    home = tmp_path / "home"
    run(event(1), config(home))

    assert home.stat().st_mode & 0o077 == 0
    assert (home / "spool.jsonl").stat().st_mode & 0o077 == 0


def test_a_fresh_claim_is_not_mistaken_for_a_killed_hooks(
    tmp_path: Path, recorder: Recorder
) -> None:
    """Claiming keeps the spool's old mtime unless refreshed; it mustn't look stale at once."""
    run(event(1), config(tmp_path))  # spooled
    past = time.time() - 600
    os.utime(tmp_path / "spool.jsonl", (past, past))  # long idle since the last append

    claimed, events = client._claim_backlog(tmp_path)  # pyright: ignore[reportPrivateUsage]

    assert len(events) == 1
    assert all(time.time() - p.stat().st_mtime < 60 for p in claimed)


@pytest.mark.parametrize("status", [404, 421, 500])
def test_setup_mistakes_keep_the_events(tmp_path: Path, status: int) -> None:
    """A 404 (an older server) or 421 (a host name) is fixable: don't throw the backlog away."""
    wrong = Recorder(status=status)
    try:
        run(event(1), config(tmp_path, wrong.url))
    finally:
        wrong.close()

    assert [e["hook"]["tool_use_id"] for e in spooled(tmp_path)] == ["t1"]


def test_a_huge_backlog_drains_over_several_hooks(
    tmp_path: Path, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "MAX_CHUNKS_PER_HOOK", 2)
    for n in range(CHUNK_EVENTS * 3):
        run(event(n), config(tmp_path))

    run(event(999), config(tmp_path, recorder.url))

    assert len(recorder.batches) == 2
    assert len(spooled(tmp_path)) == CHUNK_EVENTS + 1  # the rest waits, oldest first
    assert "left for the next hook" in (tmp_path / "hook.log").read_text(encoding="utf-8")


def test_transcript_paths_are_not_sent(tmp_path: Path, recorder: Recorder) -> None:
    transcript = tmp_path / "AbCdEf0123456789GhIjKl0123456789MnOpQr.jsonl"  # entropy-ish name
    transcript.write_text("", encoding="utf-8")
    stop = {"session_id": "s", "hook_event_name": "Stop", "transcript_path": str(transcript)}

    run(json.dumps(stop), config(tmp_path, recorder.url))

    [[sent]] = recorder.batches
    assert "transcript_path" not in sent["hook"]
    assert sent["client_redactions"] == {}
