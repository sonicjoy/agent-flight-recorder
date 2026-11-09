"""The run store: one schema for every source of agent runs.

    runs ─┬─ spans (a tree: parent_id)
          ├─ embeddings   run summaries and failed spans, for semantic search
          ├─ labels       failure modes, from a human, the LLM judge or the analyst
          └─ eval_results one row per eval case per eval run

Modelled on the OpenTelemetry GenAI conventions (see docs/schema.md for the mapping).
Every row has our own UUID; the id the source sent (OTel trace/span id, Claude Code
session or tool-use id) is kept in `external_id`, unique per source, so re-sent data
updates a row instead of duplicating it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector  # pyright: ignore[reportMissingTypeStubs]
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIMENSIONS = 384  # bge-small (#11)

SOURCES = ("otel", "claude_code")
STATUSES = ("unset", "ok", "error")  # OTel span status codes
SPAN_KINDS = ("agent", "llm", "tool", "step", "other")
EMBEDDING_KINDS = ("run_summary", "failed_span")
LABEL_SOURCES = ("human", "judge", "analyst")

# Stable constraint names, so migrations can refer to them.
NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _json() -> Mapped[dict[str, Any]]:
    return mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))


Money = Numeric(14, 8)  # US dollars; a cheap model call costs fractions of a cent


class Run(Base):
    """One agent run (an OTel trace) or one Claude Code session."""

    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = _id()
    source: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    agent: Mapped[str | None] = mapped_column(Text, comment="Agent or service name")
    name: Mapped[str | None] = mapped_column(Text, comment="Root span name or session title")
    status: Mapped[str] = mapped_column(Text, default="unset", server_default="unset")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="Null while a run is still going"
    )
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    cost_usd: Mapped[Decimal | None] = mapped_column(
        Money, comment="Sum of span costs at ingest; null if any priced span is unknown"
    )
    attributes: Mapped[dict[str, Any]] = _json()
    created_at: Mapped[datetime] = _created_at()

    spans: Mapped[list[Span]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        CheckConstraint(_one_of("source", SOURCES), name="source"),
        CheckConstraint(_one_of("status", STATUSES), name="status"),
        Index(None, "started_at"),
        Index(None, "agent", "started_at"),
    )


class Span(Base):
    """One step in a run: a model call, a tool call, an agent, or any other step."""

    __tablename__ = "spans"

    id: Mapped[uuid.UUID] = _id()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(Text)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("spans.id", ondelete="CASCADE"))
    parent_external_id: Mapped[str | None] = mapped_column(
        Text, comment="Kept so a child that arrives before its parent can be linked later"
    )
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text, comment="As <provider>:<model> when known")
    status: Mapped[str] = mapped_column(Text, default="unset", server_default="unset")
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    cost_usd: Mapped[Decimal | None] = mapped_column(Money, comment="Priced at ingest")
    input: Mapped[Any | None] = mapped_column(JSONB, comment="Redacted before storing")
    output: Mapped[Any | None] = mapped_column(JSONB, comment="Redacted before storing")
    attributes: Mapped[dict[str, Any]] = _json()
    redactions: Mapped[dict[str, int]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="Count per redaction pattern; the original values are never stored",
    )

    run: Mapped[Run] = relationship(back_populates="spans")
    parent: Mapped[Span | None] = relationship(remote_side="Span.id", back_populates="children")
    children: Mapped[list[Span]] = relationship(back_populates="parent")

    __table_args__ = (
        UniqueConstraint("run_id", "external_id"),
        CheckConstraint(_one_of("kind", SPAN_KINDS), name="kind"),
        CheckConstraint(_one_of("status", STATUSES), name="status"),
        Index(None, "run_id", "started_at"),
        Index(None, "parent_id"),
        Index("ix_spans_errors", "run_id", postgresql_where=text("status = 'error'")),
    )


class Embedding(Base):
    """A vector for a run summary or a failed span (#11)."""

    __tablename__ = "embeddings"

    id: Mapped[uuid.UUID] = _id()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    span_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("spans.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text, comment="Embedding model, e.g. bge-small-en-v1.5")
    content: Mapped[str] = mapped_column(Text, comment="The text that was embedded")
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    created_at: Mapped[datetime] = _created_at()

    run: Mapped[Run] = relationship()
    span: Mapped[Span | None] = relationship()

    __table_args__ = (
        UniqueConstraint("run_id", "span_id", "model", postgresql_nulls_not_distinct=True),
        CheckConstraint(_one_of("kind", EMBEDDING_KINDS), name="kind"),
        Index(
            "ix_embeddings_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class Label(Base):
    """A failure mode on a run or one of its spans."""

    __tablename__ = "labels"

    id: Mapped[uuid.UUID] = _id()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    span_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("spans.id", ondelete="CASCADE"))
    failure_mode: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text, comment="Who labelled it, or which model")
    confidence: Mapped[float | None]
    created_at: Mapped[datetime] = _created_at()

    run: Mapped[Run] = relationship()
    span: Mapped[Span | None] = relationship()

    __table_args__ = (
        CheckConstraint(_one_of("source", LABEL_SOURCES), name="source"),
        CheckConstraint("confidence IS NULL OR confidence BETWEEN 0 AND 1", name="confidence"),
        Index(None, "run_id"),
        Index(None, "failure_mode"),
    )


class EvalResult(Base):
    """One eval case's result in one eval run. The cases themselves live in git (evals/cases/)."""

    __tablename__ = "eval_results"

    id: Mapped[uuid.UUID] = _id()
    suite_run_id: Mapped[uuid.UUID] = mapped_column(comment="Groups the results of one eval run")
    case_id: Mapped[str] = mapped_column(Text)
    passed: Mapped[bool]
    score: Mapped[float | None]
    model: Mapped[str] = mapped_column(Text)
    git_sha: Mapped[str] = mapped_column(Text)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), comment="The recorded run the case produced"
    )
    details: Mapped[dict[str, Any]] = _json()
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("suite_run_id", "case_id"),
        Index(None, "case_id", "created_at"),
    )
