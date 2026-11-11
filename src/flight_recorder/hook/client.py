"""`python -m flight_recorder.hook`: one Claude Code hook event in on stdin, sent to the recorder.

It must never get in Claude Code's way:
- Installed as an async hook for every event but SessionEnd, so Claude Code doesn't wait
  for it. SessionEnd runs once, at exit, with a short send timeout.
- Fails open: every error is caught and logged (without content) to <AFR_HOME>/hook.log,
  and it always exits 0, which never blocks anything. It prints nothing, to stdout or
  stderr: an async hook's output reaches the model on its next turn.
- If the recorder is down, the event is kept in <AFR_HOME>/spool.jsonl and sent, oldest
  first, with the next event that gets through.

Before sending or spooling, it redacts (the first pass; the server redacts again) and caps
each string at MAX_FIELD_CHARS, since a tool can return a whole file.

It runs in other projects' folders, so it reads its three settings from the environment,
never from a .env file (which would be that project's): AFR_HOME, AFR_HOOK_URL and
AFR_REDACTION_KEY. It avoids heavy imports: a new process starts for every event.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flight_recorder.hook.transcript import new_usage
from flight_recorder.redact.key import KEY_NAME, key_from
from flight_recorder.redact.redactor import Redactor

MAX_FIELD_CHARS = 32_768
MAX_SPOOL_BYTES = 20 * 1024 * 1024
SEND_TIMEOUT_SECONDS = 3.0
SYNC_SEND_TIMEOUT_SECONDS = 0.5  # SessionEnd is synchronous; a local recorder answers in ms
SYNC_EVENTS = {"SessionEnd"}
DEFAULT_URL = "http://127.0.0.1:4318/v1/claude-code/events"
TURN_END = {
    "Stop": "transcript_path",
    "SubagentStop": "agent_transcript_path",
    "SessionEnd": "transcript_path",
}


@dataclass(frozen=True)
class HookConfig:
    home: Path
    url: str
    key: str | None

    @classmethod
    def from_env(cls) -> HookConfig:
        return cls(
            home=Path(os.environ.get("AFR_HOME") or Path.home() / ".afr"),
            url=os.environ.get("AFR_HOOK_URL") or DEFAULT_URL,
            key=os.environ.get("AFR_REDACTION_KEY") or None,
        )


def cap(value: Any) -> Any:
    """Truncate long strings at any depth, saying how much was cut."""
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        cut = len(value) - MAX_FIELD_CHARS
        return f"{value[:MAX_FIELD_CHARS]}...[truncated {cut} chars]"
    if isinstance(value, dict):
        items: dict[Any, Any] = value  # pyright: ignore[reportUnknownVariableType]
        return {k: cap(v) for k, v in items.items()}
    if isinstance(value, list):
        return [cap(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def build_event(payload: dict[str, Any], config: HookConfig, redactor: Redactor) -> dict[str, Any]:
    """The hook payload, plus when it fired and any new token usage, redacted and capped."""
    event: dict[str, Any] = {"received_at": time.time(), "hook": payload}
    if (key := TURN_END.get(str(payload.get("hook_event_name")))) and payload.get(key):
        sidechain = key == "agent_transcript_path"
        event["usage"] = new_usage(
            Path(str(payload[key])), config.home / "hook-state", include_sidechain=sidechain
        )
    redacted = redactor.redact(event)
    event = cap(redacted.value)
    event["client_redactions"] = dict(redacted.counts)
    return event


def _post(url: str, events: list[dict[str, Any]], timeout: float) -> None:
    body = json.dumps({"events": events}).encode()
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status >= 300:
            raise urllib.error.HTTPError(
                url, response.status, "not accepted", response.headers, None
            )


def _take_spool(spool: Path) -> list[dict[str, Any]]:
    """Claim the spooled events. Renaming first means two hooks can't both send them."""
    claimed = spool.with_name(f"{spool.name}.{os.getpid()}.sending")
    try:
        os.replace(spool, claimed)
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in claimed.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    claimed.unlink(missing_ok=True)
    return events


def _spool(spool: Path, events: list[dict[str, Any]]) -> None:
    spool.parent.mkdir(parents=True, exist_ok=True)
    if spool.exists() and spool.stat().st_size > MAX_SPOOL_BYTES:
        raise OSError(f"spool over {MAX_SPOOL_BYTES} bytes; dropping events")
    with spool.open("a", encoding="utf-8") as file:
        for event in events:
            file.write(json.dumps(event) + "\n")


def _log(home: Path, message: str) -> None:
    try:
        home.mkdir(parents=True, exist_ok=True)
        with (home / "hook.log").open("a", encoding="utf-8") as file:
            file.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


def run(stdin_text: str, config: HookConfig | None = None) -> None:
    """Handle one hook call. Never raises, never prints."""
    logging.disable(logging.CRITICAL)  # e.g. "generated a key": stderr isn't ours to use
    config = config or HookConfig.from_env()
    try:
        payload = json.loads(stdin_text)
        if not isinstance(payload, dict):
            raise ValueError("hook input is not a JSON object")
        if not payload.get("session_id") or not payload.get("hook_event_name"):  # pyright: ignore[reportUnknownMemberType]
            raise ValueError("hook input has no session_id or hook_event_name")
        redactor = Redactor(key_from(config.key, config.home / KEY_NAME))
        event = build_event(payload, config, redactor)  # pyright: ignore[reportUnknownArgumentType]
    except Exception as error:  # never let a bad event or key reach Claude Code
        _log(config.home, f"skipped event: {type(error).__name__}")
        return
    name = str(event["hook"].get("hook_event_name", "?"))
    spool = config.home / "spool.jsonl"
    pending = [*_take_spool(spool), event]
    timeout = SYNC_SEND_TIMEOUT_SECONDS if name in SYNC_EVENTS else SEND_TIMEOUT_SECONDS
    try:
        _post(config.url, pending, timeout)
    except Exception as error:  # recorder down or refusing: keep the events for next time
        _log(config.home, f"recorder unreachable ({type(error).__name__}); spooled {name}")
        try:
            _spool(spool, pending)
        except Exception as spool_error:
            _log(config.home, f"could not spool: {type(spool_error).__name__}")
    else:
        if len(pending) > 1:
            _log(config.home, f"sent {len(pending) - 1} spooled events")
