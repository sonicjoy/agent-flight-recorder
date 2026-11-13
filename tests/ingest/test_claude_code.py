"""POST /v1/claude-code/events: hook events become one run per session with a span tree."""

from __future__ import annotations

from collections.abc import Callable, Generator
from datetime import UTC, datetime
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
    return TestClient(create_app(store, REDACTOR), base_url="http://127.0.0.1:4318")


def ev(name: str, at: float, **hook: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "received_at": T0 + at,
        "hook": {"session_id": "sess-1", "hook_event_name": name} | hook,
    }
    for key in ("usage", "permissions"):  # sent beside the hook payload, not in it
        if key in hook:
            event[key] = event["hook"].pop(key)
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


def test_the_hooks_redaction_counts_are_stored(client: TestClient, store: Engine) -> None:
    """The hook already replaced the secret, so only its own counts say one was there."""
    pre = ev("PreToolUse", 2, prompt_id="p1", tool_name="Bash", tool_use_id="t1", tool_input={})
    pre["client_redactions"] = {"openai_key": 1}
    post = ev("PostToolUse", 3, prompt_id="p1", tool_name="Bash", tool_use_id="t1", tool_input={})
    post["client_redactions"] = {"openai_key": 1, "email": 2}

    client.post(URL, json={"events": [pre, post]})

    tool = stored(store)[1]["tool:t1"]
    assert tool.redactions == {"openai_key": 1, "email": 2}  # the larger count per pattern


def test_events_arriving_one_by_one_in_reverse_make_the_same_run(
    client: TestClient, store: Engine
) -> None:
    """Each request applied as it comes, so the merge, not the server's sort, is tested."""
    for event in reversed(session_events()):
        assert client.post(URL, json={"events": [event]}).status_code == 200

    [run], spans = stored(store)
    glob = spans["tool:t1"]
    assert (glob.input, glob.output, glob.status) == ({"pattern": "src/**"}, {"files": 12}, "ok")
    assert glob.ended_at is not None and glob.started_at < glob.ended_at
    turn = spans["turn:p1"]
    assert (turn.input, turn.output) == ("Count the files in src\nthen stop", "There are 12 files.")
    assert run.name == "Count the files in src" and run.status == "ok"


def test_a_response_read_again_at_session_end_keeps_its_turn(
    client: TestClient, store: Engine
) -> None:
    response = [usage("msg_1", input_tokens=10, output_tokens=2)]
    client.post(
        URL,
        json={
            "events": [
                ev("UserPromptSubmit", 1, prompt_id="p1", prompt="hi"),
                ev("Stop", 2, prompt_id="p1", usage=response),
                ev("SessionEnd", 3, usage=response),  # read again before Stop's position was saved
            ]
        },
    )

    spans = stored(store)[1]
    assert spans["llm:msg_1"].parent_id == spans["turn:p1"].id


def test_subagent_start_gives_the_subagent_a_real_duration(
    client: TestClient, store: Engine
) -> None:
    client.post(
        URL,
        json={
            "events": [
                ev("SubagentStart", 1, prompt_id="p1", agent_id="a1", agent_type="Explore"),
                ev("SubagentStop", 5, prompt_id="p1", agent_id="a1", agent_type="Explore"),
            ]
        },
    )

    agent = stored(store)[1]["agent:a1"]
    assert agent.ended_at is not None
    assert (agent.ended_at - agent.started_at).total_seconds() == 4


def test_a_response_spans_from_its_first_transcript_line_to_its_last(
    client: TestClient, store: Engine
) -> None:
    item = usage("msg_1", input_tokens=1, output_tokens=1) | {
        "started_at": "2026-11-11T09:00:00Z",
        "timestamp": "2026-11-11T09:00:03Z",
    }
    client.post(URL, json={"events": [ev("Stop", 4, prompt_id="p1", usage=[item])]})

    llm = stored(store)[1]["llm:msg_1"]
    assert llm.ended_at is not None
    assert (llm.ended_at - llm.started_at).total_seconds() == 3


