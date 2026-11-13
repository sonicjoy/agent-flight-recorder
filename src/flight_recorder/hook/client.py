"""`python -I -m flight_recorder.hook`: one Claude Code hook event in on stdin, to the recorder.

It must never get in Claude Code's way:
- Installed as an async hook for every event but SessionEnd, so Claude Code doesn't wait
  for it. SessionEnd runs once, at exit, sends only its own event, with a short timeout.
- Fails open: every error is caught and logged (without content) to <AFR_HOME>/hook.log,
  and it always exits 0, which never blocks anything. It prints nothing, to stdout or
  stderr: an async hook's output reaches the model on its next turn.
- A turn end (Stop, SubagentStop, SessionEnd) carries the model responses and permission
  decisions written since the last one ("items"). It is sent as one or more parts, each a
  whole event with at most EVENT_ITEMS items and EVENT_BYTES, which the recorder merges, so
  no single event can outgrow a request or the timeout. One hook reads at most READ_ITEMS
  items; the rest waits for the next turn end.
- If the recorder is down, the event is kept in <AFR_HOME>/spool.jsonl. Later async events
  send the backlog first, oldest first, in chunks that are estimated to finish well within
  the timeout (EVENT_SECONDS per event, ITEM_SECONDS per item) and are under the server's
  size limit. A chunk the server refuses outright (a 4xx) is dropped and logged rather than
  retried for ever. The spool keeps at most MAX_SPOOL_BYTES, dropping the oldest events.
  A hook killed mid-send leaves its claimed batch behind; the next hook picks it up once
  it's stale. Whatever is dropped, by the hook or the recorder, is logged.

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
from typing import Any, cast

from flight_recorder.hook.install import SYNC_EVENTS
from flight_recorder.hook.transcript import TranscriptRead, read_new
from flight_recorder.redact.key import KEY_NAME, key_from
from flight_recorder.redact.redactor import Redactor

MAX_FIELD_CHARS = 32_768
MAX_SPOOL_BYTES = 20 * 1024 * 1024
# A turn end's responses and decisions ("items") go in parts of at most this many, and an
# event of at most this size. tests/ingest/test_limits.py checks that the limits agree.
EVENT_ITEMS = 100
EVENT_BYTES = 2 * 1024 * 1024
READ_ITEMS = 1_000  # read from a transcript by one hook; the rest waits for the next turn end
# The recorder's time to apply an event, and each item in it: measured on Windows with a
# local Postgres (an ordinary event 20 to 28 ms, a response 4 to 6 ms), rounded up.
EVENT_SECONDS = 0.03
ITEM_SECONDS = 0.008
CHUNK_SECONDS = 2.0  # a chunk's estimated time, with headroom under SEND_TIMEOUT_SECONDS
CHUNK_EVENTS = 50
CHUNK_BYTES = 4 * 1024 * 1024  # well under the server's 10 MB request limit
SEND_TIMEOUT_SECONDS = 3.0
SYNC_SEND_TIMEOUT_SECONDS = 0.5  # SessionEnd is synchronous; it sends one bare event
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


def build_events(
    payload: dict[str, Any], config: HookConfig, redactor: Redactor
) -> tuple[list[dict[str, Any]], TranscriptRead | None]:
    """The hook payload, plus when it fired and any new token usage, redacted and capped: one
    event, or for a turn end its parts (see _split).

    Also returns the transcript read, to commit once the events are safely sent or spooled.
    """
    name = str(payload.get("hook_event_name"))
    event: dict[str, Any] = {"received_at": time.time(), "hook": payload}
    read = None
    if (key := TURN_END.get(name)) and payload.get(key):
        try:
            read = read_new(
                Path(str(payload[key])),
                config.home / "hook-state",
                include_sidechain=key == "agent_transcript_path",
                max_items=READ_ITEMS,
            )
        except Exception as error:  # the turn still ends; only its token usage is missing
            _log(config.home, f"could not read the transcript ({type(error).__name__}): {name}")
        event["usage"] = read.usage if read else []
        event["permissions"] = read.permissions if read else []
        if read is not None and read.more:
            _log(config.home, f"read {_items(event)} transcript items ({name}); rest next time")
    event["hook"] = {k: v for k, v in payload.items() if k not in PATH_KEYS}
    redacted = redactor.redact(event)
    event = cap(redacted.value)
    event["client_redactions"] = dict(redacted.counts)
    if "usage" not in event:
        return [event], read
    parts, cut = _split(event)
    if cut:
        _log(config.home, f"dropped the output of {cut} model responses too big to send")
    if name in SYNC_EVENTS and _items(event):  # its own event first, alone: it must be quick
        parts.insert(0, event | {"usage": [], "permissions": []})
    return parts, read


def _items(event: dict[str, Any]) -> int:
    """How many responses and decisions an event carries: the recorder's work is per item."""
    lists = (event.get("usage"), event.get("permissions"))
    return sum(len(cast("list[Any]", v)) for v in lists if isinstance(v, list))


def estimated_seconds(events: list[dict[str, Any]]) -> float:
    """What the recorder is expected to take to apply these events."""
    return sum(EVENT_SECONDS + ITEM_SECONDS * _items(e) for e in events)


def _size(value: Any) -> int:
    return len(json.dumps(value))


