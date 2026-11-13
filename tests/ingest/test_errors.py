"""What the ingest API does when writing one item fails, by what the error says: about the
moment (a lock, a deadlock, the database going away), so the caller should retry, or about
the item, so only that item is rejected. Each code is raised in both ingest paths."""

from __future__ import annotations

import decimal
import json
import logging
from collections.abc import Callable, Generator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import DataError, DBAPIError

from flight_recorder.ingest import app as ingest_app
from flight_recorder.ingest.app import create_app
from flight_recorder.redact import Redactor
from flight_recorder.store.db import upgrade

pytestmark = pytest.mark.db

REDACTOR = Redactor(b"test-key-0123456789abcdef0123456789abcdef")
BAD, GOOD = "bb" * 16, "aa" * 16

# About the moment: the same item will go in later.
TRANSIENT = {
    "40001": "serialization failure",
    "40P01": "deadlock detected",
    "40003": "statement completion unknown",
    "55P03": "lock not available",
    "55006": "object in use",
    "25006": "read-only transaction (a standby, or the database set read-only)",
    "25P03": "idle-in-transaction session timeout",
    "72000": "snapshot too old",
    "57014": "query canceled (statement timeout)",
    "57P01": "admin shutdown",
    "53300": "too many connections",
    "08006": "connection failure",
}
# About the item: the same item will fail the same way.
ITEM = {
    "54000": "program limit exceeded (an id too long for an index row)",
    "22003": "numeric value out of range",
    "23514": "check violation",
    "40002": "integrity violation at commit (a deferred constraint)",
}


def database_error(sqlstate: str) -> Exception:
    """The error SQLAlchemy raises for this SQLSTATE, wrapping psycopg's own class."""
    orig = psycopg.errors.lookup(sqlstate)("raised by the test")
    return DBAPIError.instance("INSERT ...", {"name": "recorded data"}, orig, psycopg.Error)


@pytest.fixture
def store(make_database: Callable[[], str]) -> Generator[Engine]:
    url = make_database()
    upgrade(url)
    engine = create_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture
def client(store: Engine) -> TestClient:
    return TestClient(create_app(store, REDACTOR), base_url="http://127.0.0.1:4318")


