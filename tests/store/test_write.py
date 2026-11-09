"""record_run against a real Postgres: nothing sensitive is stored, and runs stay coherent."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fake_secrets import in_context, secrets_by_kind
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from flight_recorder.redact import Redactor
from flight_recorder.store.models import Run, Span
from flight_recorder.store.write import RunIn, SpanIn, record_run

pytestmark = pytest.mark.db

REDACTOR = Redactor(b"test-key-0123456789abcdef0123456789abcdef")
T0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
TABLES = ("runs", "spans", "embeddings", "labels", "eval_results")


def run_in(**overrides: Any) -> RunIn:
    return RunIn(**({"source": "otel", "external_id": "trace-1", "started_at": T0} | overrides))


def span_in(external_id: str, **overrides: Any) -> SpanIn:
    values = {"external_id": external_id, "kind": "step", "name": external_id, "started_at": T0}
    return SpanIn(**(values | overrides))


def database_text(session: Session) -> str:
    """Every row of every table, as text: what someone with a database dump would see."""
    rows = [
        session.scalar(text(f"SELECT coalesce(string_agg(t::text, ' '), '') FROM {table} t"))
        for table in TABLES
    ]
    return " ".join(r or "" for r in rows)


def test_planted_secrets_never_reach_the_database(session: Session) -> None:
    """The canary test: a fake of every kind, in every field that takes text."""
    canaries = secrets_by_kind()
    contextual = in_context()
    everything = " ".join(canaries.values()) + " " + " ".join(t for t, _ in contextual.values())
    secret_parts = [*canaries.values(), *(part for _, part in contextual.values())]

    run = run_in(
        agent=f"agent {canaries['email']}",
        name=f"Session about {canaries['openai_key']}",
        attributes={
            "service.name": "triager",
            "env": {"DATABASE_URL": contextual["connection_password"][0]},
        },
    )
    spans = [
        span_in("root", kind="agent", name="triage_agent"),
        span_in(
            "llm-1",
            parent_external_id="root",
            kind="llm",
            name=f"chat {canaries['phone']}",
            model="openai:gpt-5.4-mini",
            error=f"401 for key {canaries['anthropic_key']}",
            input_tokens=1000,
            output_tokens=65,
            input={"messages": [{"role": "user", "content": everything}]},
            output={"content": f"Use {canaries['github_token']}"},
            attributes={
                "gen_ai.input.messages": json.dumps([{"content": everything}]),  # a JSON string
                canaries["aws_access_key"]: "a secret used as a key",
                "events": [{"name": "exception", "message": canaries["private_key"]}],
            },
        ),
    ]

    record_run(session, run, spans, redactor=REDACTOR)
    session.flush()

    dump = database_text(session)
    leaked = [secret for secret in secret_parts if secret in dump]
    assert leaked == []
    assert "[REDACTED:" in dump
    stored_run = session.scalars(select(Run)).one()
    assert stored_run.redactions == {"email": 1, "openai_key": 1, "connection_password": 1}
    llm = session.scalars(select(Span).where(Span.external_id == "llm-1")).one()
    assert set(llm.redactions) >= set(canaries) | set(contextual)  # every kind was caught
    assert llm.model == "openai:gpt-5.4-mini"  # no false positive on the model name


def test_spans_are_priced_and_runs_totalled(session: Session) -> None:
    run_id = record_run(
        session,
        run_in(),
        [
            span_in(
                "a", kind="llm", model="openai:gpt-5.4-mini", input_tokens=1000, output_tokens=65
            ),
            span_in("b", kind="tool", name="search_issues"),
        ],
        redactor=REDACTOR,
    )
    session.expire_all()

    run = session.get_one(Run, run_id)
    priced = session.scalars(select(Span).where(Span.external_id == "a")).one()
    assert priced.cost_usd is not None and priced.cost_usd > 0
    assert (run.input_tokens, run.output_tokens, run.cost_usd) == (1000, 65, priced.cost_usd)


def test_unknown_model_makes_the_run_cost_unknown_not_zero(session: Session) -> None:
    run_id = record_run(
        session,
        run_in(),
        [
            span_in("a", kind="llm", model="openai:gpt-5.4-mini", input_tokens=10, output_tokens=5),
            span_in("b", kind="llm", model="acme:unpriced-1", input_tokens=10, output_tokens=5),
        ],
        redactor=REDACTOR,
    )
    session.expire_all()

    assert session.get_one(Run, run_id).cost_usd is None
    assert session.get_one(Run, run_id).input_tokens == 20


def test_resending_updates_instead_of_duplicating(session: Session) -> None:
    record_run(session, run_in(status="unset"), [span_in("a", status="unset")], redactor=REDACTOR)
    record_run(
        session,
        run_in(status="ok", ended_at=T0 + timedelta(seconds=7)),
        [span_in("a", status="ok")],
        redactor=REDACTOR,
    )
    session.expire_all()

    assert session.scalar(select(func.count()).select_from(Run)) == 1
    assert session.scalar(select(func.count()).select_from(Span)) == 1
    assert session.scalars(select(Run.status)).one() == "ok"
    assert session.scalars(select(Span.status)).one() == "ok"


def test_a_partial_batch_merges_into_the_run_instead_of_wiping_it(session: Session) -> None:
    """A later batch that knows less (no name, status unset) mustn't erase what's stored."""
    record_run(
        session,
        run_in(
            agent="triager",
            name="triage #42",
            status="ok",
            started_at=T0 + timedelta(seconds=2),
            ended_at=T0 + timedelta(seconds=9),
            attributes={"service.name": "triager", "owner": "a@example.com"},
        ),
        [span_in("root")],
        redactor=REDACTOR,
    )
    record_run(
        session,
        run_in(started_at=T0, attributes={"deployment": "local", "owner": "b@example.com"}),
        [span_in("late-child", parent_external_id="root")],
        redactor=REDACTOR,
    )
    session.expire_all()

    run = session.scalars(select(Run)).one()
    assert (run.agent, run.name, run.status) == ("triager", "triage #42", "ok")
    assert (run.started_at, run.ended_at) == (T0, T0 + timedelta(seconds=9))  # widest span
    assert run.attributes["service.name"] == "triager"  # kept
    assert run.attributes["deployment"] == "local"  # added
    assert run.attributes["owner"] == REDACTOR.placeholder("email", "b@example.com")  # newer
    assert run.redactions == {"email": 1}  # largest per batch, not a running sum


def test_a_later_status_replaces_an_earlier_one(session: Session) -> None:
    record_run(session, run_in(status="ok"), [], redactor=REDACTOR)
    record_run(session, run_in(status="error"), [], redactor=REDACTOR)
    record_run(session, run_in(), [], redactor=REDACTOR)  # unset: no news
    session.expire_all()

    assert session.scalars(select(Run.status)).one() == "error"


def test_child_arriving_before_its_parent_is_linked_later(session: Session) -> None:
    record_run(session, run_in(), [span_in("child", parent_external_id="root")], redactor=REDACTOR)
    session.expire_all()
    assert session.scalars(select(Span.parent_id)).one() is None  # parent not here yet

    record_run(session, run_in(), [span_in("root", kind="agent")], redactor=REDACTOR)
    session.expire_all()

    root = session.scalars(select(Span).where(Span.external_id == "root")).one()
    child = session.scalars(select(Span).where(Span.external_id == "child")).one()
    assert child.parent_id == root.id