def _split(event: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """A turn end as parts of at most EVENT_ITEMS items and EVENT_BYTES: each is the same
    event with some of its items, and the recorder merges them. A response too big for any
    part keeps its tokens but not its output. Returns the parts and how many outputs were
    cut. Events spooled by an older hook are split the same way when they are sent."""
    usage, permissions = event.get("usage"), event.get("permissions")
    every = [("usage", u) for u in (cast("list[Any]", usage) if isinstance(usage, list) else [])]
    every += [
        ("permissions", p)
        for p in (cast("list[Any]", permissions) if isinstance(permissions, list) else [])
    ]
    base: dict[str, Any] = {**event, **_empty()}
    room = EVENT_BYTES - _size(base)
    parts: list[dict[str, Any]] = []
    current: dict[str, list[Any]] = _empty()
    count = size = cut = 0
    for kind, item in every:
        length = _size(item) + 2  # and its separator
        if length > room and kind == "usage" and isinstance(item, dict):
            dropped = f"[output dropped by the hook: {length} bytes, too big to send]"
            item = cast("dict[str, Any]", item) | {"output": {"text": dropped}}
            length, cut = _size(item) + 2, cut + 1
        if count and (count >= EVENT_ITEMS or size + length > room):
            parts.append({**base, **current})
            current, count, size = _empty(), 0, 0
        current[kind].append(item)
        count, size = count + 1, size + length
    if count or not parts:
        parts.append({**base, **current})
    return parts, cut


def _empty() -> dict[str, list[Any]]:
    return {"usage": [], "permissions": []}


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
    """Requests that each stay within CHUNK_EVENTS, CHUNK_BYTES and CHUNK_SECONDS."""
    chunks: list[list[dict[str, Any]]] = [[]]
    size, seconds = 0, 0.0
    for event in events:
        length, cost = _size(event), estimated_seconds([event])
        over = size + length > CHUNK_BYTES or seconds + cost > CHUNK_SECONDS
        if chunks[-1] and (len(chunks[-1]) >= CHUNK_EVENTS or over):
            chunks.append([])
            size, seconds = 0, 0.0
        chunks[-1].append(event)
        size, seconds = size + length, seconds + cost
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
                spooled = json.loads(line)
            except ValueError:
                continue
            if isinstance(spooled, dict):  # a damaged line is skipped, as the server would
                events.append(cast("dict[str, Any]", spooled))
    return claimed, events


def _newest(lines: list[str], room: int) -> list[str]:
    """The newest lines that fit in room bytes, oldest first."""
    kept: list[str] = []
    for line in reversed(lines):
        if len(line.encode()) > room:
            break
        kept.append(line)
        room -= len(line.encode())
    return kept[::-1]


def _spool(home: Path, events: list[dict[str, Any]]) -> int:
    """Append, keeping the spool under MAX_SPOOL_BYTES by dropping its oldest events, whether
    this hook claimed the spool (async events) or not (SessionEnd). Returns how many dropped."""
    _private_dir(home)
    spool = home / "spool.jsonl"
    lines = [json.dumps(e) + "\n" for e in events]
    held = spool.stat().st_size if spool.exists() else 0
    if held + sum(len(line.encode()) for line in lines) > MAX_SPOOL_BYTES:
        try:  # full: rewrite it without its oldest events
            every = spool.read_text(encoding="utf-8").splitlines(keepends=True) + lines
            kept = _newest(every, MAX_SPOOL_BYTES)
            partial = home / f"spool.jsonl.{os.getpid()}.tmp"
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.writelines(kept)
            os.replace(partial, spool)  # an append by another hook at this moment is lost
            return len(every) - len(kept)
        except OSError:  # held open elsewhere (Windows): keep what still fits after it
            pass
    kept = _newest(lines, MAX_SPOOL_BYTES - (spool.stat().st_size if spool.exists() else 0))
    fd = os.open(spool, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as file:
        file.writelines(kept)
    return len(lines) - len(kept)


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _log(home: Path, message: str) -> None:
    try:
        _private_dir(home)
        with (home / "hook.log").open("a", encoding="utf-8") as file:
            file.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


def _deliver(config: HookConfig, events: list[dict[str, Any]], name: str) -> None:
    """Send the backlog (async events only) and these events; spool whatever didn't get
    through. SessionEnd sends only its first event, alone, and spools any other parts."""
    sync = name in SYNC_EVENTS
    try:
        claimed, backlog = ([], []) if sync else _claim_backlog(config.home)
    except OSError as error:  # can't read the spool: still send or spool these events
        _log(config.home, f"could not read the spool: {type(error).__name__}")
        claimed, backlog = [], []
    backlog = [part for event in backlog for part in _split(event)[0]]
    pending = [*backlog, *events]
    timeout = SYNC_SEND_TIMEOUT_SECONDS if sync else SEND_TIMEOUT_SECONDS
    chunks = [[e] for e in pending] if sync else _chunks(pending)
    limit = 1 if sync else MAX_CHUNKS_PER_HOOK
    sent = rejected = refused = cut = 0
    reasons: set[str] = set()
    failure = ""
    for number, chunk in enumerate(chunks):
        if number == limit:  # leave the rest for the next hook
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
        rejected += _count(reply.get("rejected"))
        cut += _count(reply.get("dropped_items"))
        errors = reply.get("errors")
        reasons |= (
            {str(e) for e in cast("list[Any]", errors)} if isinstance(errors, list) else set()
        )
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
    if rejected:  # the recorder's reasons are field and type names, never data
        why = "; ".join(sorted(reasons))[:500] or "no reason given"
        _log(config.home, f"recorder rejected {rejected} events: {why}")
    if cut:
        _log(config.home, f"recorder dropped {cut} usage or decision items over its limit")
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
        events, read = build_events(payload, config, redactor)  # pyright: ignore[reportUnknownArgumentType]
    except Exception as error:  # never let a bad event or key reach Claude Code
        _log(config.home, f"skipped event: {type(error).__name__}")
        return
    try:
        _deliver(config, events, name)
        if read is not None:
            read.commit()  # sent or spooled: the transcript lines are safe to skip next time
    except Exception as error:
        _log(config.home, f"could not deliver or spool {name}: {type(error).__name__}")