def failing_for(error: Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    """Writing the item named BAD raises error; the others are written as usual."""
    record_run, apply_event = ingest_app.record_run, ingest_app.apply_event

    def record(session: Any, run: Any, spans: Any, *, redactor: Redactor) -> Any:
        if run.external_id == BAD:
            raise error
        return record_run(session, run, spans, redactor=redactor)

    def apply(session: Any, redactor: Redactor, event: Any) -> None:
        if event["hook"]["session_id"] == BAD:
            raise error
        apply_event(session, redactor, event)

    monkeypatch.setattr(ingest_app, "record_run", record)
    monkeypatch.setattr(ingest_app, "apply_event", apply)


def post_traces(client: TestClient) -> Any:
    def span(trace: str) -> dict[str, Any]:
        return {"traceId": trace, "spanId": "01" * 8, "name": "s",
                "startTimeUnixNano": "1760000000000000000"}  # fmt: skip

    body = {"resourceSpans": [{"scopeSpans": [{"spans": [span(BAD), span(GOOD)]}]}]}
    return client.post(
        "/v1/traces", content=json.dumps(body), headers={"Content-Type": "application/json"}
    )


def post_events(client: TestClient) -> Any:
    def event(session_id: str, at: float) -> dict[str, Any]:
        return {"received_at": at, "hook": {"session_id": session_id,
                                            "hook_event_name": "SessionStart"}}  # fmt: skip

    return client.post("/v1/claude-code/events", json={"events": [event(BAD, 1), event(GOOD, 2)]})


@pytest.mark.parametrize("sqlstate", TRANSIENT, ids=TRANSIENT.values())
def test_an_error_about_the_moment_is_a_503_for_otlp(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlstate: str
) -> None:
    failing_for(database_error(sqlstate), monkeypatch)

    response = post_traces(client)

    assert (response.status_code, response.headers.get("retry-after")) == (503, "5")


@pytest.mark.parametrize("sqlstate", TRANSIENT, ids=TRANSIENT.values())
def test_an_error_about_the_moment_is_a_503_for_hook_events(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlstate: str
) -> None:
    failing_for(database_error(sqlstate), monkeypatch)

    response = post_events(client)

    assert (response.status_code, response.headers.get("retry-after")) == (503, "5")


@pytest.mark.parametrize("sqlstate", ITEM, ids=ITEM.values())
def test_an_error_about_the_item_rejects_it_alone_for_otlp(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlstate: str
) -> None:
    failing_for(database_error(sqlstate), monkeypatch)

    response = post_traces(client)

    assert response.status_code == 200
    assert response.json()["partialSuccess"]["rejectedSpans"] == "1"


@pytest.mark.parametrize("sqlstate", ITEM, ids=ITEM.values())
def test_an_error_about_the_item_rejects_it_alone_for_hook_events(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlstate: str
) -> None:
    failing_for(database_error(sqlstate), monkeypatch)

    response = post_events(client)

    assert response.status_code == 200
    assert (response.json()["accepted"], response.json()["rejected"]) == (1, 1)


# ---- Errors an item can cause, and errors nobody expected ------------------------------

SECRET_MESSAGE = "raised by the test"  # an error's message may quote recorded data

# What one item can legitimately get wrong: rejected alone.
ITEM_RAISES: dict[str, Callable[[], Exception]] = {
    "a value": lambda: ValueError(SECRET_MESSAGE),
    "a number out of range": lambda: OverflowError(SECRET_MESSAGE),
    "a token count too big to price": lambda: decimal.InvalidOperation(SECRET_MESSAGE),
    "nesting too deep": lambda: RecursionError(SECRET_MESSAGE),
    "a NUL psycopg refuses": lambda: DataError("INSERT ...", {}, Exception(SECRET_MESSAGE)),
}
# Nobody expected these: a bug, or a database behind the code. Kept, logged, retried.
UNEXPECTED_RAISES: dict[str, Callable[[], Exception]] = {
    "a bug (TypeError)": lambda: TypeError(SECRET_MESSAGE),
    "a bug (KeyError)": lambda: KeyError(SECRET_MESSAGE),
    "a table missing (migrations behind)": lambda: database_error("42P01"),
    "an object not ready (55000)": lambda: database_error("55000"),
}


@pytest.mark.parametrize("make", ITEM_RAISES.values(), ids=ITEM_RAISES.keys())
@pytest.mark.parametrize("path", ["otlp", "hook"])
def test_an_error_an_item_can_cause_rejects_it_alone(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, make: Callable[[], Exception], path: str
) -> None:
    failing_for(make(), monkeypatch)

    response = post_traces(client) if path == "otlp" else post_events(client)

    assert response.status_code == 200
    if path == "otlp":
        assert response.json()["partialSuccess"]["rejectedSpans"] == "1"
    else:
        assert (response.json()["accepted"], response.json()["rejected"]) == (1, 1)


@pytest.mark.parametrize("make", UNEXPECTED_RAISES.values(), ids=UNEXPECTED_RAISES.keys())
@pytest.mark.parametrize("path", ["otlp", "hook"])
def test_an_unexpected_error_is_logged_without_data_and_the_item_kept(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    make: Callable[[], Exception],
    path: str,
) -> None:
    error = make()
    failing_for(error, monkeypatch)

    with caplog.at_level(logging.ERROR, logger="flight_recorder.ingest.app"):
        response = post_traces(client) if path == "otlp" else post_events(client)

    assert (response.status_code, response.headers.get("retry-after")) == (503, "5")
    logged = caplog.text
    assert type(error).__name__ in logged
    assert "test_errors.py" in logged  # where it was raised: enough to find the bug
    assert SECRET_MESSAGE not in logged and "recorded data" not in logged  # never data
    assert SECRET_MESSAGE not in response.text
