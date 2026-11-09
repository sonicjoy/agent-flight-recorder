"""The run store against a real Postgres: migrations, constraints, the span tree, vectors."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, create_engine, func, inspect, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from flight_recorder.cli import app
from flight_recorder.store.db import downgrade, upgrade
from flight_recorder.store.models import (
    EMBEDDING_DIMENSIONS,
    Base,
    Embedding,
    EvalResult,
    Label,
    Run,
    Span,
)

pytestmark = pytest.mark.db

T0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
TABLES = {"runs", "spans", "embeddings", "labels", "eval_results"}


def make_run(**overrides: Any) -> Run:
    values: dict[str, Any] = {
        "source": "otel",
        "external_id": uuid.uuid4().hex,
        "agent": "issue_triager",
        "started_at": T0,
    }
    return Run(**(values | overrides))


def make_span(run: Run, external_id: str, **overrides: Any) -> Span:
    values: dict[str, Any] = {
        "run": run,
        "external_id": external_id,
        "kind": "step",
        "name": external_id,
        "started_at": T0,
    }
    return Span(**(values | overrides))


def vector(*head: float) -> list[float]:
    return [*head, *([0.0] * (EMBEDDING_DIMENSIONS - len(head)))]


# ---- Migrations ------------------------------------------------------------------


def test_migrations_create_and_remove_everything(make_database: Callable[[], str]) -> None:
    url = make_database()
    engine = create_engine(url)
    try:
        upgrade(url)
        assert set(inspect(engine).get_table_names()) >= TABLES
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))

        downgrade(url)
        assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
        with engine.connect() as connection:
            assert not connection.scalar(text("SELECT 1 FROM pg_extension WHERE extname='vector'"))

        upgrade(url)  # and back up again
        assert set(inspect(engine).get_table_names()) >= TABLES
    finally:
        engine.dispose()


def test_models_match_the_migrations(engine: Engine) -> None:
    """Fails when a model changes without a migration (run `afr db revision`)."""
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)

    assert diff == []


def test_cli_upgrades_the_configured_database(
    make_database: Callable[[], str], clean_env: pytest.MonkeyPatch
) -> None:
    url = make_database()
    clean_env.setenv("AFR_DATABASE_URL", url)

    result = CliRunner().invoke(app, ["db", "upgrade"])

    assert result.exit_code == 0, result.output
    engine = create_engine(url)
    try:
        assert set(inspect(engine).get_table_names()) >= TABLES
    finally:
        engine.dispose()


# ---- Runs and spans --------------------------------------------------------------


def test_span_tree_round_trips(session: Session) -> None:
    run = make_run()
    root = make_span(run, "root", kind="agent", name="triage_agent")
    llm = make_span(
        run,
        "llm-1",
        kind="llm",
        parent=root,
        model="openai:gpt-5.4-mini",
        input_tokens=1000,
        output_tokens=65,
        cost_usd=Decimal("0.00103250"),
        input={"messages": [{"role": "user", "content": "Triage #19"}]},
        ended_at=T0 + timedelta(seconds=2),
    )
    make_span(run, "tool-1", kind="tool", parent=root, name="search_issues", status="error")
    session.add(run)
    session.commit()
    session.expire_all()

    loaded = session.scalars(select(Span).where(Span.external_id == "root")).one()
    assert sorted(child.kind for child in loaded.children) == ["llm", "tool"]
    stored = session.get_one(Span, llm.id)
    assert stored.parent_id == root.id
    assert stored.cost_usd == Decimal("0.00103250")  # exact, not a float
    assert stored.input == {"messages": [{"role": "user", "content": "Triage #19"}]}
    assert stored.redactions == {}
    assert stored.ended_at is not None
    assert stored.ended_at - stored.started_at == timedelta(seconds=2)


def test_deleting_a_run_deletes_its_spans_and_labels(session: Session) -> None:
    run = make_run()
    span = make_span(run, "root")
    session.add_all([run, Label(run=run, span=span, failure_mode="timeout", source="human")])
    session.commit()

    session.delete(run)
    session.commit()

    assert session.scalar(select(func.count()).select_from(Span)) == 0
    assert session.scalar(select(func.count()).select_from(Label)) == 0


def test_resent_run_updates_instead_of_duplicating(session: Session) -> None:
    """The ingest pattern: (source, external_id) identifies a run across re-sends."""
    statement = insert(Run).values(
        source="otel", external_id="trace-1", started_at=T0, status="unset"
    )
    upsert = statement.on_conflict_do_update(
        index_elements=["source", "external_id"], set_={"status": statement.excluded.status}
    )
    session.execute(upsert)
    session.execute(upsert.values(status="ok"))

    runs = session.scalars(select(Run).where(Run.external_id == "trace-1")).all()
    assert [r.status for r in runs] == ["ok"]


def test_same_external_id_from_different_sources_is_two_runs(session: Session) -> None:
    session.add_all(
        [make_run(external_id="abc"), make_run(external_id="abc", source="claude_code")]
    )
    session.commit()

    assert session.scalar(select(func.count()).select_from(Run)) == 2


@pytest.mark.parametrize(
    "build",
    [
        lambda: make_run(external_id="dup"),  # second run with the same source and id
        lambda: make_run(source="langsmith"),
        lambda: make_run(status="done"),
    ],
    ids=["duplicate-id", "unknown-source", "unknown-status"],
)
def test_run_constraints(session: Session, build: Callable[[], Run]) -> None:
    session.add(make_run(external_id="dup"))
    session.commit()

    session.add(build())
    with pytest.raises(IntegrityError):
        session.commit()


def test_span_kind_must_be_one_of_ours(session: Session) -> None:
    run = make_run()
    make_span(run, "s1", kind="CHAIN")  # OpenInference's word; ingest maps it to "step"
    session.add(run)

    with pytest.raises(IntegrityError, match="ck_spans_kind"):
        session.commit()


@pytest.mark.parametrize(
    ("change", "constraint"),
    [
        ({"input_tokens": -5}, "tokens"),
        ({"output_tokens": -1}, "tokens"),
        ({"cost_usd": Decimal("-0.01")}, "cost"),
        ({"ended_at": T0 - timedelta(hours=1)}, "ended_after_started"),
    ],
    ids=["negative-input-tokens", "negative-output-tokens", "negative-cost", "ends-before-start"],
)
@pytest.mark.parametrize("table", ["runs", "spans"])
def test_measurements_must_be_possible(
    session: Session, table: str, change: dict[str, Any], constraint: str
) -> None:
    if table == "runs":
        session.add(make_run(**change))
    else:
        run = make_run()
        make_span(run, "s1", **change)
        session.add(run)

    with pytest.raises(IntegrityError, match=f"ck_{table}_{constraint}"):
        session.commit()


def test_span_parent_must_be_in_the_same_run(session: Session) -> None:
    """Otherwise one run's replay could show another run's steps."""
    run_a, run_b = make_run(), make_run()
    root = make_span(run_a, "root")
    session.add_all([run_a, run_b])
    session.commit()

    session.add(make_span(run_b, "child", parent=root))
    with pytest.raises(IntegrityError, match="fk_spans_run_id_parent_id_spans"):
        session.commit()


@pytest.mark.parametrize("model", [Label, Embedding])
def test_label_and_embedding_span_must_be_in_the_same_run(
    session: Session, model: type[Label] | type[Embedding]
) -> None:
    run_a, run_b = make_run(), make_run()
    span_a = make_span(run_a, "root")
    session.add_all([run_a, run_b])
    session.commit()

    if model is Label:
        session.add(Label(run=run_b, span=span_a, failure_mode="timeout", source="human"))
    else:
        session.add(
            Embedding(run=run_b, span=span_a, kind="failed_span", model="m", content="x",
                      embedding=vector(1))
        )  # fmt: skip
    with pytest.raises(IntegrityError, match=f"fk_{model.__tablename__}_run_id_span_id_spans"):
        session.commit()


def test_every_foreign_key_is_indexed(engine: Engine) -> None:
    """Postgres doesn't index foreign keys itself; without one, deletes scan the table."""
    query = text("""
        SELECT c.conrelid::regclass::text || ' (' || c.conname || ')'
        FROM pg_constraint c
        WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace
          AND NOT EXISTS (
            SELECT 1 FROM pg_index i
            WHERE i.indrelid = c.conrelid
              AND (i.indkey::int2[])[0:cardinality(c.conkey) - 1] = c.conkey
          )
    """)
    with engine.connect() as connection:
        unindexed = connection.scalars(query).all()

    assert unindexed == []


