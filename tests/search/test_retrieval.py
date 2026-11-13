# ruff: noqa: F811  (fixtures imported from test_search)
"""The retrieval check for the embedding model (#14): recall@k over hand-written queries.

Written from the #14 plan before the code (.review/14-label/plan.md): queries name their
right runs by trace id; the stored model is ranked by the real `afr search`; another model
is ranked in memory over the same texts, fused with the same keyword ranking, so comparing
needs no migration and writes nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_search import FakeEmbedder, OtherModel, cli, corpus, db, fake  # noqa: F401  (fixtures)

from flight_recorder.store.models import Embedding
from flight_recorder.store.read import RunFilter

pytestmark = pytest.mark.db

QUERIES = """\
- query: tool timed out while fetching
  runs: [timeout]
- query: rate limited by the provider, 429
  runs: [rate]
- query: labelled a bug
  runs: [fine, timeout]
"""


def test_recall_at_k_counts_right_runs_in_the_top_k() -> None:
    from flight_recorder.search.retrieval import recall_at_k

    assert recall_at_k(["a", "b", "c"], {"a", "x"}, k=2) == 0.5
    assert recall_at_k(["a", "b", "c"], {"c"}, k=2) == 0.0
    assert recall_at_k(["a", "b"], {"b"}, k=5) == 1.0
    many = {f"r{i}" for i in range(8)}
    assert recall_at_k(sorted(many), many, k=5) == 1.0  # at most k can be found
    with pytest.raises(ValueError, match="right run"):
        recall_at_k(["a"], set(), k=5)


def test_queries_load_from_yaml(tmp_path: Path) -> None:
    from flight_recorder.search.retrieval import load_queries

    path = tmp_path / "q.yaml"
    path.write_text(QUERIES, encoding="utf-8")

    queries = load_queries(path)

    assert [q.text for q in queries][:2] == [
        "tool timed out while fetching",
        "rate limited by the provider, 429",
    ]
    assert queries[2].runs == ("fine", "timeout")


@pytest.mark.parametrize(
    "bad",
    [
        "- query: x\n",
        "- runs: [a]\n",
        "- query: ''\n  runs: [a]\n",
        "- query: x\n  runs: []\n",
        "{}",
    ],
)
def test_a_query_needs_text_and_right_runs(tmp_path: Path, bad: str) -> None:
    from flight_recorder.search.retrieval import load_queries

    path = tmp_path / "q.yaml"
    path.write_text(bad, encoding="utf-8")

    with pytest.raises(ValueError):
        load_queries(path)


def test_the_stored_ranking_is_afr_searchs(db: Session, fake: FakeEmbedder, corpus: Any) -> None:
    from flight_recorder.search.query import search
    from flight_recorder.search.retrieval import stored_ranking

    now = datetime.now(UTC)
    for query in ("tool timed out while fetching", "429", "label a ui bug"):
        hits = search(db, fake, query, RunFilter(limit=5), now)
        assert stored_ranking(db, fake, query, k=5) == [h.summary.run.external_id for h in hits]


def test_ranking_in_memory_with_the_stored_model_agrees_with_afr_search(
    db: Session,
    fake: FakeEmbedder,
    corpus: Any,
) -> None:
    """The same best run. Not always the same order below it: the fake gives exactly 0 to
    texts sharing no word with the query, and pgvector orders exact ties arbitrarily (real
    embeddings practically never tie)."""
    from flight_recorder.search.retrieval import MemoryIndex, stored_ranking

    index = MemoryIndex(db, stored_model=fake.model, embedder=FakeEmbedder())
    for query in ("tool timed out while fetching", "HTTP 429", "label a ui bug", "release notes"):
        mine, afr = index.ranking(query, k=5), stored_ranking(db, fake, query, k=5)
        assert mine[0] == afr[0], query


def test_another_model_ranks_in_memory_and_writes_nothing(
    db: Session,
    fake: FakeEmbedder,
    corpus: Any,
) -> None:
    from flight_recorder.search.retrieval import MemoryIndex

    before = db.scalar(select(func.count()).select_from(Embedding))
    index = MemoryIndex(db, stored_model=fake.model, embedder=OtherModel())

    ranked = index.ranking("tool timed out while fetching", k=5)

    assert ranked[0] == "timeout"
    assert set(ranked) <= {"timeout", "rate", "fine"}
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Embedding)) == before
    assert db.scalar(select(func.count()).where(Embedding.model == OtherModel.model)) == 0


def test_the_check_command_reports_recall_per_query_and_on_average(
    cli: Callable[..., Any],
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    import flight_recorder.search.embedder as embedder

    clean_env.setattr(embedder, "named_embedder", lambda name: OtherModel())
    path = tmp_path / "q.yaml"
    path.write_text(QUERIES, encoding="utf-8")

    result = cli("search-check", str(path), "--compare", "another-fake")

    out = result.output
    assert result.exit_code == 0, out
    assert "fake-bag-of-words" in out and "another-fake" in out
    assert "tool timed out while fetching" in out
    assert "mean recall@5" in out
    row = next(line for line in out.splitlines() if "tool timed out" in line)
    assert "1.00" in row


def test_the_check_names_right_runs_that_dont_exist(
    cli: Callable[..., Any],
    tmp_path: Path,
) -> None:
    path = tmp_path / "q.yaml"
    path.write_text("- query: x\n  runs: [timeout, no-such-trace]\n", encoding="utf-8")

    result = cli("search-check", str(path))

    assert result.exit_code == 1
    assert "no-such-trace" in result.output


def test_the_check_only_reads(
    cli: Callable[..., Any],
    tmp_path: Path,
    db: Session,
) -> None:
    path = tmp_path / "q.yaml"
    path.write_text(QUERIES, encoding="utf-8")
    before = db.scalar(select(func.max(Embedding.updated_at)))

    assert cli("search-check", str(path)).exit_code == 0
    db.expire_all()
    assert db.scalar(select(func.max(Embedding.updated_at))) == before


def test_a_named_model_is_built_lazily() -> None:
    """Building it doesn't download anything; only embedding does."""
    from flight_recorder.search.embedder import named_embedder

    model = named_embedder("BAAI/bge-base-en-v1.5")

    assert model.model == "BAAI/bge-base-en-v1.5"


