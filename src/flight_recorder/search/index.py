"""`afr embed`: embed settled runs that are new or changed.

A run is settled once it has finished, or has had no end and no change for 30 minutes
(unfinished: a crash or a lost end event, often the most interesting failures). Each gets a
summary vector and one per root-cause failure, up to MAX_FAILURES. A run's texts are rebuilt
every time, but only those that differ from what's stored are embedded again; vectors for
failures that are gone are removed. Vectors from other models are left alone, so switching
models never loses the old ones.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from flight_recorder.search.embedder import Embedder
from flight_recorder.search.summary import MAX_FAILURES, Document, documents
from flight_recorder.store.models import Embedding, Run
from flight_recorder.store.read import last_change, run_state, settled

__all__ = ["MAX_FAILURES", "EmbedStats", "embed_runs", "has_vectors", "pending_runs"]


@dataclass
class EmbedStats:
    runs: int = 0
    """Settled runs that got at least one new or changed vector."""
    vectors: int = 0
    unchanged: int = 0
    """Settled runs whose texts were all already embedded."""
    removed: int = 0
    """Vectors of failures that are no longer in their run."""
    open_runs: int = 0
    """Runs still going: left until they settle."""


def pending_runs(session: Session, model: str, now: datetime | None = None) -> int:
    """Settled runs with no summary vector from this model yet."""
    embedded = exists().where(
        Embedding.run_id == Run.id, Embedding.model == model, Embedding.kind == "run_summary"
    )
    when = now or datetime.now(UTC)
    count = select(func.count()).select_from(Run).where(settled(when), ~embedded)
    return session.scalar(count) or 0


def has_vectors(session: Session, model: str) -> bool:
    """Whether anything has been embedded with this model."""
    return bool(session.scalar(select(exists().where(Embedding.model == model))))


def embed_runs(
    session: Session,
    embedder: Embedder,
    *,
    commit_every: int | None = None,
    commit: Callable[[], None] | None = None,
) -> EmbedStats:
    """Embed every settled run's new or changed texts. Calls `commit` after each
    `commit_every` runs, so an interrupted job keeps its progress; otherwise only flushes."""
    now = datetime.now(UTC)
    rows = session.execute(
        select(Run, last_change().label("last_change"))
        .where(settled(now))
        .order_by(Run.started_at, Run.id)
    ).all()
    total = session.scalar(select(func.count()).select_from(Run)) or 0
    stats = EmbedStats(open_runs=total - len(rows))
    for done, row in enumerate(rows, 1):
        _embed_run(session, embedder, row.Run, run_state(row.Run, row.last_change, now), stats)
        if commit is not None and commit_every and done % commit_every == 0:
            commit()
    session.flush()
    return stats


def _embed_run(
    session: Session, embedder: Embedder, run: Run, state: str, stats: EmbedStats
) -> None:
    docs = documents(session, run, state)
    stored = {
        e.span_id: e.content
        for e in session.scalars(
            select(Embedding).where(Embedding.run_id == run.id, Embedding.model == embedder.model)
        )
    }
    stale = set(stored) - {d.span_id for d in docs}  # the summary (None) is always current
    if stale:
        session.execute(
            delete(Embedding).where(
                Embedding.run_id == run.id,
                Embedding.model == embedder.model,
                Embedding.span_id.in_(stale),
            )
        )
        stats.removed += len(stale)
    changed = [d for d in docs if stored.get(d.span_id) != d.content]
    if not changed:
        if not stale:
            stats.unchanged += 1
        return
    vectors = embedder.documents([d.content for d in changed])
    for doc, vector in zip(changed, vectors, strict=True):
        _upsert(session, run.id, embedder.model, doc, vector)
    stats.runs += 1
    stats.vectors += len(changed)


def _upsert(
    session: Session, run_id: object, model: str, doc: Document, vector: list[float]
) -> None:
    statement = insert(Embedding).values(
        run_id=run_id,
        span_id=doc.span_id,
        kind=doc.kind,
        model=model,
        content=doc.content,
        embedding=vector,
    )
    session.execute(
        statement.on_conflict_do_update(
            constraint="uq_embeddings_run_id_span_id_model",
            set_={
                "kind": statement.excluded.kind,
                "content": statement.excluded.content,
                "embedding": statement.excluded.embedding,
            },
        )
    )
