"""The retrieval check (#14): how often search puts a query's right runs in its top k.

Each query names its right runs by trace id (the run's `external_id`, the same in every
database). The stored model is ranked by the real
`afr search`. Another model is ranked in memory: the same stored texts, embedded with it,
fused with the same keyword ranking, so a comparison needs no migration and writes nothing.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from flight_recorder.search.embedder import Embedder
from flight_recorder.search.query import by_words, candidates, fuse, pool_size, run_order, search
from flight_recorder.store.models import Embedding, Run
from flight_recorder.store.read import RunFilter, run_conditions

K = 5


@dataclass(frozen=True)
class Query:
    text: str
    runs: tuple[str, ...]
    """Trace ids of the runs a good search returns for it."""


def load_queries(path: Path) -> list[Query]:
    """A YAML list of `{query: ..., runs: [trace id, ...]}`."""
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path}: expected a list of queries")
    queries: list[Query] = []
    items: list[Any] = raw  # pyright: ignore[reportUnknownVariableType]
    for i, item in enumerate(items, 1):
        fields: dict[str, Any] = item if isinstance(item, dict) else {}  # pyright: ignore[reportUnknownVariableType]
        text, runs = fields.get("query"), fields.get("runs")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{path}: query {i} has no text")
        if not isinstance(runs, list) or not runs:
            raise ValueError(f"{path}: query {i} names no right runs")
        right: list[Any] = runs  # pyright: ignore[reportUnknownVariableType]
        queries.append(Query(text.strip(), tuple(str(r) for r in right)))
    return queries


def recall_at_k(ranked: Sequence[str], relevant: Collection[str], k: int = K) -> float:
    """The share of the right runs in the top k, out of as many as k can hold."""
    if not relevant:
        raise ValueError("a query needs at least one right run")
    right = set(relevant)
    return len(set(ranked[:k]) & right) / min(len(right), k)


def missing_runs(session: Session, queries: Sequence[Query]) -> list[str]:
    """Right runs named by the queries that aren't in the store."""
    wanted = {run for q in queries for run in q.runs}
    present = set(session.scalars(select(Run.external_id).where(Run.external_id.in_(wanted))))
    return sorted(wanted - present)


def unsearchable_runs(
    session: Session, queries: Sequence[Query], model: str, agent: str | None = None
) -> list[str]:
    """Right runs search can't return: no text embedded with `model`, or another agent's."""
    wanted = {run for q in queries for run in q.runs}
    query = (
        select(Run.external_id)
        .join(Embedding, Embedding.run_id == Run.id)
        .where(Run.external_id.in_(wanted), Embedding.model == model)
    )
    if agent:
        query = query.where(Run.agent == agent)
    return sorted(wanted - set(session.scalars(query)))


def stored_ranking(
    session: Session, embedder: Embedder, query: str, k: int = K, agent: str | None = None
) -> list[str]:
    """What `afr search` returns for the query, as trace ids, best first."""
    hits = search(session, embedder, query, RunFilter(agent=agent, limit=k), datetime.now(UTC))
    return [hit.summary.run.external_id for hit in hits]


def _unit(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


class MemoryIndex:
    """The stored texts of `stored_model`, embedded again with `embedder`, in memory."""

    def __init__(
        self, session: Session, *, stored_model: str, embedder: Embedder, agent: str | None = None
    ) -> None:
        self.session = session
        self.stored_model = stored_model
        self.embedder = embedder
        self.where = RunFilter(agent=agent)
        rows = session.execute(
            select(Embedding.id, Embedding.run_id, Embedding.content, Run.external_id)
            .join(Run, Run.id == Embedding.run_id)
            .where(Embedding.model == stored_model, *run_conditions(self.where, datetime.now(UTC)))
            .order_by(Embedding.id)
        ).all()
        self.ids: list[uuid.UUID] = [row.id for row in rows]
        self.run_of: dict[uuid.UUID, uuid.UUID] = {row.id: row.run_id for row in rows}
        self.trace_of: dict[uuid.UUID, str] = {row.run_id: row.external_id for row in rows}
        self.content: dict[uuid.UUID, str] = {row.id: row.content for row in rows}
        texts = [row.content for row in rows]
        self.vectors = [_unit(v) for v in embedder.documents(texts)] if rows else []

    def by_meaning(self, query: str, pool: int) -> list[uuid.UUID]:
        """Text ids by cosine similarity to the query, best first, at most `pool`."""
        wanted = _unit(self.embedder.query(query))
        similarity = {
            text_id: sum(a * b for a, b in zip(vector, wanted, strict=True))
            for text_id, vector in zip(self.ids, self.vectors, strict=True)
        }
        return sorted(self.ids, key=lambda i: -similarity[i])[:pool]

    def ranking(self, query: str, k: int = K) -> list[str]:
        """Trace ids, best first: cosine and keyword rankings fused as `afr search` does."""
        if not self.ids:
            return []
        pool = pool_size(k)
        by_meaning = self.by_meaning(query, pool)
        base = candidates(self.stored_model, self.where, datetime.now(UTC), pool)
        scores = fuse([by_meaning, by_words(self.session, base, query)])
        best: dict[uuid.UUID, float] = {}
        for text_id, score in scores.items():
            run_id = self.run_of[text_id]
            best[run_id] = max(best.get(run_id, 0.0), score)
        ranked = sorted(best, key=lambda run_id: run_order(run_id, best[run_id]))[:k]
        return [self.trace_of[run_id] for run_id in ranked]
