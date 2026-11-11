"""POST /v1/claude-code/events: hook events become one run per session with a span tree."""

from __future__ import annotations

from collections.abc import Callable, Generator
from decimal import Decimal
from typing import Any

import pytest
from fake_secrets import secrets_by_kind
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session

from flight_recorder.ingest.app import create_app
from flight_recorder.pricing import cost_usd
from flight_recorder.redact import Redactor
from flight_recorder.store.db import upgrade
from flight_recorder.store.models import Run, Span

pytestmark = pytest.mark.db

REDACTOR = Redactor(b"test-key-0123456789abcdef0123456789abcdef")
URL = "/v1/claude-code/events"
T0 = 1_794_300_000.0  # seconds since the epoch


@pytest.fixture
def store(make_database: Callable[[], str]) -> Generator[Engine]:
    url = make_database()
    upgrade(url)
    engine = create_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture
def client(store: Engine) -> TestClient:
    return TestClient(create_app(store, REDACTOR))


def ev(name: str, at: float, **hook: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "received_at": T0 + at,
        "hook": {"session_id": "sess-1", "hook_event_name": name} | hook,
    }
    if "usage" in hook:
        event["usage"] = event["hook"].pop("usage")
    return event


def usage(message_id: str, **counts: Any) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "model": "claude-opus-5-5",
        "timestamp": "2026-11-11T09:00:01Z",
    } | counts


def session_events(prompt: str = "Count the files in src\nthen stop") -> list[dict[str, Any]]:
    tokens = {
        "input_tokens": 1_250,
        "output_tokens": 80,
        "cache_read_tokens": 1_000,
        "cache_write_5m_tokens": 200,
    }
    return [
        ev("SessionStart", 0, source="startup", cwd="/repo", permission_mode="default"),
        ev("UserPromptSubmit", 1, prompt_id="p1", prompt=prompt),
        ev(
            "PreToolUse",
            2,
            prompt_id="p1",
            tool_name="Glob",
            tool_use_id="t1",
            tool_input={"pattern": "src/**"},
        ),
        ev(
            "PostToolUse",
            3,
            prompt_id="p1",
            tool_name="Glob",
            tool_use_id="t1",
            tool_input={"pattern": "src/**"},
            tool_response={"files": 12},
        ),
        ev(
            "PreToolUse",
            4,
            prompt_id="p1",
            tool_name="Bash",
            tool_use_id="t2",
            tool_input={"command": "ls missing"},
        ),
        ev(
            "PostToolUseFailure",
            5,
            prompt_id="p1",
            tool_name="Bash",
            tool_use_id="t2",
            tool_input={"command": "ls missing"},
            error="exit 2: no such file",
        ),
        ev(
            "PreToolUse",
            6,
            prompt_id="p1",
            agent_id="a1",
            tool_name="Grep",
            tool_use_id="t3",
            tool_input={"pattern": "def"},
        ),
        ev(
            "PostToolUse",
            7,
            prompt_id="p1",
            agent_id="a1",
            tool_name="Grep",
            tool_use_id="t3",
            tool_input={"pattern": "def"},
            tool_response="3 matches",
        ),
        ev(
            "SubagentStop",
            8,
            prompt_id="p1",
            agent_id="a1",
            agent_type="Explore",
            last_assistant_message="found 3",
            usage=[usage("msg_sub", input_tokens=50, output_tokens=5)],
        ),
        ev(
            "Stop",
            9,
            prompt_id="p1",
            last_assistant_message="There are 12 files.",
            usage=[usage("msg_1", **tokens)],
        ),
        ev("SessionEnd", 10, reason="prompt_input_exit"),
    ]


def stored(store: Engine) -> tuple[list[Run], dict[str, Span]]:
    with Session(store) as session:
        runs = list(session.scalars(select(Run)))
        return runs, {s.external_id: s for s in session.scalars(select(Span))}


def test_a_session_becomes_a_run_with_a_span_tree(client: TestClient, store: Engine) -> None:
    response = client.post(URL, json={"events": session_events()})

    assert response.json() == {"accepted": 11, "rejected": 0, "errors": []}
    [run], spans = stored(store)
    assert (run.source, run.external_id, run.agent) == ("claude_code", "sess-1", "claude-code")
    assert run.name == "Count the files in src"  # the first line of the first prompt
    assert run.status == "ok" and run.ended_at is not None
    assert run.attributes["claude_code.cwd"] == "/repo"
    root, turn = spans["session"], spans["turn:p1"]
    assert (root.kind, turn.kind, turn.parent_id) == ("agent", "step", root.id)
    assert turn.input == "Count the files in src\nthen stop"
    assert turn.output == "There are 12 files."
    glob, bash = spans["tool:t1"], spans["tool:t2"]
    assert (glob.name, glob.status, glob.parent_id, glob.output) == (
        "Glob",
        "ok",
        turn.id,
        {"files": 12},
    )
    assert glob.ended_at is not None and glob.ended_at > glob.started_at
    assert (bash.status, bash.error) == ("error", "exit 2: no such file")
    agent = spans["agent:a1"]
    assert (agent.kind, agent.name, agent.parent_id) == ("agent", "Explore", turn.id)
    assert spans["tool:t3"].parent_id == agent.id
    assert spans["llm:msg_sub"].parent_id == agent.id