@pytest.mark.parametrize(
    ("host", "status"),
    [("127.0.0.1:4318", 200), ("localhost:4318", 200), ("[::1]:4318", 200), ("evil.example", 421)],
)
def test_only_loopback_hosts_are_served(client: TestClient, host: str, status: int) -> None:
    """A web page using DNS rebinding sends its own host name: refuse it (until #25)."""
    response = client.post(URL, json={"events": []}, headers={"Host": host})

    assert response.status_code == status


def test_only_known_pattern_names_are_kept_from_the_hooks_counts(
    client: TestClient, store: Engine
) -> None:
    """The keys are stored unredacted, and anything local can post: accept pattern names only."""
    pre = ev("PreToolUse", 2, prompt_id="p1", tool_name="Bash", tool_use_id="t1", tool_input={})
    pre["client_redactions"] = {"openai_key": 10**9, "free text: a secret": 1, "email": -3}

    client.post(URL, json={"events": [pre]})

    assert stored(store)[1]["tool:t1"].redactions == {"openai_key": 1_000_000}


def test_a_response_moves_to_its_turn_even_when_session_end_came_first(
    client: TestClient, store: Engine
) -> None:
    response = [usage("msg_1", input_tokens=10, output_tokens=2)]
    client.post(URL, json={"events": [ev("UserPromptSubmit", 1, prompt_id="p1", prompt="hi")]})
    client.post(URL, json={"events": [ev("SessionEnd", 3, usage=response)]})  # arrived first
    client.post(URL, json={"events": [ev("Stop", 2, prompt_id="p1", usage=response)]})

    spans = stored(store)[1]
    assert spans["llm:msg_1"].parent_id == spans["turn:p1"].id


def test_a_response_goes_under_the_turn_it_was_written_in_whichever_event_carries_it(
    client: TestClient, store: Engine
) -> None:
    """Later hooks read a long turn's rest and send it with that turn's end, so the next
    turn's responses can come with it, and the next Stop can carry the old turn's rest. The
    transcript says which turn each response belongs to (its prompt_id)."""
    client.post(
        URL,
        json={
            "events": [
                ev("UserPromptSubmit", 1, prompt_id="p1", prompt="first"),
                ev("UserPromptSubmit", 5, prompt_id="p2", prompt="second"),
                ev("Stop", 4, prompt_id="p1", usage=[usage("msg_2") | {"prompt_id": "p2"}]),
                ev("Stop", 8, prompt_id="p2", usage=[usage("msg_1") | {"prompt_id": "p1"}]),
                ev("Stop", 9, prompt_id="p2", usage=[usage("msg_3")]),  # an older hook's
            ]
        },
    )

    spans = stored(store)[1]
    assert spans["llm:msg_1"].parent_id == spans["turn:p1"].id
    assert spans["llm:msg_2"].parent_id == spans["turn:p2"].id
    assert spans["llm:msg_3"].parent_id == spans["turn:p2"].id


def test_the_rest_of_an_old_turn_end_adds_only_its_items(client: TestClient, store: Engine) -> None:
    """The rest of a long turn, read by later hooks, comes as more parts of its turn end
    (marked rest): it must not take the run back to that turn's permission mode."""
    turn = {"prompt_id": "p1", "permission_mode": "default"}
    client.post(
        URL,
        json={
            "events": [
                ev("UserPromptSubmit", 1, **turn, prompt="hi"),
                ev("Stop", 4, **turn, last_assistant_message="done"),
                ev("UserPromptSubmit", 6, prompt_id="p2", permission_mode="plan", prompt="go"),
            ]
        },
    )
    rest = ev("Stop", 4, **turn, last_assistant_message="done", usage=[usage("msg_9")])
    client.post(URL, json={"events": [rest | {"rest": True}]})

    [run], spans = stored(store)
    assert run.attributes["claude_code.permission_mode"] == "plan"
    assert spans["llm:msg_9"].parent_id == spans["turn:p1"].id


