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
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from flight_recorder.search.embedder import Embedder
from flight_recorder.store.models import Embedding, Run, Span
from flight_recorder.store.read import RunFilter, RunSummary, find_runs, run_conditions

K = 60  # the usual RRF constant: damps the gap between first and tenth place
CANDIDATES = 50  # texts taken from each side before fusing
TEXT_SEARCH = "english"

Matched = Literal["meaning", "words", "both"]


@dataclass(frozen=True)
class Hit:
    summary: RunSummary
    score: float
    """The fused score of the run's best text: only for ordering."""
    similarity: float
    """Cosine similarity of that text to the query, from -1 to 1."""
    matched: Matched
    span_name: str | None
    """The failed span, when its text was the best match; else None (the run summary)."""
    span_error: str | None


def fuse(rankings: Sequence[Sequence[uuid.UUID]], k: int = K) -> dict[uuid.UUID, float]:
    """Reciprocal rank fusion: each ranking adds 1 / (k + rank) to every id in it."""
    scores: dict[uuid.UUID, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, 1):
            scores[item] = scores.get(item, 0.0) + 1 / (k + rank)
    return scores


def search(
    session: Session, embedder: Embedder, query: str, where: RunFilter, now: datetime
) -> list[Hit]:
    """The runs that best match `query`, best first, at most `where.limit`."""
    distance = Embedding.embedding.cosine_distance(embedder.query(query))
    words = func.websearch_to_tsquery(TEXT_SEARCH, query)
    base = (
        select(Embedding.id)
        .join(Run, Run.id == Embedding.run_id)
        .where(Embedding.model == embedder.model, *run_conditions(where, now))
        .limit(CANDIDATES)
    )
    by_meaning = list(session.scalars(base.order_by(distance)))
    by_words = list(
        session.scalars(
            base.where(Embedding.content_tsv.op("@@")(words)).order_by(
                func.ts_rank_cd(Embedding.content_tsv, words).desc()
            )
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
    best: dict[uuid.UUID, tuple[float, float, uuid.UUID, str | None, str | None]] = {}
    for row in rows:
        score = scores[row.id]
        if row.run_id not in best or score > best[row.run_id][0]:
            best[row.run_id] = (score, float(row.similarity), row.id, row.name, row.error)
    ranked = sorted(best.items(), key=lambda item: -item[1][0])[: where.limit]
    summaries = {
        s.run.id: s
        for s in find_runs(
            session, RunFilter(limit=len(ranked)), now, ids=[run_id for run_id, _ in ranked]
        )
    }
    meaning, words_found = set(by_meaning), set(by_words)
    hits: list[Hit] = []
    for run_id, (score, similarity, text_id, span_name, span_error) in ranked:
        in_meaning, in_words = text_id in meaning, text_id in words_found
        matched: Matched = "both" if in_meaning and in_words else "words" if in_words else "meaning"
        hits.append(Hit(summaries[run_id], score, similarity, matched, span_name, span_error))
    return hits
