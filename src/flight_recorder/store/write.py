"""The only way runs and spans get into the store: redact, price, then write.

Ingest (#8 for OTel, #9 for Claude Code) maps each source's data to RunIn/SpanIn in
memory and calls record_run(). record_run redacts every free-text field, attributes and
input/output at any depth, before any row is built, so no column can hold an original
value (docs/redaction.md). A test fails if anything else in src writes to runs or spans.

Re-sending a run or span updates it: rows are keyed by the source's ids. A span is
replaced, since it is sent whole once it ends. A run is merged, since each batch may know
only part of it: what a batch leaves out keeps its stored value (see _upsert_run). A child
span may arrive before its parent (in an earlier batch); it is linked once the parent
arrives.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, literal_column, text
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Session

from flight_recorder.pricing import cost_usd
from flight_recorder.redact import Redactor
from flight_recorder.store.models import Run, Span

Source = Literal["otel", "claude_code"]
Status = Literal["unset", "ok", "error"]
Kind = Literal["agent", "llm", "tool", "step", "other"]
CENT_FRACTION = Decimal("0.00000001")  # matches numeric(14, 8)


class _In(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunIn(_In):
    """A run as sent by a source, before redaction.

    A batch may send only what it knows: leave a field None (or status "unset") and the
    stored value is kept. started_at may be the earliest span in this batch.

    fills_gaps marks a batch whose agent and attributes may describe something other than
    the run, such as an OTel batch without the root span, which can come from a downstream
    service. Its agent is stored only if none is, and its attributes never replace stored
    ones.
    """

    source: Source
    external_id: str
    agent: str | None = None
    name: str | None = None
    status: Status = "unset"
    started_at: datetime
    ended_at: datetime | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    fills_gaps: bool = False


class SpanIn(_In):
    """A span as sent by a source, before redaction. Events go in attributes or output."""

    external_id: str
    parent_external_id: str | None = None
    kind: Kind
    name: str
    model: str | None = None
    status: Status = "unset"
    error: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    """All input tokens, cached or not (the OTel GenAI convention)."""
    output_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    """Part of input_tokens: read from the prompt cache."""
    cache_write_5m_tokens: int | None = Field(default=None, ge=0)
    cache_write_1h_tokens: int | None = Field(default=None, ge=0)
    fast: bool = False
    """Claude fast mode, priced at its premium rates."""
    input: Any = None
    output: Any = None
    attributes: dict[str, Any] = Field(default_factory=dict)


def _price(model: str | None, span: SpanIn) -> Decimal | None:
    if model is None or (span.input_tokens is None and span.output_tokens is None):
        return None
    read = span.cache_read_tokens or 0
    write_5m = span.cache_write_5m_tokens or 0
    write_1h = span.cache_write_1h_tokens or 0
    cost = cost_usd(
        model,
        max((span.input_tokens or 0) - read - write_5m - write_1h, 0),  # uncached input
        span.output_tokens or 0,
        cache_read=read,
        cache_write_5m=write_5m,
        cache_write_1h=write_1h,
        fast=span.fast,
    )
    return None if cost is None else Decimal(str(cost)).quantize(CENT_FRACTION)


def _usage(span: SpanIn) -> dict[str, Any]:
    """Cache and speed details, kept in attributes so a later reprice can use them."""
    usage = {
        "cache_read_tokens": span.cache_read_tokens,
        "cache_write_5m_tokens": span.cache_write_5m_tokens,
        "cache_write_1h_tokens": span.cache_write_1h_tokens,
        "fast": span.fast or None,
    }
    present = {k: v for k, v in usage.items() if v is not None}
    return {"usage": present} if present else {}


def _upsert_span(values: dict[str, Any]) -> Any:
    """A span is sent whole once it ends, so a re-send replaces it."""
    statement = insert(Span).values(**values)
    key = ("run_id", "external_id")
    changes = {name: statement.excluded[name] for name in values if name not in key}
    return statement.on_conflict_do_update(index_elements=key, set_=changes)


# The largest count per pattern seen in one batch. A sum would grow every time a source
# re-sends the same resource attributes; the originals are gone, so it can't be recounted.
_MAX_REDACTIONS = literal_column(
    """(SELECT coalesce(jsonb_object_agg(key, n), '{}'::jsonb) FROM (
          SELECT key, max(value::int) AS n FROM (
            SELECT * FROM jsonb_each_text(runs.redactions)
            UNION ALL SELECT * FROM jsonb_each_text(excluded.redactions)
          ) AS c GROUP BY key
        ) AS m)""",
    type_=JSONB,
)


def _upsert_run(values: dict[str, Any], *, fills_gaps: bool) -> Any:
    """Each batch may know only part of a run, so a re-send merges into what is stored.

    A field the batch leaves out (None, or status "unset") keeps its stored value; the run
    spans the earliest start and the latest end seen; attributes merge, newer keys winning.
    With fills_gaps, the stored agent and attribute keys win instead.
    """
    statement = insert(Run).values(**values)
    new = statement.excluded
    first, second = (Run, new) if fills_gaps else (new, Run)
    changes = {
        "agent": func.coalesce(first.agent, second.agent),
        "name": func.coalesce(new.name, Run.name),
        "status": case((new.status == "unset", Run.status), else_=new.status),
        "started_at": func.least(Run.started_at, new.started_at),
        "ended_at": func.greatest(Run.ended_at, new.ended_at),  # greatest() skips NULLs
        "attributes": second.attributes.op("||")(first.attributes),  # right-hand keys win
        "redactions": _MAX_REDACTIONS,
    }
    return statement.on_conflict_do_update(
        index_elements=["source", "external_id"], set_=changes
    ).returning(Run.id)


def record_run(
    session: Session, run: RunIn, spans: Sequence[SpanIn], *, redactor: Redactor
) -> uuid.UUID:
    """Redact and write a run and some or all of its spans. Returns the run's id."""
    counts: Counter[str] = Counter()
    run_values = {
        "source": run.source,
        "external_id": run.external_id,
        "agent": redactor.value(run.agent, counts),
        "name": redactor.value(run.name, counts),
        "status": run.status,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "attributes": redactor.value(run.attributes, counts),
        "redactions": dict(counts),
    }
    run_id: uuid.UUID = session.execute(
        _upsert_run(run_values, fills_gaps=run.fills_gaps)
    ).scalar_one()

    for span in spans:
        counts = Counter()
        model = redactor.value(span.model, counts)
        values = {
            "run_id": run_id,
            "external_id": span.external_id,
            "parent_external_id": span.parent_external_id,
            "kind": span.kind,
            "name": redactor.value(span.name, counts),
            "model": model,
            "status": span.status,
            "error": redactor.value(span.error, counts),
            "started_at": span.started_at,
            "ended_at": span.ended_at,
            "input_tokens": span.input_tokens,
            "output_tokens": span.output_tokens,
            "cost_usd": _price(model, span),
            "input": redactor.value(span.input, counts),
            "output": redactor.value(span.output, counts),
            "attributes": redactor.value(span.attributes | _usage(span), counts),
            "redactions": dict(counts),
        }
        session.execute(_upsert_span(values))

    _link_parents(session, run_id)
    _total(session, run_id)
    return run_id


def _link_parents(session: Session, run_id: uuid.UUID) -> None:
    """Set parent_id wherever the parent has arrived, in this batch or an earlier one."""
    session.execute(
        text("""
            UPDATE spans AS child SET parent_id = parent.id
            FROM spans AS parent
            WHERE child.run_id = :run_id AND parent.run_id = child.run_id
              AND parent.external_id = child.parent_external_id
              AND child.parent_id IS DISTINCT FROM parent.id
        """),
        {"run_id": run_id},
    )


def _total(session: Session, run_id: uuid.UUID) -> None:
    """Run totals from all its spans. Cost is NULL if any span with tokens has no price."""
    session.execute(
        text("""
            UPDATE runs SET input_tokens = t.input_tokens, output_tokens = t.output_tokens,
                            cost_usd = t.cost_usd
            FROM (
                SELECT sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
                       CASE WHEN bool_or(cost_usd IS NULL AND (input_tokens IS NOT NULL
                                                               OR output_tokens IS NOT NULL))
                            THEN NULL ELSE sum(cost_usd) END AS cost_usd
                FROM spans WHERE run_id = :run_id
            ) AS t
            WHERE runs.id = :run_id
        """),
        {"run_id": run_id},
    )
