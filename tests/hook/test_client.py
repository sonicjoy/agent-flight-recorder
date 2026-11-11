"""`afr hook run`: redacts before anything leaves, spools when the recorder is down, and
never fails or prints."""

from __future__ import annotations

import json
import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fake_secrets import secrets_by_kind

from flight_recorder.hook.client import MAX_FIELD_CHARS, HookConfig, run

KEY = "test-key-" + "0123456789abcdef" * 2  # built, so the secret scanner sees no literal


class Recorder:
    """A stand-in for afr serve that keeps what it's sent."""

    def __init__(self) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                recorder.batches.append(json.loads(body)["events"])
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/claude-code/events"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def recorder() -> Generator[Recorder]:
    server = Recorder()
    yield server
    server.server.shutdown()
    server.server.server_close()  # shutdown() alone leaves the socket open


def settings(home: Path, url: str = "http://127.0.0.1:9/unreachable") -> HookConfig:
    return HookConfig(home=home, url=url, key=KEY)


def tool_event(secret: str, **extra: Any) -> str:
    return json.dumps(
        {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "prompt_id": "p1",
            "tool_name": "Bash",
            "tool_use_id": "toolu_1",
            "tool_input": {"command": f"export OPENAI_API_KEY={secret}"},
            "tool_response": {"stdout": "x" * (MAX_FIELD_CHARS + 500)},
        }
        | extra
    )


def test_recorder_down_spools_a_redacted_event_and_logs_no_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = secrets_by_kind()["openai_key"]

    run(tool_event(secret), settings(tmp_path))

    spooled = (tmp_path / "spool.jsonl").read_text(encoding="utf-8")
    assert secret not in spooled and "[REDACTED:openai_key:" in spooled
    [event] = [json.loads(line) for line in spooled.splitlines()]
    assert event["client_redactions"] == {"openai_key": 1}
    assert event["hook"]["tool_response"]["stdout"].endswith("...[truncated 500 chars]")
    log = (tmp_path / "hook.log").read_text(encoding="utf-8")
    assert "spooled 1 (PostToolUse)" in log and secret not in log
    assert capsys.readouterr() == ("", "")  # async hook output would reach the model


def test_spooled_events_are_sent_first_with_the_next_one(
    tmp_path: Path, recorder: Recorder
) -> None:
    run(tool_event("one"), settings(tmp_path))  # recorder down: spooled
    run(tool_event("two", tool_use_id="toolu_2"), settings(tmp_path, recorder.url))

    [batch] = recorder.batches
    assert [e["hook"]["tool_use_id"] for e in batch] == ["toolu_1", "toolu_2"]
    assert not (tmp_path / "spool.jsonl").exists()


def test_turn_end_attaches_new_token_usage(tmp_path: Path, recorder: Recorder) -> None:
    transcript = tmp_path / "session.jsonl"
    line = {
        "type": "assistant",
        "message": {
            "id": "msg_1",
            "model": "claude-opus-5-5",
            "usage": {"input_tokens": 3, "output_tokens": 4},
        },
    }
    transcript.write_text(json.dumps(line) + "\n", encoding="utf-8")
    stop = {
        "session_id": "sess-1",
        "hook_event_name": "Stop",
        "prompt_id": "p1",
        "transcript_path": str(transcript),
    }

    run(json.dumps(stop), settings(tmp_path, recorder.url))

    [[event]] = recorder.batches
    assert [u["message_id"] for u in event["usage"]] == ["msg_1"]


@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]", "{}", '{"session_id": "s"}'])
def test_bad_input_is_skipped_quietly(
    tmp_path: Path, stdin: str, capsys: pytest.CaptureFixture[str]
) -> None:
    run(stdin, settings(tmp_path))  # must not raise

    assert not (tmp_path / "spool.jsonl").exists()
    assert "skipped event" in (tmp_path / "hook.log").read_text(encoding="utf-8")
    assert capsys.readouterr() == ("", "")


def test_no_output_even_when_it_generates_a_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tool_event("x"), HookConfig(home=tmp_path, url="http://127.0.0.1:9/", key=None))

    assert (tmp_path / "redaction.key").exists()
    assert capsys.readouterr() == ("", "")


def test_settings_come_from_the_environment_not_a_dot_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # another project's folder, with its own .env
    (tmp_path / ".env").write_text("AFR_HOOK_URL=http://elsewhere.example/", encoding="utf-8")
    monkeypatch.setenv("AFR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AFR_HOOK_URL", raising=False)
    monkeypatch.delenv("AFR_REDACTION_KEY", raising=False)

    config = HookConfig.from_env()

    assert config.home == tmp_path / "home"
    assert config.url == "http://127.0.0.1:4318/v1/claude-code/events"