def test_a_response_runs_from_its_request_and_says_what_it_did(
    client: TestClient, store: Engine
) -> None:
    said = {"text": "Let me look.", "tool_calls": [{"id": "t1", "name": "Glob", "input": {}}]}
    item = usage("msg_1", input_tokens=1, output_tokens=1) | {
        "requested_at": "2026-11-11T09:00:00Z",
        "started_at": "2026-11-11T09:00:02Z",
        "timestamp": "2026-11-11T09:00:03Z",
        "stop_reason": "tool_use",
        "output": said,
    }
    client.post(URL, json={"events": [ev("Stop", 4, prompt_id="p1", usage=[item])]})

    llm = stored(store)[1]["llm:msg_1"]
    assert llm.ended_at is not None
    assert (llm.ended_at - llm.started_at).total_seconds() == 3  # request sent to last block
    assert llm.output == said
    assert llm.attributes["stop_reason"] == "tool_use"
    assert llm.attributes["first_output_at"] == "2026-11-11T09:00:02+00:00"


def test_the_session_span_reads_as_a_summary(client: TestClient, store: Engine) -> None:
    client.post(URL, json={"events": session_events()})
    client.post(
        URL,
        json={
            "events": [
                ev("UserPromptSubmit", 11, prompt_id="p2", prompt="and the tests?"),
                ev("Stop", 12, prompt_id="p2", last_assistant_message="There are 3 tests."),
            ]
        },
    )

    root = stored(store)[1]["session"]
    assert root.input == "Count the files in src\nthen stop"  # the first prompt
    assert root.output == "There are 3 tests."  # the latest reply


def test_a_subagent_without_a_type_is_named_by_its_id_and_keeps_a_known_name(
    client: TestClient, store: Engine
) -> None:
    client.post(URL, json={"events": [ev("SubagentStop", 2, agent_id="abcdef1234567")]})
    assert stored(store)[1]["agent:abcdef1234567"].name == "subagent abcdef12"

    client.post(URL, json={"events": [ev("SubagentStart", 1, agent_id="b1", agent_type="Plan")]})
    client.post(URL, json={"events": [ev("SubagentStop", 2, agent_id="b1")]})
    assert stored(store)[1]["agent:b1"].name == "Plan"


def test_a_span_whose_parent_never_arrived_gets_a_placeholder(
    client: TestClient, store: Engine
) -> None:
    """The turn's UserPromptSubmit was lost: the tool still hangs off the session."""
    pre = ev("PreToolUse", 2, prompt_id="lost", tool_name="Read", tool_use_id="t1", tool_input={})
    inside = ev(
        "PreToolUse", 3, prompt_id="lost", agent_id="a9", tool_name="Grep", tool_use_id="t2",
        tool_input={},
    )  # fmt: skip
    client.post(URL, json={"events": [pre, inside]})

    spans = stored(store)[1]
    turn, agent = spans["turn:lost"], spans["agent:a9"]
    assert (turn.kind, turn.parent_id) == ("step", spans["session"].id)
    assert spans["tool:t1"].parent_id == turn.id
    assert (agent.name, spans["tool:t2"].parent_id) == ("subagent a9", agent.id)

    client.post(URL, json={"events": [ev("UserPromptSubmit", 1, prompt_id="lost", prompt="hi")]})
    turn = stored(store)[1]["turn:lost"]
    assert turn.input == "hi" and turn.started_at.timestamp() == T0 + 1  # filled in later


def test_waiting_for_permission_is_a_span_under_its_tool(client: TestClient, store: Engine) -> None:
    tool = {"prompt_id": "p1", "tool_name": "Bash", "tool_input": {"command": "rm -r build"}}
    client.post(
        URL,
        json={
            "events": [
                ev("PreToolUse", 1, tool_use_id="t1", **tool),
                ev("PermissionRequest", 2, **tool),  # no tool_use_id
                ev("PostToolUse", 10, tool_use_id="t1", duration_ms=1_500, **tool),
            ]
        },
    )

    spans = stored(store)[1]
    [wait] = waits(spans)
    assert (wait.name, wait.parent_id, wait.status) == (
        "permission: Bash",
        spans["tool:t1"].id,
        "ok",
    )
    assert wait.ended_at is not None
    assert (wait.ended_at - wait.started_at).total_seconds() == 6.5  # until the tool began


