"""The hook's limits and the recorder's must agree: each chunk finishes within the send
timeout and under the request limit, and no event carries more than the recorder takes.
Changing one constant alone fails here (docs/claude-code.md, "Recorder down")."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, text

from flight_recorder.hook import client
from flight_recorder.hook.install import SYNC_TIMEOUT_SECONDS
from flight_recorder.ingest import claude_code, otlp
from flight_recorder.ingest.app import create_app
from flight_recorder.redact import Redactor
from flight_recorder.store.db import upgrade

URL = "/v1/claude-code/events"


def test_one_event_and_one_chunk_fit_what_the_recorder_takes() -> None:
    assert client.EVENT_ITEMS <= claude_code.MAX_ITEMS
    assert client.READ_ITEMS <= claude_code.MAX_ITEMS
    assert client.EVENT_BYTES <= client.CHUNK_BYTES < otlp.MAX_BODY_BYTES


def test_a_chunk_is_estimated_to_finish_well_within_the_timeout() -> None:
    full_event = (
        client.EVENT_SECONDS
        + client.EVENT_ITEMS * client.ITEM_SECONDS
        + client.EVENT_BYTES / 1_000_000 * client.MB_SECONDS
    )
    assert full_event <= client.CHUNK_SECONDS  # a full part fits a chunk on its own
    assert client.CHUNK_EVENTS * client.EVENT_SECONDS <= client.CHUNK_SECONDS
    assert client.CHUNK_SECONDS <= client.SEND_TIMEOUT_SECONDS * 2 / 3  # headroom


def test_session_end_fits_its_own_timeouts() -> None:
    """SessionEnd sends one bare event, then gives up: well inside Claude Code's limit."""
    assert client.EVENT_SECONDS <= client.SYNC_SEND_TIMEOUT_SECONDS
    assert client.SYNC_SEND_TIMEOUT_SECONDS * 2 <= SYNC_TIMEOUT_SECONDS


