"""The MCP server (#24): its tools over an in-memory client, its errors, stdio and imports.

The server opens its own sessions, which can't see a rolled-back test transaction, so these
tests commit to a scratch database of their own. None reaches the real store: the stdio
tests point AFR_DATABASE_URL at a closed port and run in a temporary folder (no `.env`).
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Generator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fake_embedder import FakeEmbedder
from mcp import Client, StdioServerParameters
from mcp.types import LATEST_PROTOCOL_VERSION
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from flight_recorder import queries
from flight_recorder.mcp_server import (
    BEHIND_MESSAGE,
    NOT_DOWNLOADED_MESSAGE,
    TIMEOUT_MESSAGE,
    UNTRUSTED,
    create_server,
)
from flight_recorder.redact import Redactor
from flight_recorder.search.embedder import ModelNotDownloaded
from flight_recorder.search.index import embed_runs
from flight_recorder.store.db import UNREACHABLE, downgrade, readonly_session, upgrade
from flight_recorder.store.labels import add_label
from flight_recorder.store.write import RunIn, SpanIn, record_run

pytestmark = [pytest.mark.db, pytest.mark.anyio]

REDACTOR = Redactor(b"test-key-0123456789abcdef0123456789abcdef")
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
CLOSED = "postgresql+psycopg://afr:afr@127.0.0.1:1/afr"  # nothing listens on port 1
TOOLS = {"search_runs", "explain_run", "list_failure_modes"}
HIDDEN = "\x1b[2J‮"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def store(make_database: Callable[[], str]) -> Generator[Engine]:
    """A scratch database of the test's own, migrated: rows committed here stay here."""
    url = make_database()
    upgrade(url)
    engine = create_engine(url)
    yield engine
    engine.dispose()


def _span(external_id: str, parent: str | None, **values: Any) -> SpanIn:
    fields: dict[str, Any] = {
        "external_id": external_id,
        "parent_external_id": parent,
        "kind": "step",
        "name": external_id,
        "started_at": T0,
        "ended_at": T0 + timedelta(seconds=1),
        "status": "ok",
    }
    return SpanIn(**(fields | values))


def _failed_run(engine: Engine) -> uuid.UUID:
    """A run whose error, a decided field's name and a label note hold hidden characters."""
    with Session(engine) as session:
        run_id = record_run(
            session,
            RunIn(
                source="otel",
                external_id="trace-1",
                agent="triager",
                name="triage issue 12",
                status="error",
                started_at=T0,
                ended_at=T0 + timedelta(seconds=5),
                attributes={f"triager.decided.labels{HIDDEN}": "bug"},
            ),
            [
                _span("root", None, kind="agent", status="error", error="boom"),
                _span("llm", "root", kind="llm", status="error", error=f"timed out {HIDDEN}"),
            ],
            redactor=REDACTOR,
        )
        add_label(session, run_id, "timeout", f"note {HIDDEN}", author="owner", redactor=REDACTOR)
        session.commit()
    return run_id


async def _call(server: Any, tool: str, arguments: dict[str, Any]) -> Any:
    async with Client(server) as client:
        return await client.call_tool(tool, arguments)


def _error_text(result: Any) -> str:
    """The tool's own message, without the SDK's "Error executing tool <name>: " prefix."""
    assert result.is_error, result
    return "".join(part.text for part in result.content).split(": ", 1)[1]


# --- the tools ---------------------------------------------------------------------------


async def test_three_read_only_tools_each_warning_that_fields_are_untrusted(store: Engine) -> None:
    async with Client(create_server(store, FakeEmbedder)) as client:
        listed = (await client.list_tools()).tools

    assert {tool.name for tool in listed} == TOOLS
    for tool in listed:
        assert tool.description is not None and UNTRUSTED in tool.description
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
    explain = next(t for t in listed if t.name == "explain_run")
    assert "at least 4 hex characters" in json.dumps(explain.input_schema)


async def test_explain_run_returns_escaped_structured_data(store: Engine) -> None:
    run_id = _failed_run(store)

    result = await _call(
        create_server(store, FakeEmbedder), "explain_run", {"run": str(run_id)[:8]}
    )

    assert not result.is_error, result
    out = result.structured_content
    assert out["run"]["id"] == str(run_id)
    [failure] = out["failures"]
    assert failure["error"] == "timed out \\x1b[2J\\u202e"
    assert list(out["decisions"]) == ["labels\\x1b[2J\\u202e"]
    assert out["labels"][0]["note"] == "note \\x1b[2J\\u202e"
    assert "\x1b" not in json.dumps(out, ensure_ascii=False)


async def test_search_and_modes_answer(store: Engine) -> None:
    run_id = _failed_run(store)
    with Session(store) as session:
        embed_runs(session, FakeEmbedder())
        session.commit()
    server = create_server(store, FakeEmbedder)

    found = await _call(server, "search_runs", {"query": "timed out", "status": "error"})
    modes = await _call(server, "list_failure_modes", {})

    assert [h["run"]["id"] for h in found.structured_content["hits"]] == [str(run_id)]
    assert [m["mode"] for m in modes.structured_content["modes"]] == ["timeout"]


# --- errors ------------------------------------------------------------------------------


