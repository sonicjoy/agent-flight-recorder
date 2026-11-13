"""Schema changes must never lose data: rows survive every migration, upgrades don't drop
anything without a reviewed reason, updated_at stays honest, and downgrades need --yes."""

from __future__ import annotations

import ast
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from flight_recorder.cli import app
from flight_recorder.store.db import alembic_config, downgrade, upgrade
from flight_recorder.store.models import EMBEDDING_DIMENSIONS, Run

pytestmark = pytest.mark.db

VERSIONS = Path(__file__).parents[2] / "src/flight_recorder/store/migrations/versions"
TABLES = ("runs", "spans", "embeddings", "labels", "eval_results")
VECTOR = "[" + ",".join(["1"] + ["0"] * (EMBEDDING_DIMENSIONS - 1)) + "]"

# Written against revision 0001's columns and never changed: a later migration that can't
# cope with existing rows (say, a NOT NULL column with no default) fails this test.
SEED_AT_0001 = f"""
INSERT INTO runs (id, source, external_id, started_at)
VALUES ('00000000-0000-0000-0000-000000000001', 'otel', 'seed-run', '2026-11-06 17:00+00');
INSERT INTO spans (id, run_id, external_id, kind, name, started_at)
VALUES ('00000000-0000-0000-0000-000000000011', '00000000-0000-0000-0000-000000000001',
        'root', 'agent', 'root', '2026-11-06 17:00+00');
INSERT INTO spans (id, run_id, external_id, parent_id, kind, name, started_at)
VALUES ('00000000-0000-0000-0000-000000000012', '00000000-0000-0000-0000-000000000001',
        'child', '00000000-0000-0000-0000-000000000011', 'llm', 'chat', '2026-11-06 17:00+00');
INSERT INTO embeddings (run_id, span_id, kind, model, content, embedding)
VALUES ('00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000012',
        'failed_span', 'm', 'seed', '{VECTOR}');
INSERT INTO labels (run_id, failure_mode, source)
VALUES ('00000000-0000-0000-0000-000000000001', 'none', 'human');
INSERT INTO eval_results (suite_run_id, case_id, passed, model, git_sha, run_id)
VALUES (gen_random_uuid(), 'seed', true, 'm', 'abc', '00000000-0000-0000-0000-000000000001');
"""


def _counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {t: connection.scalar(text(f"SELECT count(*) FROM {t}")) or 0 for t in TABLES}


def test_existing_rows_survive_every_migration(make_database: Callable[[], str]) -> None:
    url = make_database()
    script = ScriptDirectory.from_config(alembic_config(url))
    revisions = [r.revision for r in reversed(list(script.walk_revisions()))]  # oldest first
    assert revisions[0] == "0001"

    upgrade(url, "0001")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text(SEED_AT_0001))
        seeded = _counts(engine)
        assert all(seeded.values())

        for revision in revisions[1:]:  # one step at a time, checking after each
            upgrade(url, revision)
            assert _counts(engine) == seeded, f"rows lost by migration {revision}"
        with engine.connect() as connection:
            parent = connection.scalar(
                text("SELECT parent_id FROM spans WHERE external_id='child'")
            )
        assert str(parent) == "00000000-0000-0000-0000-000000000011"
    finally:
        engine.dispose()


def test_rejected_spans_become_errors_when_0005_is_undone(
    make_database: Callable[[], str],
) -> None:
    url = make_database()
    upgrade(url)
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text(SEED_AT_0001))
            connection.execute(
                text("UPDATE spans SET status = 'rejected' WHERE external_id='child'")
            )
        downgrade(url, "0004")
        with engine.connect() as connection:
            status = connection.scalar(text("SELECT status FROM spans WHERE external_id='child'"))
        assert status == "error"
    finally:
        engine.dispose()


# ---- Upgrades never drop data without a reviewed reason -------------------------------

DESTRUCTIVE_OPS = {"drop_table", "drop_column"}
DESTRUCTIVE_SQL = ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM")
MARKER = "allow-data-loss:"