BASH = {"prompt_id": "p1", "tool_name": "Bash", "tool_input": {"command": "rm -r build"}}


def waits(spans: dict[str, Span]) -> list[Span]:
    return sorted(
        (s for s in spans.values() if s.external_id.startswith("permission:")),
        key=lambda s: s.started_at,
    )


def seconds(span: Span) -> float | None:
    return (span.ended_at - span.started_at).total_seconds() if span.ended_at else None


@pytest.mark.parametrize(
    "order",
    [
        ["PreToolUse", "PermissionRequest", "PostToolUse"],
        ["PermissionRequest", "PreToolUse", "PostToolUse"],  # the request arrived first
        ["PreToolUse", "PostToolUse", "PermissionRequest"],  # the tool's end arrived first
        ["PostToolUse", "PermissionRequest", "PreToolUse"],
    ],
)
def test_a_permission_wait_ends_under_its_tool_whatever_the_arrival_order(
    client: TestClient, store: Engine, order: list[str]
) -> None:
    events = {
        "PreToolUse": ev("PreToolUse", 1, tool_use_id="t1", **BASH),
        "PermissionRequest": ev("PermissionRequest", 2, **BASH),
        "PostToolUse": ev("PostToolUse", 10, tool_use_id="t1", duration_ms=1_500, **BASH),
    }
    for name in order:
        assert client.post(URL, json={"events": [events[name]]}).json()["rejected"] == 0

    for _ in range(2):  # as it arrived, then after the request is re-sent
        spans = stored(store)[1]
        [wait] = waits(spans)  # one wait, not a duplicate
        assert (wait.parent_id, wait.status, seconds(wait)) == (spans["tool:t1"].id, "ok", 6.5)
        client.post(URL, json={"events": [events["PermissionRequest"]]})


@pytest.mark.parametrize("tool_input", ["rm -r build", 3, True, None], ids=str)
def test_a_tool_input_that_is_not_an_object_is_stored_not_retried_for_ever(
    client: TestClient, store: Engine, tool_input: Any
) -> None:
    """Postgres can't compare jsonb with text, a number or a bool (42883). That was an
    unexpected error, so a 503 for every retry, stalling the hook's backlog behind it."""
    call = {"prompt_id": "p1", "tool_name": "Bash", "tool_input": tool_input}
    events = [
        ev("PreToolUse", 1, tool_use_id="t1", **call),
        ev("PermissionRequest", 2, **call),
        ev("PermissionDenied", 3, tool_use_id="t1", **call),
        ev("PostToolUse", 4, tool_use_id="t1", **call),
    ]

    response = client.post(URL, json={"events": events})

    assert response.status_code == 200
    assert response.json()["rejected"] == 0
    spans = stored(store)[1]
    assert spans["tool:t1"].input == tool_input
    assert waits(spans)[0].parent_id == spans["tool:t1"].id


def test_a_fast_approval_never_ends_before_it_began(client: TestClient, store: Engine) -> None:
    """Each hook stamps its own time, a little late: the tool's run time can exceed the gap."""
    response = client.post(
        URL,
        json={
            "events": [
                ev("PreToolUse", 1, tool_use_id="t1", **BASH),
                ev("PermissionRequest", 2, **BASH),
                ev("PostToolUse", 3, tool_use_id="t1", duration_ms=1_500, **BASH),
            ]
        },
    )

    assert response.json()["rejected"] == 0
    spans = stored(store)[1]
    assert spans["tool:t1"].ended_at is not None
    assert seconds(waits(spans)[0]) == 0


