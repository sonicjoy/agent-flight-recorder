"""The analyst's selection of failed runs (#22, docs/designs/analyst.md §1)."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from flight_recorder.redact import Redactor
from flight_recorder.store.db import upgrade
from flight_recorder.store.labels import add_label
from flight_recorder.store.models import Label
from flight_recorder.store.write import RunIn, SpanIn, record_run

pytestmark = pytest.mark.db

REDACTOR = Redactor(b"test-key-0123456789abcdef0123456789abcdef")
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
NOW = T0 + timedelta(days=1)
AGENT = "issue-triager"


def _record(
    session: Session,
    external_id: str,
    *,
    status: Literal["ok", "error"] = "ok",
    agent: str = AGENT,
    source: Literal["otel", "claude_code"] = "otel",
    attributes: dict[str, Any] | None = None,
) -> uuid.UUID:
    span = SpanIn(external_id=f"root-{external_id}", kind="agent", name="agent", started_at=T0,
                  ended_at=T0 + timedelta(seconds=5), status=status,
                  error="boom" if status == "error" else None)  # fmt: skip
    return record_run(
        session,
        RunIn(
            source=source,
            external_id=external_id,
            agent=agent,
            name=external_id,
            status=status,
            started_at=T0,
            ended_at=T0 + timedelta(seconds=5),
            attributes=attributes or {},
        ),
        [span],
        redactor=REDACTOR,
    )


@pytest.fixture(scope="module")
def store(make_database: Callable[[], str]) -> Generator[tuple[str, dict[str, uuid.UUID]]]:
    url = make_database()
    upgrade(url)
    engine = create_engine(url)
    with Session(engine) as session, session.begin():
        ids = {
            "errored": _record(session, "errored", status="error"),
            "wrong": _record(session, "wrong", attributes={"triager.grade": "wrong"}),
            "both": _record(session, "both", status="error",
                            attributes={"triager.grade": "wrong"}),
            "right": _record(session, "right", attributes={"triager.grade": "correct"}),
            "other_agent": _record(session, "other", status="error", agent="someone-else"),
            "session": _record(session, "session", status="error", agent=AGENT,
                               source="claude_code"),
            "bad_truth": _record(session, "bad-truth", attributes={"triager.grade": "wrong"}),
        }  # fmt: skip
    yield url, ids
    engine.dispose()


@pytest.fixture
def db(store: tuple[str, dict[str, uuid.UUID]]) -> Generator[Session]:
    engine = create_engine(store[0])
    with Session(engine) as session:
        session.execute(delete(Label))
        session.commit()
        yield session
    engine.dispose()


@pytest.fixture
def ids(store: tuple[str, dict[str, uuid.UUID]], db: Session) -> dict[str, uuid.UUID]:
    return store[1]


def _names(found: Any) -> dict[str, set[str]]:
    return {f.run.run.external_id: set(f.reasons) for f in found.failed}


def test_error_runs_and_runs_matching_every_attr_are_failed_with_their_reasons(
    db: Session,
) -> None:
    from flight_recorder.analyst.select import select_failed

    found = select_failed(db, AGENT, {"triager.grade": ("wrong",)}, NOW)

    assert _names(found) == {
        "errored": {"error"},
        "wrong": {"attr"},
        "both": {"error", "attr"},
        "bad-truth": {"attr"},
    }
    assert found.counts == {"error": 2, "attr": 3}
    assert found.vetoed == 0


def test_without_attr_only_error_runs_are_failed(db: Session) -> None:
    from flight_recorder.analyst.select import select_failed

    assert set(_names(select_failed(db, AGENT, {}, NOW))) == {"errored", "both"}


def test_another_agents_runs_and_claude_code_sessions_are_never_failed(db: Session) -> None:
    from flight_recorder.analyst.select import select_failed

    names = set(_names(select_failed(db, AGENT, {"triager.grade": ("wrong",)}, NOW)))

    assert "other" not in names
    assert "session" not in names
    assert "right" not in names


def test_a_run_whose_truth_a_human_marked_bad_is_vetoed_in_every_branch(
    db: Session, ids: dict[str, uuid.UUID]
) -> None:
    from flight_recorder.analyst.select import select_failed

    add_label(db, ids["bad_truth"], "bad-real-truth", None, author="t", redactor=REDACTOR)
    add_label(db, ids["both"], "Debatable Truth", None, author="t", redactor=REDACTOR)
    db.commit()

    found = select_failed(db, AGENT, {"triager.grade": ("wrong",)}, NOW)

    assert set(_names(found)) == {"errored", "wrong"}
    assert found.vetoed == 2


def test_selection_is_not_cut_at_the_default_page_size(store: Any, db: Session) -> None:
    from flight_recorder.analyst.select import select_failed

    engine = create_engine(store[0])
    with Session(engine) as session, session.begin():
        for n in range(25):  # more than RunFilter's default limit of 20
            _record(session, f"many-{n}", status="error", agent="many")
    engine.dispose()

    assert len(select_failed(db, "many", {}, NOW).failed) == 25
