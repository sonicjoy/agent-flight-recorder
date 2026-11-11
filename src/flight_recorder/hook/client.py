"""`python -I -m flight_recorder.hook`: one Claude Code hook event in on stdin, to the recorder.

It must never get in Claude Code's way:
- Installed as an async hook for every event but SessionEnd, so Claude Code doesn't wait
  for it. SessionEnd runs once, at exit, sends only its own event, with a short timeout.
- Fails open: every error is caught and logged (without content) to <AFR_HOME>/hook.log,
  and it always exits 0, which never blocks anything. It prints nothing, to stdout or
  stderr: an async hook's output reaches the model on its next turn.
- If the recorder is down, the event is kept in <AFR_HOME>/spool.jsonl. Later async events
  send the backlog first, oldest first, in chunks small enough to finish within the timeout
  and under the server's size limit. A chunk the server refuses outright (a 4xx) is dropped
  and logged rather than retried for ever. The spool keeps at most MAX_SPOOL_BYTES, newest
  events first. A hook killed mid-send leaves its claimed batch behind; the next hook picks
  it up once it's stale.

Before sending or spooling, it redacts (the first pass; the server redacts again) and caps
each string at MAX_FIELD_CHARS, since a tool can return a whole file. The counts of what it
redacted go with the event, so the server can store them.

It runs in other projects' folders, so it reads its three settings from the environment,
never from a .env file (which would be that project's): AFR_HOME, AFR_HOOK_URL and
AFR_REDACTION_KEY. It is started with `python -I`, so a file such as json.py in the project
folder can't replace a standard module. It avoids heavy imports: a new process starts for
every event.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flight_recorder.hook.install import SYNC_EVENTS
from flight_recorder.hook.transcript import TranscriptRead, read_new
from flight_recorder.redact.key import KEY_NAME, key_from
from flight_recorder.redact.redactor import Redactor

MAX_FIELD_CHARS = 32_768
MAX_SPOOL_BYTES = 20 * 1024 * 1024
CHUNK_EVENTS = 50  # the server applies ~60 events a second: a chunk fits the timeout
CHUNK_BYTES = 4 * 1024 * 1024  # well under the server's 10 MB request limit
SEND_TIMEOUT_SECONDS = 3.0
SYNC_SEND_TIMEOUT_SECONDS = 0.5  # SessionEnd is synchronous; a local recorder answers in ms
STALE_CLAIM_SECONDS = 120  # a claimed batch older than this was left by a killed hook
DEFAULT_URL = "http://127.0.0.1:4318/v1/claude-code/events"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
DROP_STATUSES = {400, 413, 422}  # the events themselves were refused; others are setup
MAX_CHUNKS_PER_HOOK = 10  # a big backlog drains over several hooks, keeping its progress
PATH_KEYS = ("transcript_path", "agent_transcript_path")  # local paths the server doesn't use
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


class Refused(Exception):
    """The recorder refused the events themselves (400, 413, 422): retrying won't help."""


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


def build_event(
    payload: dict[str, Any], config: HookConfig, redactor: Redactor
) -> tuple[dict[str, Any], TranscriptRead | None]:
    """The hook payload, plus when it fired and any new token usage, redacted and capped.

    Also returns the transcript read, to commit once the event is safely sent or spooled.
    """
    event: dict[str, Any] = {"received_at": time.time(), "hook": payload}
    read = None
    if (key := TURN_END.get(str(payload.get("hook_event_name")))) and payload.get(key):
        read = read_new(
            Path(str(payload[key])),
            config.home / "hook-state",
            include_sidechain=key == "agent_transcript_path",
        )
        event["usage"] = read.usage
    event["hook"] = {k: v for k, v in payload.items() if k not in PATH_KEYS}
    redacted = redactor.redact(event)
    event = cap(redacted.value)
    event["client_redactions"] = dict(redacted.counts)
    return event, read


def _opener(url: str) -> urllib.request.OpenerDirector:
    """No system proxy for a recorder on this machine."""
    if urllib.parse.urlsplit(url).hostname in LOOPBACK:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _post(url: str, events: list[dict[str, Any]], timeout: float) -> dict[str, Any]:
    body = json.dumps({"events": events}).encode()
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with _opener(url).open(request, timeout=timeout) as response:
            reply = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        if error.code in DROP_STATUSES:
            raise Refused(f"HTTP {error.code}") from error
        raise  # 404, 421, 5xx...: a wrong URL or a recorder in trouble; keep the events
    except ValueError:  # accepted (2xx) but not JSON: delivered all the same
        return {}
    return reply if isinstance(reply, dict) else {}  # pyright: ignore[reportUnknownVariableType]


def _chunks(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = [[]]
    size = 0
    for event in events:
        length = len(json.dumps(event))
        if chunks[-1] and (len(chunks[-1]) >= CHUNK_EVENTS or size + length > CHUNK_BYTES):
            chunks.append([])
            size = 0
        chunks[-1].append(event)
        size += length
    return chunks


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)  # owner-only where modes apply