def test_the_same_command_twice_gets_a_wait_each(client: TestClient, store: Engine) -> None:
    events = [
        ev("PreToolUse", 1, tool_use_id="t1", **BASH),
        ev("PermissionRequest", 2, **BASH),
        ev("PostToolUse", 3, tool_use_id="t1", **BASH),
        ev("PermissionRequest", 12, **BASH),  # before the second call's start arrives
        ev("PreToolUse", 11, tool_use_id="t2", **BASH),
        ev("PostToolUse", 15, tool_use_id="t2", **BASH),
    ]
    for event in events:  # one at a time, in this order
        client.post(URL, json={"events": [event]})

    spans = stored(store)[1]
    first, second = waits(spans)
    assert (first.parent_id, seconds(first)) == (spans["tool:t1"].id, 1)
    assert (second.parent_id, seconds(second)) == (spans["tool:t2"].id, 3)


def test_an_auto_mode_denial_closes_the_wait(client: TestClient, store: Engine) -> None:
    client.post(
        URL,
        json={
            "events": [
                ev("PreToolUse", 1, tool_use_id="t1", **BASH),
                ev("PermissionRequest", 2, **BASH),
                ev("PermissionDenied", 4, tool_use_id="t1", reason="outside the project", **BASH),
            ]
        },
    )

    [wait] = waits(stored(store)[1])
    assert (wait.status, wait.output, seconds(wait)) == (
        "rejected",
        "rejected by auto mode: outside the project",
        2,
    )


def test_a_response_read_in_two_parts_keeps_both(client: TestClient, store: Engine) -> None:
    """A queued prompt: one Stop read the first blocks, the next read the rest."""
    head = usage("msg_1", input_tokens=5, output_tokens=1) | {
        "requested_at": "2026-11-11T09:00:00Z",
        "started_at": "2026-11-11T09:00:01Z",
        "timestamp": "2026-11-11T09:00:02Z",
        "output": {"thinking": "Look first.", "tool_calls": [{"id": "t1", "name": "Glob"}]},
    }
    tail = usage("msg_1", input_tokens=5, output_tokens=9) | {
        "started_at": "2026-11-11T09:00:03Z",
        "timestamp": "2026-11-11T09:00:04Z",
        "output": {"text": "Found them.", "tool_calls": [{"id": "t2", "name": "Read"}]},
    }
    client.post(URL, json={"events": [ev("Stop", 3, prompt_id="p1", usage=[head])]})
    client.post(URL, json={"events": [ev("Stop", 5, prompt_id="p2", usage=[tail])]})
    client.post(URL, json={"events": [ev("Stop", 5, prompt_id="p2", usage=[tail])]})  # re-sent

    llm = stored(store)[1]["llm:msg_1"]
    assert seconds(llm) == 4  # request sent to the last block
    assert llm.attributes["first_output_at"] == "2026-11-11T09:00:01+00:00"
    assert llm.output == {
        "thinking": "Look first.",
        "text": "Found them.",
        "tool_calls": [{"id": "t1", "name": "Glob"}, {"id": "t2", "name": "Read"}],
    }
    assert llm.output_tokens == 9  # the last line's usage


def test_a_late_old_reply_does_not_replace_the_latest(client: TestClient, store: Engine) -> None:
    client.post(
        URL,
        json={
            "events": [
                ev("UserPromptSubmit", 5, prompt_id="p2", prompt="second"),
                ev("Stop", 6, prompt_id="p2", last_assistant_message="second reply"),
            ]
        },
    )
    client.post(
        URL,
        json={
            "events": [
                ev("UserPromptSubmit", 1, prompt_id="p1", prompt="first"),  # arrived late
                ev("Stop", 2, prompt_id="p1", last_assistant_message="first reply"),
            ]
        },
    )

    root = stored(store)[1]["session"]
    assert (root.input, root.output) == ("first", "second reply")


def test_a_compaction_whose_end_arrived_first_is_one_span(
    client: TestClient, store: Engine
) -> None:
    post = ev("PostCompact", 9, trigger="auto", compact_summary="summary")
    pre = ev("PreCompact", 5, trigger="auto")
    for event in (post, pre, pre, post):  # out of order, each re-sent
        client.post(URL, json={"events": [event]})

    [compaction] = [s for s in stored(store)[1].values() if s.external_id.startswith("compact:")]
    assert (seconds(compaction), compaction.output) == (4, "summary")