def stop(n: int, items: int, chars: int = 1_000) -> dict[str, Any]:
    usage = [
        {"message_id": f"msg_{n}_{i}", "model": "claude-opus-5-5",
         "timestamp": "2026-11-13T10:00:01Z", "input_tokens": 1000, "output_tokens": 10,
         "output": {"text": "word " * (chars // 5)}}
        for i in range(items)
    ]  # fmt: skip
    hook = {"session_id": "s", "hook_event_name": "Stop", "prompt_id": f"p{n}"}
    return {"received_at": 1_794_300_000 + n, "hook": hook, "usage": usage, "permissions": []}


@pytest.fixture
def store(make_database: Callable[[], str]) -> Generator[Engine]:
    url = make_database()
    upgrade(url)
    engine = create_engine(url)
    yield engine
    engine.dispose()


@pytest.mark.db
def test_a_full_chunk_of_turn_ends_is_applied_within_the_send_timeout(store: Engine) -> None:
    """Measured, not estimated: the chunk the hook would send, against the real app."""
    app = TestClient(create_app(store, Redactor(b"k" * 32)), base_url="http://127.0.0.1:4318")
    events = [stop(n, client.EVENT_ITEMS) for n in range(20)]
    [first, *_] = client._chunks(events)  # pyright: ignore[reportPrivateUsage]
    assert len(json.dumps({"events": first})) < otlp.MAX_BODY_BYTES

    started = time.perf_counter()
    reply = app.post(URL, json={"events": first}).json()
    took = time.perf_counter() - started

    assert reply["rejected"] == 0
    assert took < client.SEND_TIMEOUT_SECONDS, f"{len(first)} events took {took:.2f}s"


def timed(app: TestClient, events: list[dict[str, Any]]) -> float:
    started = time.perf_counter()
    reply = app.post(URL, json={"events": events}).json()
    assert reply["rejected"] == 0
    return time.perf_counter() - started


@pytest.mark.db
def test_a_chunk_of_the_biggest_parts_finishes_within_its_estimate(store: Engine) -> None:
    """The recorder redacts and stores every byte: full parts take longer than their items
    alone say (two of 2 MB took 2.1 to 2.4 s against 1.66 s estimated)."""
    app = TestClient(create_app(store, Redactor(b"k" * 32)), base_url="http://127.0.0.1:4318")
    events = [stop(n, client.EVENT_ITEMS, chars=14_000) for n in range(4)]
    assert all(len(json.dumps(e)) <= client.EVENT_BYTES for e in events)
    [first, *_] = client._chunks(events)  # pyright: ignore[reportPrivateUsage]
    timed(app, [stop(99, 1)])  # a running recorder: its first request's setup isn't counted

    took = timed(app, first)

    estimate = client.estimated_seconds(first)
    assert took <= estimate, f"{len(first)} parts took {took:.2f}s, estimated {estimate:.2f}s"


@pytest.mark.db
def test_a_chunk_takes_no_longer_late_in_a_long_session(store: Engine) -> None:
    """A Claude Code session is one run, applied an event at a time: an event's work must
    not grow with the spans already stored (50 events took 3.1 s in a 50,000-span run)."""
    app = TestClient(create_app(store, Redactor(b"k" * 32)), base_url="http://127.0.0.1:4318")
    hook = {"session_id": "s", "prompt_id": "p0"}
    timed(
        app,
        [{"received_at": 1_794_300_000, "hook": hook | {"hook_event_name": "UserPromptSubmit"}}],
    )
    with store.begin() as connection:  # 50,000 linked tool spans, as a long session leaves
        connection.execute(
            text("""
                INSERT INTO spans (run_id, external_id, parent_external_id, parent_id, kind, name,
                                   started_at)
                SELECT turn.run_id, 'tool:old_' || g, 'turn:p0', turn.id, 'tool', 'Bash', now()
                FROM spans AS turn, generate_series(1, 50000) AS g
                WHERE turn.external_id = 'turn:p0'
            """)
        )
        connection.execute(text("ANALYZE spans"))
    events = [
        {"received_at": 1_794_300_100 + n, "hook": hook | {"hook_event_name": "PreToolUse",
         "tool_name": "Bash", "tool_use_id": f"t{n}", "tool_input": {"command": f"ls {n}"}}}
        for n in range(client.CHUNK_EVENTS)
    ]  # fmt: skip
    [chunk] = client._chunks(events)  # pyright: ignore[reportPrivateUsage]

    took = timed(app, chunk)

    estimate = client.estimated_seconds(chunk)
    assert took <= estimate, f"{len(chunk)} events took {took:.2f}s, estimated {estimate:.2f}s"


def tool_event(name: str, n: int, order: int) -> dict[str, Any]:
    hook = {
        "session_id": "s",
        "prompt_id": "p0",
        "hook_event_name": name,
        "tool_name": "Bash",
        "tool_use_id": f"t{n}",
        "tool_input": {"command": f"ls {n}"},
    }
    if name == "PostToolUse":
        hook |= {"tool_response": {"stdout": "ok"}, "duration_ms": 10}
    if name == "PermissionDenied":
        hook |= {"reason": "not allowed"}
    return {"received_at": 1_794_300_100 + order, "hook": hook}  # fmt: skip


def spans_read(plan: dict[str, Any]) -> float:
    """Rows of spans a plan node and its children read, kept or filtered out."""
    own = 0.0
    if plan.get("Relation Name") == "spans":
        kept = plan["Actual Rows"] + plan.get("Rows Removed by Filter", 0)
        own = (kept + plan.get("Rows Removed by Index Recheck", 0)) * plan["Actual Loops"]
    return own + sum(spans_read(child) for child in plan.get("Plans", []))


@pytest.mark.db
def test_no_tool_or_permission_event_reads_the_whole_session(store: Engine) -> None:
    """Deterministic where timing is noisy: each statement an event runs, run again under
    EXPLAIN ANALYZE, reads a few rows of a 60,000-span session, not all of them. A
    PermissionRequest finds its tool, and a tool's end or a refusal its permission wait,
    by what they are rather than by id: those lookups scanned the session."""
    app = TestClient(create_app(store, Redactor(b"k" * 32)), base_url="http://127.0.0.1:4318")
    hook = {"session_id": "s", "prompt_id": "p0"}
    timed(
        app,
        [{"received_at": 1_794_300_000, "hook": hook | {"hook_event_name": "UserPromptSubmit"}}],
    )
    with store.begin() as connection:  # 50,000 finished tool calls, 10,000 of them waited
        connection.execute(
            text("""
                INSERT INTO spans (run_id, external_id, parent_external_id, parent_id, kind, name,
                                   input, started_at, ended_at)
                SELECT turn.run_id, 'tool:old_' || g, 'turn:p0', turn.id, 'tool', 'Bash',
                       jsonb_build_object('command', 'ls ' || g % 100),
                       to_timestamp(1794200000 + g), to_timestamp(1794200000 + g)
                FROM spans AS turn, generate_series(1, 50000) AS g
                WHERE turn.external_id = 'turn:p0'
            """)
        )
        connection.execute(
            text("""
                INSERT INTO spans (run_id, external_id, parent_external_id, parent_id, kind, name,
                                   input, started_at, ended_at)
                SELECT run_id, 'permission:old_' || external_id, external_id, id, 'step',
                       'permission: Bash', input, started_at, ended_at
                FROM spans WHERE external_id LIKE 'tool:old_%' AND random() < 0.2
            """)
        )
        connection.execute(text("ANALYZE spans"))
    events: list[dict[str, Any]] = []
    for n in range(3):  # a tool that asks first, and one whose request arrives first
        for name in ("PreToolUse", "PermissionRequest", "PostToolUse"):
            events.append(tool_event(name, n, len(events)))
    for name in ("PermissionRequest", "PreToolUse", "PermissionDenied"):
        events.append(tool_event(name, 9, len(events)))
    refused = {"tool_use_id": "t0", "decision": "reject", "source": "user", "at": None}
    events.append(
        {"received_at": 1_794_300_200, "hook": hook | {"hook_event_name": "Stop"},
         "usage": [], "permissions": [refused]}
    )  # fmt: skip
    statements: list[tuple[str, Any]] = []

    def record(*args: Any) -> None:
        statement, parameters = args[2], args[3]
        if "spans" in statement:
            statements.append((statement, parameters))

    event.listen(store, "before_cursor_execute", record)
    try:
        timed(app, events)
    finally:
        event.remove(store, "before_cursor_execute", record)

    assert len(statements) > 20
    with store.connect() as connection:
        for statement, parameters in statements:
            with connection.begin() as transaction:  # run it again, then undo it
                [[plans]] = connection.exec_driver_sql(
                    f"EXPLAIN (ANALYZE, FORMAT JSON) {statement}", parameters
                ).all()
                transaction.rollback()
            read = spans_read(plans[0]["Plan"])
            assert read < 1_000, f"read {read:.0f} spans: {statement}"