def destructive_steps(source: str) -> list[str]:
    """Data-losing steps in a migration's upgrade(), unless it says why with MARKER."""
    tree = ast.parse(source)
    upgrade_fn = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    if MARKER in (ast.get_source_segment(source, upgrade_fn) or ""):
        return []
    found: list[str] = []
    for node in ast.walk(upgrade_fn):
        func = node.func if isinstance(node, ast.Call) else None
        if isinstance(func, ast.Attribute) and func.attr in DESTRUCTIVE_OPS:
            found.append(f"op.{func.attr}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found += [sql for sql in DESTRUCTIVE_SQL if sql in node.value.upper()]
    return found


@pytest.mark.parametrize("path", sorted(VERSIONS.glob("*.py")), ids=lambda p: p.stem)
def test_upgrades_never_drop_data_without_a_reason(path: Path) -> None:
    steps = destructive_steps(path.read_text(encoding="utf-8"))

    assert not steps, (
        f"{path.name} upgrade() would lose data: {steps}. Prefer an additive change; if the "
        f"loss is intended, say why in a comment starting '# {MARKER}'"
    )


def test_destructive_step_check_catches_drops() -> None:
    drop = "def upgrade():\n    op.drop_column('runs', 'agent')\n"
    raw = "def upgrade():\n    op.execute('delete from runs')\n"
    allowed = (
        "def upgrade():\n"
        "    # allow-data-loss: column never written\n"
        "    op.drop_column('runs', 'x')\n"
    )
    downgrade_only = "def upgrade():\n    pass\n\ndef downgrade():\n    op.drop_table('runs')\n"

    assert destructive_steps(drop) == ["op.drop_column"]
    assert destructive_steps(raw) == ["DELETE FROM"]
    assert destructive_steps(allowed) == []
    assert destructive_steps(downgrade_only) == []


# ---- updated_at ----------------------------------------------------------------------


def _updated_at(session: Session, external_id: str) -> datetime:
    session.expire_all()
    return session.scalars(select(Run.updated_at).where(Run.external_id == external_id)).one()


def test_updated_at_moves_only_on_real_changes(session: Session) -> None:
    """Through the ingest pattern, INSERT ... ON CONFLICT DO UPDATE."""
    t0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)

    def resend(status: str) -> None:
        statement = insert(Run).values(
            source="otel", external_id="trace-1", started_at=t0, status=status
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=["source", "external_id"],
                set_={"status": statement.excluded.status},
            )
        )

    resend("unset")
    first = _updated_at(session, "trace-1")
    resend("unset")  # identical re-send
    assert _updated_at(session, "trace-1") == first
    resend("ok")  # a real change
    assert _updated_at(session, "trace-1") > first


def test_every_table_with_updated_at_has_the_trigger(engine: Engine) -> None:
    query = text("""
        SELECT c.table_name FROM information_schema.columns c
        WHERE c.table_schema = 'public' AND c.column_name = 'updated_at'
          AND NOT EXISTS (
            SELECT 1 FROM information_schema.triggers t
            WHERE t.event_object_table = c.table_name AND t.action_statement LIKE '%set_updated_at%'
          )
    """)
    with engine.connect() as connection:
        missing = connection.scalars(query).all()

    assert missing == []


# ---- Downgrades -----------------------------------------------------------------------


def test_downgrade_needs_yes(
    make_database: Callable[[], str], clean_env: pytest.MonkeyPatch
) -> None:
    url = make_database()
    upgrade(url)
    clean_env.setenv("AFR_DATABASE_URL", url)

    refused = CliRunner().invoke(app, ["db", "downgrade", "base"])

    assert refused.exit_code == 1
    assert "re-run with --yes" in refused.output
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM runs")) == 0  # table still there
    finally:
        engine.dispose()


def test_0007_merges_duplicate_human_labels_keeping_their_notes(
    make_database: Callable[[], str],
) -> None:
    url = make_database()
    upgrade(url, "0006")
    engine = create_engine(url)
    run = "'00000000-0000-0000-0000-000000000001'"
    try:
        with engine.begin() as connection:
            connection.execute(text(SEED_AT_0001))  # one human 'none' label, no note
            connection.execute(text(f"""
                INSERT INTO labels (run_id, failure_mode, source, note, created_at) VALUES
                ({run}, 'none', 'human', 'second', '2026-11-06 18:00+00'),
                ({run}, 'none', 'human', NULL, '2026-11-06 19:00+00'),
                ({run}, 'none', 'human', 'fourth', '2026-11-06 20:00+00'),
                ({run}, 'other', 'human', NULL, '2026-11-06 18:00+00'),
                ({run}, 'none', 'judge', 'j1', '2026-11-06 18:00+00'),
                ({run}, 'none', 'judge', 'j2', '2026-11-06 18:00+00')
            """))  # fmt: skip
        upgrade(url, "0007")
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT failure_mode, source, note FROM labels "
                    "ORDER BY source, failure_mode, note"
                )
            ).all()
    finally:
        engine.dispose()

    # the earliest 'none' (18:00, "second") is kept; the seed row's created_at is now()
    assert [tuple(r) for r in rows] == [
        ("none", "human", "second; [duplicate] fourth"),
        ("other", "human", None),
        ("none", "judge", "j1"),  # model labels may repeat
        ("none", "judge", "j2"),
    ]