def test_placeholders_nest_and_start_with_their_earliest_child(
    client: TestClient, store: Engine
) -> None:
    def grep(at: float, tool_use_id: str) -> dict[str, Any]:
        return ev("PreToolUse", at, prompt_id="p9", agent_id="a9", tool_name="Grep",
                  tool_use_id=tool_use_id, tool_input={})  # fmt: skip

    client.post(URL, json={"events": [grep(5, "t1")]})
    client.post(URL, json={"events": [grep(3, "t2")]})  # an earlier child, arriving later

    spans = stored(store)[1]
    agent, turn = spans["agent:a9"], spans["turn:p9"]
    assert agent.parent_id == turn.id  # the subagent sits in its turn, not the session
    assert agent.started_at.timestamp() == turn.started_at.timestamp() == T0 + 3


def test_an_auto_mode_denial_fails_the_tool(client: TestClient, store: Engine) -> None:
    tool = {"prompt_id": "p1", "tool_name": "Bash", "tool_use_id": "t1", "tool_input": {}}
    client.post(
        URL,
        json={
            "events": [
                ev("PreToolUse", 1, **tool),
                ev("PermissionDenied", 2, reason="touches files outside the project", **tool),
            ]
        },
    )

    denied = stored(store)[1]["tool:t1"]
    assert (denied.status, denied.error) == (
        "rejected",
        "rejected by auto mode: touches files outside the project",
    )


def test_compaction_notifications_and_commands_are_part_of_the_story(
    client: TestClient, store: Engine
) -> None:
    events = [
        ev("UserPromptSubmit", 1, prompt_id="p1", prompt="Review the diff"),
        ev("UserPromptExpansion", 1.5, prompt_id="p1", command_name="review",
           command_args="--quick", prompt="Review the diff", command_source="project"),
        ev("Notification", 3, prompt_id="p1", notification_type="permission_prompt",
           message="Claude needs your permission to use Bash"),
        ev("PreCompact", 5, trigger="auto", custom_instructions=None),
        ev("PostCompact", 9, trigger="auto", compact_summary="We reviewed the diff."),
    ]  # fmt: skip
    client.post(URL, json={"events": events})
    client.post(URL, json={"events": events[-1:]})  # PostCompact re-sent: same span

    spans = stored(store)[1]
    turn, root = spans["turn:p1"], spans["session"]
    by_name = {s.name: s for s in spans.values()}
    command, note = by_name["command: /review"], by_name["notification: permission_prompt"]
    assert (command.parent_id, command.input) == (turn.id, "--quick")
    assert (note.parent_id, note.output) == (turn.id, "Claude needs your permission to use Bash")
    [compaction] = [s for s in spans.values() if s.external_id.startswith("compact:")]
    assert (compaction.name, compaction.parent_id) == ("compaction (auto)", root.id)
    assert compaction.output == "We reviewed the diff."
    assert compaction.ended_at is not None
    assert (compaction.ended_at - compaction.started_at).total_seconds() == 4


def decision(
    tool_use_id: str, made: str, source: str, reason_type: str | None = None, at: float = 5
) -> dict[str, Any]:
    when = datetime.fromtimestamp(T0 + at, tz=UTC).isoformat()
    return {"tool_use_id": tool_use_id, "decision": made, "source": source,
            "reason_type": reason_type, "reason_code": None, "at": when}  # fmt: skip


def one_by_one(client: TestClient, *events: dict[str, Any]) -> None:
    """Each event alone, in this order (a batch would be sorted by time)."""
    for event in events:
        assert client.post(URL, json={"events": [event]}).json()["rejected"] == 0


def stop_with(*decisions: dict[str, Any], at: float = 5) -> dict[str, Any]:
    return ev("Stop", at, prompt_id="p1", permissions=list(decisions))