def _claim_backlog(home: Path) -> tuple[list[Path], list[dict[str, Any]]]:
    """Take the spool, plus any batch a killed hook claimed and never finished."""
    spool = home / "spool.jsonl"
    claimed: list[Path] = []
    mine = home / f"spool.jsonl.{os.getpid()}.{time.time_ns()}.sending"
    try:
        os.replace(spool, mine)
        os.utime(mine)  # fresh from now: the spool's own mtime is its last append
        claimed.append(mine)
    except OSError:
        pass
    for stale in home.glob("spool.jsonl.*.sending"):
        try:
            if stale not in claimed and time.time() - stale.stat().st_mtime > STALE_CLAIM_SECONDS:
                taken = stale.with_suffix(f".{os.getpid()}.taken.sending")
                os.replace(stale, taken)  # only one hook gets it
                claimed.append(taken)
        except OSError:
            continue
    events: list[dict[str, Any]] = []
    for path in claimed:  # oldest events first: each is ordered by received_at on the server
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:  # another hook took it after all: its events go with that hook
            continue
        for line in lines:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    return claimed, events


def _spool(home: Path, events: list[dict[str, Any]]) -> int:
    """Append, keeping the spool under MAX_SPOOL_BYTES (newest win). Returns how many dropped."""
    _private_dir(home)
    spool = home / "spool.jsonl"
    room = MAX_SPOOL_BYTES - (spool.stat().st_size if spool.exists() else 0)
    lines = [json.dumps(e) + "\n" for e in events]
    kept: list[str] = []
    for line in reversed(lines):  # newest first
        if len(line.encode()) > room:
            break
        kept.append(line)
        room -= len(line.encode())
    fd = os.open(spool, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as file:
        file.writelines(reversed(kept))
    return len(lines) - len(kept)


def _log(home: Path, message: str) -> None:
    try:
        _private_dir(home)
        with (home / "hook.log").open("a", encoding="utf-8") as file:
            file.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


def _deliver(config: HookConfig, event: dict[str, Any], name: str) -> None:
    """Send the backlog (async events only) and this event; spool whatever didn't get through."""
    sync = name in SYNC_EVENTS
    try:
        claimed, backlog = ([], []) if sync else _claim_backlog(config.home)
    except OSError as error:  # can't read the spool: still send or spool this event
        _log(config.home, f"could not read the spool: {type(error).__name__}")
        claimed, backlog = [], []
    pending = [*backlog, event]
    timeout = SYNC_SEND_TIMEOUT_SECONDS if sync else SEND_TIMEOUT_SECONDS
    sent = rejected = refused = 0
    failure = ""
    for number, chunk in enumerate(_chunks(pending)):
        if number == MAX_CHUNKS_PER_HOOK:  # leave the rest for the next hook
            failure = "deferred"
            break
        try:
            reply = _post(config.url, chunk, timeout)
        except Refused as error:  # the server won't take these: don't retry them for ever
            refused += len(chunk)
            sent += len(chunk)
            _log(config.home, f"recorder refused {len(chunk)} events ({error}); dropped")
            continue
        except Exception as error:  # down, timed out: keep the rest for next time
            failure = type(error).__name__
            break
        sent += len(chunk)
        rejected += int(reply.get("rejected") or 0)
    unsent = pending[sent:]
    if unsent:
        dropped = _spool(config.home, unsent)
        reason = (
            "left for the next hook"
            if failure == "deferred"
            else f"recorder unreachable ({failure})"
        )
        _log(config.home, f"{reason}; spooled {len(unsent)} ({name})")
        if dropped:
            _log(config.home, f"spool full: dropped the {dropped} oldest events")
    for path in claimed:  # only now: a hook killed before this leaves them for the next one
        path.unlink(missing_ok=True)
    if rejected:
        _log(config.home, f"recorder rejected {rejected} events (see the recorder's reply)")
    if backlog and sent > refused:
        _log(config.home, f"sent {min(len(backlog), sent)} spooled events")


def run(stdin_text: str, config: HookConfig | None = None) -> None:
    """Handle one hook call. Never raises, never prints."""
    logging.disable(logging.CRITICAL)  # e.g. "generated a key": stderr isn't ours to use
    config = config or HookConfig.from_env()
    try:
        payload = json.loads(stdin_text)
        if not isinstance(payload, dict):
            raise ValueError("hook input is not a JSON object")
        name = payload.get("hook_event_name")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if not payload.get("session_id") or not isinstance(name, str) or not name:  # pyright: ignore[reportUnknownMemberType]
            raise ValueError("hook input has no session_id or hook_event_name")
        redactor = Redactor(key_from(config.key, config.home / KEY_NAME))
        event, read = build_event(payload, config, redactor)  # pyright: ignore[reportUnknownArgumentType]
    except Exception as error:  # never let a bad event or key reach Claude Code
        _log(config.home, f"skipped event: {type(error).__name__}")
        return
    try:
        _deliver(config, event, name)
        if read is not None:
            read.commit()  # sent or spooled: the transcript lines are safe to skip next time
    except Exception as error:
        _log(config.home, f"could not deliver or spool {name}: {type(error).__name__}")