def test_model_responses_are_priced_with_cache_rates(client: TestClient, store: Engine) -> None:
    client.post(URL, json={"events": session_events()})

    [run], spans = stored(store)
    llm = spans["llm:msg_1"]
    assert (llm.kind, llm.model, llm.parent_id) == (
        "llm",
        "anthropic:claude-opus-5-5",
        spans["turn:p1"].id,
    )
    expected = cost_usd("anthropic:claude-opus-5-5", 50, 80, cache_read=1_000, cache_write_5m=200)
    assert llm.cost_usd == Decimal(str(expected)).quantize(Decimal("0.00000001"))
    assert llm.attributes["usage"] == {"cache_read_tokens": 1_000, "cache_write_5m_tokens": 200}
    assert run.input_tokens == 1_250 + 50 and run.output_tokens == 80 + 5


def test_events_out_of_order_and_sent_twice_make_the_same_run(
    client: TestClient, store: Engine
) -> None:
    events = session_events()
    client.post(URL, json={"events": list(reversed(events))})  # the server orders by time
    first = stored(store)
    client.post(URL, json={"events": events})  # a re-send, e.g. from the spool

    [run], spans = stored(store)
    assert set(spans) == set(first[1])
    assert run.input_tokens == first[0][0].input_tokens


def test_a_tools_end_can_arrive_before_its_start(client: TestClient, store: Engine) -> None:
    post = ev(
        "PostToolUse",
        3,
        prompt_id="p1",
        tool_name="Read",
        tool_use_id="t9",
        tool_input={"f": 1},
        tool_response="ok",
    )
    pre = ev(
        "PreToolUse", 2, prompt_id="p1", tool_name="Read", tool_use_id="t9", tool_input={"f": 1}
    )
    client.post(URL, json={"events": [post]})
    client.post(URL, json={"events": [pre]})  # fired earlier, arrived later

    tool = stored(store)[1]["tool:t9"]
    assert tool.started_at < tool.ended_at  # pyright: ignore[reportOperatorIssue]
    assert (tool.input, tool.output, tool.status) == ({"f": 1}, "ok", "ok")


def test_without_tool_use_id_start_and_end_pair_by_tool_and_input(
    client: TestClient, store: Engine
) -> None:
    client.post(
        URL,
        json={
            "events": [
                ev("PreToolUse", 2, prompt_id="p1", tool_name="Read", tool_input={"file": "a.py"}),
                ev(
                    "PostToolUse",
                    3,
                    prompt_id="p1",
                    tool_name="Read",
                    tool_input={"file": "a.py"},
                    tool_response="text",
                ),
            ]
        },
    )

    tools = [s for s in stored(store)[1].values() if s.kind == "tool"]
    assert len(tools) == 1 and tools[0].output == "text" and tools[0].ended_at is not None


def test_a_later_prompt_does_not_rename_the_session(client: TestClient, store: Engine) -> None:
    client.post(
        URL,
        json={
            "events": [
                ev("UserPromptSubmit", 1, prompt_id="p1", prompt="first task"),
                ev("UserPromptSubmit", 5, prompt_id="p2", prompt="second task"),
            ]
        },
    )

    [run], spans = stored(store)
    assert run.name == "first task"
    assert {"turn:p1", "turn:p2"} <= set(spans)


def test_secrets_never_reach_the_database_even_if_the_hook_missed_them(
    client: TestClient, store: Engine
) -> None:
    secret = secrets_by_kind()["anthropic_key"]  # the server redacts again
    client.post(URL, json={"events": session_events(prompt=f"use {secret} please")})

    with store.connect() as connection:
        dump = " ".join(
            connection.scalar(text(f"SELECT coalesce(string_agg(t::text, ' '), '') FROM {t} t"))
            or ""
            for t in ("runs", "spans")
        )
    assert secret not in dump and "[REDACTED:anthropic_key:" in dump


def test_bad_events_are_counted_and_the_rest_stored(client: TestClient, store: Engine) -> None:
    response = client.post(
        URL,
        json={
            "events": [
                {"received_at": T0, "hook": {"hook_event_name": "Stop"}},  # no session
                "not an event",
                ev("UserPromptSubmit", 1, prompt_id="p1", prompt="ok"),
            ]
        },
    )

    body = response.json()
    assert (body["accepted"], body["rejected"]) == (1, 2)
    assert body["errors"] == ["hook payload needs session_id and hook_event_name"]
    assert len(stored(store)[0]) == 1


@pytest.mark.parametrize(
    ("body", "content_type", "status"),
    [
        (b"{}", "text/plain", 415),
        (b"not json", "application/json", 400),
        (b'{"events": 3}', "application/json", 400),
    ],
)
def test_refused_bodies(client: TestClient, body: bytes, content_type: str, status: int) -> None:
    assert (
        client.post(URL, content=body, headers={"Content-Type": content_type}).status_code == status
    )
