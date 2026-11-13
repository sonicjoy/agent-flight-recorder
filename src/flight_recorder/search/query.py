"""Hybrid search: vector similarity and Postgres full-text, fused by reciprocal rank.

Each side ranks embedded texts (run summaries and root-cause failures) that pass the run
filter. Reciprocal rank fusion adds 1 / (K + rank) from each side, so a text both sides rank
well beats one only a single side found, without having to put a cosine similarity and a
text rank on one scale. Runs are then ranked by their best text: one line per run.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, NamedTuple

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from flight_recorder.search.embedder import Embedder
from flight_recorder.search.summary import MAX_FAILURES
from flight_recorder.store.models import KEYWORD_CONFIG, Embedding, Run, Span
from flight_recorder.store.read import RunFilter, RunSummary, find_runs, run_conditions

K = 60  # the usual RRF constant: damps the gap between first and tenth place
MIN_CANDIDATES = 50  # texts taken from each side before fusing...
CANDIDATES_PER_RESULT = MAX_FAILURES + 1  # ...or a run's most texts per run asked for

Matched = Literal["meaning", "words", "both"]


@dataclass(frozen=True)
class Hit:
    summary: RunSummary
    similarity: float
    """Cosine similarity of the run's best text to the query, from -1 to 1."""
    matched: Matched
    """Which side ranked that text: the meaning side ranks every candidate, so in a small
    store a word match shows as `both`."""
    span_name: str | None
    """The failed span, when its text was the best match; else None (the run summary)."""
    span_error: str | None


class _Best(NamedTuple):
    score: float
    id: uuid.UUID
    similarity: float
    span_name: str | None
    span_error: str | None


def fuse(rankings: Sequence[Sequence[uuid.UUID]]) -> dict[uuid.UUID, float]:
    """Reciprocal rank fusion: each ranking adds 1 / (K + rank) to every id in it."""
    scores: dict[uuid.UUID, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, 1):
            scores[item] = scores.get(item, 0.0) + 1 / (K + rank)
    return scores


def search(
    session: Session, embedder: Embedder, query: str, where: RunFilter, now: datetime
) -> list[Hit]:
    """The runs that best match `query`, best first, at most `where.limit`."""
    pool = max(MIN_CANDIDATES, CANDIDATES_PER_RESULT * where.limit)
    # Filters apply after the HNSW scan; keep scanning until enough rows pass them.
    session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    session.execute(text(f"SET LOCAL hnsw.ef_search = {min(max(pool, 40), 1000)}"))
    distance = Embedding.embedding.cosine_distance(embedder.query(query))
    words = func.websearch_to_tsquery(KEYWORD_CONFIG, query)
    rank = func.ts_rank_cd(Embedding.content_tsv, words)
    base = (
        select(Embedding.id)
        .join(Run, Run.id == Embedding.run_id)
        .where(Embedding.model == embedder.model, *run_conditions(where, now))
        .limit(pool)
    )
    by_meaning = list(session.scalars(base.order_by(distance)))
    by_words = list(
        session.scalars(
            # rank > 0: a query of only negations (`-timeout`) matches everything at rank 0
            base.where(Embedding.content_tsv.op("@@")(words), rank > 0).order_by(rank.desc())
        )
    )
    scores = fuse([by_meaning, by_words])
    if not scores:
        return []
    rows = session.execute(
        select(
            Embedding.id,
            Embedding.run_id,
            (1 - distance).label("similarity"),
            Span.name,
            Span.error,
        )
        .outerjoin(Span, Span.id == Embedding.span_id)
        .where(Embedding.id.in_(scores))
    ).all()
    best: dict[uuid.UUID, _Best] = {}
    for row in rows:
        if row.run_id not in best or scores[row.id] > best[row.run_id].score:
            best[row.run_id] = _Best(
                scores[row.id], row.id, float(row.similarity), row.name, row.error
            )
    # ties (one side each, same rank) go by run id, so a ranking is the same every time
    ranked = sorted(best.items(), key=lambda item: (-item[1].score, item[0]))[: where.limit]
    found = find_runs(session, RunFilter(limit=len(ranked)), now, ids=[i for i, _ in ranked])
    summaries = {s.run.id: s for s in found}
    meaning, by_word = set(by_meaning), set(by_words)
    hits: list[Hit] = []
    for run_id, top in ranked:
        text_id = top.id
        matched: Matched = (
            "both" if text_id in meaning and text_id in by_word
            else "words" if text_id in by_word
            else "meaning"
        )  # fmt: skip
        hits.append(
            Hit(
                summaries[run_id],
                top.similarity,
                matched,
                top.span_name,
                top.span_error,
            )
        )
    return hits
