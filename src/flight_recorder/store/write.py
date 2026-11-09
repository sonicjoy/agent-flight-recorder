"""The only way runs and spans get into the store: redact, price, then write.

Ingest (#8 for OTel, #9 for Claude Code) maps each source's data to RunIn/SpanIn in
memory and calls record_run(). record_run redacts every free-text field, attributes and
input/output at any depth, before any row is built, so no column can hold an original
value (docs/redaction.md). A test fails if anything else in src writes to runs or spans.

Re-sending a run or span updates it: rows are keyed by the source's ids. A child span may
arrive before its parent (in an earlier batch); it is linked once the parent arrives.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
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
    """A run as sent by a source, before redaction."""

    source: Source
    external_id: str
    agent: str | None = None
    name: str | None = None
    status: Status = "unset"
    started_at: datetime
    ended_at: datetime | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


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
    output_tokens: int | None = Field(default=None, ge=0)
    input: Any = None
    output: Any = None
    attributes: dict[str, Any] = Field(default_factory=dict)


def _price(
    model: str | None, input_tokens: int | None, output_tokens: int | None
) -> Decimal | None:
    if model is None or (input_tokens is None and output_tokens is None):
        return None
    cost = cost_usd(model, input_tokens or 0, output_tokens or 0)
    return None if cost is None else Decimal(str(cost)).quantize(CENT_FRACTION)


def _upsert(table: type[Run] | type[Span], key: list[str], values: dict[str, Any]) -> Any:
    statement = insert(table).values(**values)
    changes = {name: statement.excluded[name] for name in values if name not in key}
    return statement.on_conflict_do_update(index_elements=key, set_=changes).returning(table.id)


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
        _upsert(Run, ["source", "external_id"], run_values)
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
            "cost_usd": _price(model, span.input_tokens, span.output_tokens),
            "input": redactor.value(span.input, counts),
            "output": redactor.value(span.output, counts),
            "attributes": redactor.value(span.attributes, counts),
            "redactions": dict(counts),
        }
        session.execute(_upsert(Span, ["run_id", "external_id"], values))

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