async def test_a_run_that_doesnt_exist_is_an_error_naming_it(store: Engine) -> None:
    result = await _call(create_server(store, FakeEmbedder), "explain_run", {"run": "ffffffff"})
    assert "no run matches 'ffffffff'" in _error_text(result)


async def test_an_unreachable_store_is_an_error_that_says_so() -> None:
    engine = create_engine(CLOSED, connect_args={"connect_timeout": 3})
    try:
        result = await _call(create_server(engine, FakeEmbedder), "list_failure_modes", {})
    finally:
        engine.dispose()
    assert _error_text(result) == UNREACHABLE


async def test_a_store_behind_the_code_says_to_upgrade_or_restart(
    make_database: Callable[[], str],
) -> None:
    url = make_database()
    upgrade(url)
    downgrade(url, "-1")
    engine = create_engine(url)
    try:
        result = await _call(create_server(engine, FakeEmbedder), "list_failure_modes", {})
    finally:
        engine.dispose()
    assert _error_text(result) == BEHIND_MESSAGE


async def test_a_store_ahead_of_the_code_says_to_restart(store: Engine) -> None:
    with store.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = 'from_a_newer_branch'"))

    result = await _call(create_server(store, FakeEmbedder), "list_failure_modes", {})

    assert _error_text(result) == BEHIND_MESSAGE


async def test_a_slow_query_hits_the_statement_timeout(
    store: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow(session: Session, source: str, limit: int) -> dict[str, Any]:
        session.execute(text("SELECT pg_sleep(1)"))
        return {}

    monkeypatch.setattr(queries, "failure_modes", slow)
    server = create_server(store, FakeEmbedder, statement_timeout="100ms")

    result = await _call(server, "list_failure_modes", {})

    assert _error_text(result) == TIMEOUT_MESSAGE.format(timeout="100ms")


async def test_a_missing_model_says_to_run_afr_embed(store: Engine) -> None:
    _failed_run(store)
    with Session(store) as session:
        embed_runs(session, FakeEmbedder())
        session.commit()

    class NotDownloaded(FakeEmbedder):
        def query(self, value: str) -> list[float]:
            raise ModelNotDownloaded("bge")

        def documents(self, texts: Sequence[str]) -> list[list[float]]:
            raise ModelNotDownloaded("bge")

    result = await _call(create_server(store, NotDownloaded), "search_runs", {"query": "x"})

    assert _error_text(result) == NOT_DOWNLOADED_MESSAGE


async def test_an_unexpected_error_is_named_by_its_class_only(
    store: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(session: Session, source: str, limit: int) -> dict[str, Any]:
        raise RuntimeError("secret text from a run")

    monkeypatch.setattr(queries, "failure_modes", broken)

    result = await _call(create_server(store, FakeEmbedder), "list_failure_modes", {})

    assert _error_text(result) == "unexpected error: RuntimeError"


def test_the_servers_session_cant_write(store: Engine) -> None:
    with readonly_session(store) as session, pytest.raises(DBAPIError, match="read-only"):
        session.execute(text("CREATE TABLE nope (id int)"))


# --- stdio and imports -------------------------------------------------------------------


def _server_env(tmp_path: Path) -> dict[str, str]:
    return {**os.environ, "AFR_DATABASE_URL": CLOSED, "AFR_HOME": str(tmp_path / "afr-home")}


async def test_over_stdio_it_lists_its_tools_and_survives_a_store_error(tmp_path: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "flight_recorder.mcp_server"],
        env=_server_env(tmp_path),
        cwd=str(tmp_path),
    )
    async with Client(params) as client:
        listed = (await client.list_tools()).tools
        first = await client.call_tool("list_failure_modes", {})
        second = await client.call_tool("list_failure_modes", {})

    assert {tool.name for tool in listed} == TOOLS
    assert "Postgres" in _error_text(first) and "Postgres" in _error_text(second)


def test_stdout_carries_only_json_rpc(tmp_path: Path) -> None:
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": LATEST_PROTOCOL_VERSION, "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]  # fmt: skip
    server = subprocess.Popen(
        [sys.executable, "-m", "flight_recorder.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=_server_env(tmp_path),
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
    )
    assert server.stdin is not None and server.stdout is not None
    lines: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(target=lambda: [lines.put(line) for line in server.stdout or []])
    reader.start()
    try:
        for message in messages:
            server.stdin.write(json.dumps(message) + "\n")
            server.stdin.flush()
        seen: list[dict[str, Any]] = []
        while {m.get("id") for m in seen} < {1, 2}:
            line = lines.get(timeout=30)
            seen.append(json.loads(line))  # anything but JSON fails here
        assert all(m["jsonrpc"] == "2.0" for m in seen)
        tools = next(m for m in seen if m.get("id") == 2)["result"]["tools"]
        assert {tool["name"] for tool in tools} == TOOLS
    finally:
        server.stdin.close()
        server.wait(timeout=30)
        reader.join(timeout=30)
    while not lines.empty():
        json.loads(lines.get())


def test_importing_the_server_loads_no_cli_and_connects_to_nothing(tmp_path: Path) -> None:
    check = (
        "import sys, flight_recorder.mcp_server; "
        "assert not [m for m in sys.modules if m.startswith('flight_recorder.cli')]"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", check],
        env=_server_env(tmp_path),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""
