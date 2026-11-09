"""Only store/write.py writes runs and spans, so nothing can skip redaction."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).parents[1] / "src/flight_recorder"
ALLOWED = {SRC / "store/write.py", SRC / "store/models.py"}
WRITES = re.compile(
    r"insert\(\s*(Run|Span)\b"  # SQLAlchemy insert(Run)
    r"|\b(Run|Span)\("  # Run(...) / Span(...) objects
    r"|(?i:\b(insert\s+into|update|delete\s+from|copy)\s+(runs|spans)\b)",  # raw SQL
)


def writes_runs_or_spans(source: str) -> list[str]:
    return [m.group(0) for m in WRITES.finditer(source)]


def test_only_the_write_path_writes_runs_and_spans() -> None:
    offenders = {
        str(path.relative_to(SRC)): found
        for path in SRC.rglob("*.py")
        if path not in ALLOWED
        and "migrations" not in path.parts
        and (found := writes_runs_or_spans(path.read_text(encoding="utf-8")))
    }

    assert offenders == {}, "write runs and spans through store.write.record_run (it redacts)"


def test_the_check_catches_each_kind_of_write() -> None:
    assert writes_runs_or_spans("session.execute(insert(Span).values(name=x))")
    assert writes_runs_or_spans("session.add(Run(source='otel'))")
    assert writes_runs_or_spans('connection.execute(text("INSERT INTO runs VALUES (1)"))')
    assert writes_runs_or_spans('text("update spans set name = :n")')
    assert not writes_runs_or_spans("select(Run).where(Run.id == run_id)")