def test_label_confidence_is_between_zero_and_one(session: Session) -> None:
    run = make_run()
    session.add_all([run, Label(run=run, failure_mode="x", source="judge", confidence=1.5)])

    with pytest.raises(IntegrityError, match="ck_labels_confidence"):
        session.commit()


def test_eval_result_outlives_its_recorded_run(session: Session) -> None:
    run = make_run()
    session.add(run)
    session.flush()
    result = EvalResult(
        suite_run_id=uuid.uuid4(), case_id="dup-3", passed=True, model="m", git_sha="abc",
        run_id=run.id,
    )  # fmt: skip
    session.add(result)
    session.commit()

    session.delete(run)
    session.commit()
    session.refresh(result)

    assert result.run_id is None


# ---- Embeddings ------------------------------------------------------------------


def test_nearest_embedding_by_cosine_distance(session: Session) -> None:
    run = make_run()
    session.add(run)
    for name, content, embedding in [
        ("a", "tool timed out", vector(1, 0)),
        ("b", "followed the injected note", vector(0, 1)),
    ]:
        span = make_span(run, name)
        session.add(
            Embedding(run=run, span=span, kind="failed_span", model="m", content=content,
                      embedding=embedding)
        )  # fmt: skip
    session.commit()

    query = vector(0.9, 0.1)
    nearest = session.scalars(
        select(Embedding).order_by(Embedding.embedding.cosine_distance(query)).limit(1)
    ).one()

    assert nearest.content == "tool timed out"


def test_embedding_must_have_the_model_dimensions(session: Session) -> None:
    run = make_run()
    session.add_all(
        [run, Embedding(run=run, kind="run_summary", model="m", content="x", embedding=[1.0, 0.0])]
    )

    with pytest.raises(DataError, match="dimensions"):
        session.commit()


def test_one_run_summary_per_model(session: Session) -> None:
    """span_id is null for run summaries; NULLS NOT DISTINCT still makes them unique."""
    run = make_run()
    session.add(run)
    for _ in range(2):
        session.add(
            Embedding(run=run, kind="run_summary", model="m", content="x", embedding=vector(1))
        )

    with pytest.raises(IntegrityError, match="uq_embeddings_run_id_span_id_model"):
        session.commit()