def test_a_call_you_rejected_keeps_your_feedback_and_its_wait_is_rejected(
    client: TestClient, store: Engine
) -> None:
    failure = ev("PostToolUseFailure", 4, tool_use_id="t1", error="use git mv instead", **BASH)
    one_by_one(
        client,
        ev("PermissionRequest", 2, **BASH),  # before its tool's start
        ev("PreToolUse", 1, tool_use_id="t1", **BASH),
        stop_with(decision("t1", "reject", "user_reject")),  # before the failure
        failure,
        failure,  # re-sent: still rejected
    )

    spans = stored(store)[1]
    tool, [wait] = spans["tool:t1"], waits(spans)
    assert (tool.status, tool.error) == ("rejected", "rejected by you")
    assert (wait.status, wait.output, wait.parent_id) == ("rejected", "rejected by you", tool.id)


def test_your_feedback_stays_when_the_decision_comes_after_it(
    client: TestClient, store: Engine
) -> None:
    one_by_one(
        client,
        ev("PreToolUse", 1, tool_use_id="t1", **BASH),
        ev("PostToolUseFailure", 4, tool_use_id="t1", error="use git mv instead", **BASH),
        stop_with(decision("t1", "reject", "user_reject")),
    )

    tool = stored(store)[1]["tool:t1"]
    assert (tool.status, tool.error) == ("rejected", "rejected by you: use git mv instead")


@pytest.mark.parametrize("decision_first", [True, False])
def test_an_auto_mode_reason_survives_the_turn_end(
    client: TestClient, store: Engine, decision_first: bool
) -> None:
    denied = ev("PermissionDenied", 3, tool_use_id="t1", reason="outside the project", **BASH)
    stop = stop_with(decision("t1", "reject", "config", "classifier"))
    pre = ev("PreToolUse", 1, tool_use_id="t1", **BASH)
    one_by_one(client, pre, *((stop, denied) if decision_first else (denied, stop)))

    tool = stored(store)[1]["tool:t1"]
    assert (tool.status, tool.error) == ("rejected", "rejected by auto mode: outside the project")


def test_a_wait_whose_request_arrives_after_the_rejection_is_rejected(
    client: TestClient, store: Engine
) -> None:
    one_by_one(
        client,
        ev("PreToolUse", 1, tool_use_id="t1", **BASH),
        stop_with(decision("t1", "reject", "user_reject")),
        ev("PermissionRequest", 2, **BASH),  # arrived last
    )

    spans = stored(store)[1]
    [wait] = waits(spans)
    assert (wait.status, wait.parent_id) == ("rejected", spans["tool:t1"].id)


def test_an_allowed_calls_wait_ends_when_the_tool_starts(client: TestClient, store: Engine) -> None:
    one_by_one(
        client,
        ev("PreToolUse", 1, tool_use_id="t1", **BASH),
        ev("PermissionRequest", 2, **BASH),
        stop_with(decision("t1", "accept", "user_temporary"), at=9),  # before PostToolUse
        ev("PostToolUse", 10, tool_use_id="t1", duration_ms=5_000, **BASH),
    )

    spans = stored(store)[1]
    tool, [wait] = spans["tool:t1"], waits(spans)
    assert (tool.status, tool.attributes["permission"]["by"]) == ("ok", "you")
    assert (wait.status, seconds(wait)) == ("ok", 3)  # 2 s to 10 s - 5 s


def test_the_transcript_tells_of_a_call_whose_hook_events_were_lost(
    client: TestClient, store: Engine
) -> None:
    client.post(URL, json={"events": [stop_with(decision("t9", "reject", "user_reject"))]})

    spans = stored(store)[1]
    lost = spans["tool:t9"]
    assert (lost.status, lost.parent_id, lost.ended_at is not None) == (
        "rejected",
        spans["session"].id,
        True,
    )

    client.post(URL, json={"events": [ev("PreToolUse", 1, tool_use_id="t9", **BASH)]})
    found = stored(store)[1]["tool:t9"]
    assert (found.name, found.parent_id, found.status) == ("Bash", spans["turn:p1"].id, "rejected")