# --- review round 1: tests written before the fixes ----------------------------------


def test_a_right_run_named_twice_counts_once() -> None:
    from flight_recorder.search.retrieval import recall_at_k

    assert recall_at_k(["a", "b"], ("a", "a"), k=5) == 1.0


class ScaledFake(FakeEmbedder):
    """The fake's vectors, not unit length: texts about timeouts are tiny, the rest huge, so
    a ranking by dot product puts the timeout last and one by cosine puts it first."""

    model = "scaled-fake"

    def documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = super().documents(texts)
        return [
            [v * (0.001 if "timed" in t else 1000.0) for v in vector]
            for t, vector in zip(texts, vectors, strict=True)
        ]

    def query(self, value: str) -> list[float]:
        return [v * 7 for v in super().query(value)]


def test_the_memory_ranking_is_by_cosine_not_length(
    db: Session, fake: FakeEmbedder, corpus: Any
) -> None:
    from flight_recorder.search.retrieval import MemoryIndex

    plain = MemoryIndex(db, stored_model=fake.model, embedder=FakeEmbedder())
    scaled = MemoryIndex(db, stored_model=fake.model, embedder=ScaledFake())

    # words from every run too, so the huge texts aren't at 0 and win a dot product
    query = "tool timed out while fetching the page, release notes, backlog, 429, ui bug"
    best = scaled.by_meaning(query, pool=50)[0]

    assert "timed" in scaled.content[best]
    assert best == plain.by_meaning(query, pool=50)[0]


def test_the_check_warns_about_right_runs_search_cant_return(
    cli: Callable[..., Any],
    tmp_path: Path,
    db: Session,
) -> None:
    from test_search import REDACTOR, T0

    from flight_recorder.store.write import RunIn, record_run

    record_run(db, RunIn(source="otel", external_id="never-embedded", name="x", status="ok",
                         started_at=T0, ended_at=T0), [], redactor=REDACTOR)  # fmt: skip
    db.commit()
    path = tmp_path / "q.yaml"
    path.write_text("- query: x\n  runs: [timeout, never-embedded]\n", encoding="utf-8")

    result = cli("search-check", str(path))

    assert result.exit_code == 0, result.output
    assert "never-embedded" in result.output and "not embedded" in result.output
