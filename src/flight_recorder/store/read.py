"""Read recorded runs back: find runs, resolve an id, load a run's span tree and contents.

The CLI uses these now; the MCP server (#24) and the replay UI (#29) can use them later.
Nothing here loads span input or output except `span_content`, one span at a time: a long
Claude Code session has thousands of spans, each with up to 32 KB per string.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import ColumnElement, String, and_, case, cast, func, or_, select
from sqlalchemy.orm import Session

from flight_recorder.store.models import Run, Span

RUNNING_WINDOW = timedelta(minutes=30)
"""A run with no end is "running" if it changed this recently, else "unfinished"."""

RunState = Literal["unset", "ok", "error", "running", "unfinished"]
RUN_STATES: tuple[RunState, ...] = ("unset", "ok", "error", "running", "unfinished")
PROBLEMS = ("error", "rejected")


class RunNotFoundError(LookupError):
    pass


class AmbiguousRunError(LookupError):
    def __init__(self, ref: str, matches: Sequence[Run]) -> None:
        super().__init__(f"{ref!r} matches {len(matches)} runs")
        self.matches = list(matches)


def run_state(run: Run, now: datetime) -> RunState:
    """The run's status, or, while it has no end, whether it's still going."""
    if run.ended_at is not None:
        return run.status  # type: ignore[return-value]  # constrained by ck_runs_status
    return "running" if now - run.updated_at <= RUNNING_WINDOW else "unfinished"


@dataclass(frozen=True)
class RunFilter:
    source: str | None = None
    agent: str | None = None
    status: RunState | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 20


@dataclass(frozen=True)
class RunSummary:
    run: Run
    state: RunState
    spans: int
    known_cost: Decimal | None
    """The sum of the priced spans' costs; with `unpriced`, the part of the cost we know."""
    unpriced: bool
    """Some span has tokens but no price (its model isn't in pricing.yaml)."""


def _state_clause(state: RunState, now: datetime) -> ColumnElement[bool]:
    recent = Run.updated_at >= now - RUNNING_WINDOW
    if state == "running":
        return and_(Run.ended_at.is_(None), recent)
    if state == "unfinished":
        return and_(Run.ended_at.is_(None), ~recent)
    return and_(Run.ended_at.is_not(None), Run.status == state)


def _unpriced() -> ColumnElement[bool]:
    """A span with tokens but no cost: its model isn't in pricing.yaml."""
    return and_(
        Span.cost_usd.is_(None),
        or_(Span.input_tokens.is_not(None), Span.output_tokens.is_not(None)),
    )


def find_runs(
    session: Session, where: RunFilter, now: datetime, ids: Sequence[uuid.UUID] | None = None
) -> list[RunSummary]:
    """Runs matching the filter (and among `ids`, if given), newest first."""
    query = select(Run).order_by(Run.started_at.desc(), Run.id).limit(where.limit)
    if ids is not None:
        query = query.where(Run.id.in_(ids))
    if where.source:
        query = query.where(Run.source == where.source)
    if where.agent:
        query = query.where(Run.agent == where.agent)
    if where.status:
        query = query.where(_state_clause(where.status, now))
    if where.since:
        query = query.where(Run.started_at >= where.since)
    if where.until:
        query = query.where(Run.started_at < where.until)
    runs = session.scalars(query).all()
    if not runs:
        return []
    totals = {
        row.run_id: row
        for row in session.execute(
            select(
                Span.run_id,
                func.count().label("spans"),
                func.sum(Span.cost_usd).label("known_cost"),
                func.bool_or(_unpriced()).label("unpriced"),
            )
            .where(Span.run_id.in_([r.id for r in runs]))
            .group_by(Span.run_id)
        )
    }
    summaries: list[RunSummary] = []
    for run in runs:
        total = totals.get(run.id)
        summaries.append(
            RunSummary(
                run=run,
                state=run_state(run, now),
                spans=total.spans if total else 0,
                known_cost=total.known_cost if total else None,
                unpriced=bool(total and total.unpriced),
            )
        )
    return summaries


HEX_PREFIX = re.compile(r"[0-9a-f-]{4,36}")


def resolve_run(session: Session, ref: str) -> Run:
    """A run from `last` (the newest finished run), a unique prefix of its id (at least 4
    characters), or its source's id (a Claude Code session id or OTel trace id) in full."""
    ref = ref.strip()
    if ref == "last":
        run = session.scalars(
            select(Run).where(Run.ended_at.is_not(None)).order_by(Run.started_at.desc()).limit(1)
        ).first()
        if run is None:
            raise RunNotFoundError("no finished runs yet")
        return run
    matches: list[Run] = list(
        session.scalars(select(Run).where(Run.external_id == ref).order_by(Run.started_at)).all()
    )
    prefix = ref.lower()
    if HEX_PREFIX.fullmatch(prefix):
        matches += session.scalars(
            select(Run)
            .where(cast(Run.id, String).startswith(prefix, autoescape=True))
            .order_by(Run.started_at)
            .limit(20)
        ).all()
    unique = list({run.id: run for run in matches}.values())
    if not unique:
        short = len(prefix) < 4 and HEX_PREFIX.fullmatch(prefix.ljust(4, "0"))
        hint = "; give at least 4 characters of the id" if short else ""
        raise RunNotFoundError(f"no run matches {ref!r}{hint}")
    if len(unique) > 1:
        raise AmbiguousRunError(ref, unique)
    return unique[0]


