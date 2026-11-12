"""`afr embed`: embed finished runs that are new or changed.

Each finished run gets a summary vector and one per root-cause failure. A run's documents
are rebuilt every time, but only texts that differ from what's stored are embedded again;
vectors for failures that are no longer there are removed. Vectors from other models are
left alone, so switching models never loses the old ones.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from flight_recorder.search.embedder import Embedder
from flight_recorder.search.summary import Document, documents
from flight_recorder.store.models import Embedding, Run
from flight_recorder.store.read import run_state


@dataclass
class EmbedStats:
    runs: int = 0
    """Finished runs that got at least one new or changed vector."""
    vectors: int = 0
    unchanged: int = 0
    """Finished runs whose texts were all already embedded."""
    removed: int = 0
    """Vectors of failures that are no longer in their run."""
    open_runs: int = 0
    embedded_run_ids: list[uuid.UUID] = field(default_factory=lambda: [])


def pending_runs(session: Session, model: str) -> int:
    """Finished runs with no summary vector from this model yet."""
    embedded = exists().where(
        Embedding.run_id == Run.id, Embedding.model == model, Embedding.kind == "run_summary"
    )
    return (
        session.scalar(
            select(func.count()).select_from(Run).where(Run.ended_at.is_not(None), ~embedded)
        )
        or 0
    )


def embed_runs(session: Session, embedder: Embedder) -> EmbedStats:
    """Embed every finished run's new or changed texts. Flushes, doesn't commit."""
    stats = EmbedStats(
        open_runs=session.scalar(
            select(func.count()).select_from(Run).where(Run.ended_at.is_(None))
        )
        or 0
    )
    now = datetime.now(UTC)
    runs = session.scalars(
        select(Run).where(Run.ended_at.is_not(None)).order_by(Run.started_at)
    ).all()
    for run in runs:
        docs = documents(session, run, run_state(run, now, now))
        stored = {
            e.span_id: e
            for e in session.scalars(
                select(Embedding).where(
                    Embedding.run_id == run.id, Embedding.model == embedder.model
                )
            )
        }
        changed = [
            d for d in docs if _span_id(d) not in stored or stored[_span_id(d)].content != d.content
        ]
        current = {_span_id(d) for d in docs}
        stale = [span_id for span_id in stored if span_id not in current]
        if stale:
            session.execute(
                delete(Embedding).where(
                    Embedding.run_id == run.id,
                    Embedding.model == embedder.model,
                    Embedding.span_id.in_([s for s in stale if s is not None]),
                )
            )
            stats.removed += len(stale)
        if not changed:
            stats.unchanged += 1
            continue
        vectors = embedder.documents([d.content for d in changed])
        for doc, vector in zip(changed, vectors, strict=True):
            _upsert(session, run.id, embedder.model, doc, vector)
        stats.runs += 1
        stats.vectors += len(changed)
        stats.embedded_run_ids.append(run.id)
    session.flush()
    return stats


def _span_id(doc: Document) -> uuid.UUID | None:
    return doc.span.id if doc.span is not None else None


def _upsert(
    session: Session, run_id: uuid.UUID, model: str, doc: Document, vector: list[float]
) -> None:
    values = {
        "run_id": run_id,
        "span_id": _span_id(doc),
        "kind": doc.kind,
        "model": model,
        "content": doc.content,
        "embedding": vector,
    }
    statement = insert(Embedding).values(**values)
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