@pytest.mark.parametrize(
    ("source", "reason_type", "who"),
    [
        ("user_temporary", None, "you"),
        ("user_permanent", "rule", "you"),
        ("config", "classifier", "auto mode"),
        ("config", "rule", "a permission rule"),
        ("config", "mode", "the permission mode"),
        ("config", "safetyCheck", "a safety check"),
        ("config", "other", "Claude Code"),
        ("config", None, "Claude Code"),
    ],
)
def test_who_allowed_a_call_is_kept(
    client: TestClient, store: Engine, source: str, reason_type: str | None, who: str
) -> None:
    tool = {"prompt_id": "p1", "tool_name": "Read", "tool_input": {}}
    one_by_one(
        client,
        ev("PreToolUse", 1, tool_use_id="t1", **tool),
        ev("PostToolUse", 2, tool_use_id="t1", **tool),
        stop_with(decision("t1", "accept", source, reason_type)),
    )

    read = stored(store)[1]["tool:t1"]
    assert read.status == "ok"
    assert read.attributes["permission"] == {
        "decision": "accept", "by": who, "source": source, "reason_type": reason_type,
        "reason_code": None,
    }  # fmt: skip


# ---- Two clocks: hook stamps and transcript times -------------------------------------


def test_a_call_refused_before_its_hook_stamped_it_keeps_its_turn_end(
    client: TestClient, store: Engine
) -> None:
    """A deny rule refuses at once: the transcript's time comes before PreToolUse's own stamp,
    which its hook process takes ~100 ms late. The turn end must still be stored whole."""
    stop = ev(
        "Stop",
        5,
        prompt_id="p1",
        last_assistant_message="I was refused.",
        usage=[usage("msg_1", input_tokens=1000, output_tokens=50)],
        permissions=[decision("t1", "reject", "config", "rule", at=0.9)],
    )
    one_by_one(client, ev("PreToolUse", 1, tool_use_id="t1", **BASH), stop)

    spans = stored(store)[1]
    tool = spans["tool:t1"]
    assert (tool.status, tool.error) == ("rejected", "rejected by a permission rule")
    assert tool.ended_at == tool.started_at  # never before it
    assert "llm:msg_1" in spans and spans["turn:p1"].ended_at is not None


def test_a_tools_end_stamped_before_its_start_is_kept(client: TestClient, store: Engine) -> None:
    """Pre- and PostToolUse are stamped by two hook processes: a fast tool's end can carry the
    earlier time."""
    read = {"prompt_id": "p1", "tool_name": "Read", "tool_input": {}}
    one_by_one(
        client,
        ev("PreToolUse", 1.0, tool_use_id="t1", **read),
        ev("PostToolUse", 0.99, tool_use_id="t1", tool_response="text", **read),
    )

    tool = stored(store)[1]["tool:t1"]
    assert (tool.status, tool.output) == ("ok", "text")
    assert tool.ended_at is not None and tool.ended_at >= tool.started_at


@pytest.mark.parametrize("received_at", [1e20, -1e20, 10**400])
def test_a_time_out_of_range_does_not_sink_the_batch(
    client: TestClient, store: Engine, received_at: float
) -> None:
    odd = {"received_at": received_at, "hook": {"session_id": "s2", "hook_event_name": "Stop"}}

    response = client.post(URL, json={"events": [odd, ev("SessionStart", 0)]})

    assert response.status_code == 200  # the hook retries a 500 for ever
    assert {r.external_id for r in stored(store)[0]} >= {"sess-1"}


def test_an_out_of_range_time_in_the_transcript_is_unknown(
    client: TestClient, store: Engine
) -> None:
    stop = ev("Stop", 5, prompt_id="p1", usage=[usage("msg_1") | {"timestamp": 1e20}])

    assert client.post(URL, json={"events": [stop]}).json()["rejected"] == 0
    assert "llm:msg_1" in stored(store)[1]


def test_items_past_the_limit_are_reported_not_dropped_silently(
    client: TestClient, store: Engine
) -> None:
    many = [usage(f"msg_{n}") for n in range(1_500)]

    reply = client.post(URL, json={"events": [ev("Stop", 5, prompt_id="p1", usage=many)]}).json()

    assert reply["dropped_items"] == 500
    assert sum(1 for s in stored(store)[1] if s.startswith("llm:")) == 1_000