@dataclass(eq=False)  # compared and hashed by identity: one node per span
class SpanNode:
    id: uuid.UUID
    external_id: str
    parent_id: uuid.UUID | None
    kind: str
    name: str
    model: str | None
    status: str
    error: str | None
    started_at: datetime
    ended_at: datetime | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    usage: dict[str, Any]
    """Cache tokens and fast mode, from attributes.usage."""
    depth: int = 0
    children: list[SpanNode] = field(default_factory=lambda: [])
    parent: SpanNode | None = field(default=None, repr=False)

    @property
    def problem(self) -> bool:
        return self.status in PROBLEMS

    @property
    def unpriced(self) -> bool:
        return self.cost_usd is None and (
            self.input_tokens is not None or self.output_tokens is not None
        )

    def path(self) -> list[SpanNode]:
        """This span and its ancestors, root first."""
        nodes: list[SpanNode] = []
        node: SpanNode | None = self
        while node is not None:
            nodes.append(node)
            node = node.parent
        return nodes[::-1]

    def walk(self) -> list[SpanNode]:
        """This span and every descendant, in tree order."""
        nodes: list[SpanNode] = [self]
        for child in self.children:
            nodes += child.walk()
        return nodes


def _order(node: SpanNode) -> tuple[datetime, str]:
    return node.started_at, node.external_id


def span_tree(session: Session, run_id: uuid.UUID) -> list[SpanNode]:
    """The run's spans as a tree, without their contents. Returns the roots: the spans
    with no parent, and any whose parent hasn't arrived. Children are in start order."""
    usage = Span.attributes["usage"]
    rows = session.execute(
        select(
            Span.id,
            Span.external_id,
            Span.parent_id,
            Span.kind,
            Span.name,
            Span.model,
            Span.status,
            Span.error,
            Span.started_at,
            Span.ended_at,
            Span.input_tokens,
            Span.output_tokens,
            Span.cost_usd,
            case((func.jsonb_typeof(usage) == "object", usage), else_=None).label("usage"),
        ).where(Span.run_id == run_id)
    ).all()
    nodes: dict[uuid.UUID, SpanNode] = {}
    for row in rows:
        nodes[row.id] = SpanNode(
            id=row.id,
            external_id=row.external_id,
            parent_id=row.parent_id,
            kind=row.kind,
            name=row.name,
            model=row.model,
            status=row.status,
            error=row.error,
            started_at=row.started_at,
            ended_at=row.ended_at,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cost_usd=row.cost_usd,
            usage=row.usage or {},
        )
    for node in nodes.values():
        parent = nodes.get(node.parent_id) if node.parent_id else None
        if parent is not None:
            node.parent = parent
            parent.children.append(node)
    roots = sorted((n for n in nodes.values() if n.parent is None), key=_order)
    reached = _place(roots)
    while len(reached) < len(nodes):  # a parent loop: no root reaches it, so cut it
        loose = min((n for n in nodes.values() if n.id not in reached), key=_order)
        assert loose.parent is not None
        loose.parent.children.remove(loose)
        loose.parent = None
        roots = sorted([*roots, loose], key=_order)
        reached |= _place([loose])
    return roots


def _place(roots: Sequence[SpanNode]) -> set[uuid.UUID]:
    """Sort children by start and set depths below these roots; returns the ids reached."""
    reached: set[uuid.UUID] = set()
    pending = list(roots)
    while pending:
        node = pending.pop()
        reached.add(node.id)
        node.children.sort(key=_order)
        for child in node.children:
            child.depth = node.depth + 1
            pending.append(child)
    return reached


def steps(roots: Sequence[SpanNode]) -> list[SpanNode]:
    """Every span in time order, for a replay. Ties (a placeholder starts with its first
    child; instant spans share a start) put parents first, then go by id."""
    nodes = [n for root in roots for n in root.walk()]
    return sorted(nodes, key=lambda n: (n.started_at, n.depth, n.external_id))


@dataclass(frozen=True)
class SpanContent:
    input: Any
    output: Any
    attributes: dict[str, Any]


def span_content(session: Session, span_id: uuid.UUID) -> SpanContent:
    """One span's input, output and attributes (all redacted at ingest)."""
    row = session.execute(
        select(Span.input, Span.output, Span.attributes).where(Span.id == span_id)
    ).one()
    return SpanContent(row.input, row.output, row.attributes or {})
