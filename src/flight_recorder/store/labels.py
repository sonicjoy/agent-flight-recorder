"""Failure-mode labels on runs: write, count, rename, remove, and the sample to label (#14).

Hand labels have source `human`. Notes are redacted before they're stored, like every other
text a person or a model may have copied from a run (docs/schema.md).
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from typing import cast as typed

from sqlalchemy import String, and_, cast, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import aggregate_order_by, insert
from sqlalchemy.orm import Session, aliased

from flight_recorder.redact import Redactor
from flight_recorder.store.models import Label, Span

if TYPE_CHECKING:
    from sqlalchemy import CursorResult

HUMAN = "human"
MAX_MODE = 80
MERGED = "[before merge, was {}]"  # marks a note that came from a merged label


class SpanNotInRunError(LookupError):
    pass


RESERVED = ("s", "q")  # the queue's skip and quit


def check_mode(mode: str) -> str:
    """A failure mode is a short name: stripped, 1 to 80 printable characters, not a number
    (numbers pick an existing mode) and not `s` or `q`."""
    mode = mode.strip()
    if not mode or len(mode) > MAX_MODE or not mode.isprintable():
        raise ValueError(f"a failure mode is a name of 1 to {MAX_MODE} printable characters")
    if mode.isdigit() or mode.lower() in RESERVED:
        raise ValueError(
            f"{mode!r} isn't a mode name: a number picks a listed mode, s and q skip and quit"
        )
    return mode


def sample(population: Sequence[tuple[str, str]], size: int, seed: int) -> list[str]:
    """`size` run ids from (run id, stratum) pairs, each stratum in proportion to its share
    (largest remainder), chosen with `seed`. The result is sorted, and doesn't depend on the
    order of `population`. Each run's place comes from a hash of the seed and its id, so a
    new run costs the sample at most one pick in all."""
    if size >= len(population):
        return sorted(run for run, _ in population)
    strata: dict[str, list[str]] = defaultdict(list)
    for run, stratum in sorted(population):
        strata[stratum].append(run)
    shares = {s: size * len(runs) / len(population) for s, runs in strata.items()}
    counts = {s: int(share) for s, share in shares.items()}
    by_remainder = sorted(strata, key=lambda s: (-(shares[s] - counts[s]), s))
    for stratum in by_remainder[: size - sum(counts.values())]:
        counts[stratum] += 1

    def place(run: str) -> str:
        return hashlib.sha256(f"{seed}:{run}".encode()).hexdigest()

    picked = [run for s in sorted(strata) for run in sorted(strata[s], key=place)[: counts[s]]]
    return sorted(picked)


def span_of_run(session: Session, run_id: uuid.UUID, prefix: str) -> uuid.UUID:
    """The run's span whose id starts with `prefix`."""
    bare = prefix.strip().lower().replace("-", "")
    matches = session.scalars(
        select(Span.id).where(
            Span.run_id == run_id,
            func.replace(cast(Span.id, String), "-", "").startswith(bare, autoescape=True),
        )
    ).all()
    if len(matches) != 1:
        problem = "no span" if not matches else "more than one span"
        raise SpanNotInRunError(f"{problem} of this run starts with {prefix!r}")
    return matches[0]


def add_label(
    session: Session,
    run_id: uuid.UUID,
    mode: str,
    note: str | None,
    *,
    author: str | None,
    redactor: Redactor,
    span_id: uuid.UUID | None = None,
) -> Label | None:
    """A human label, or None when the run (or span) already has this mode from a human,
    even one another labeller added a moment ago: the unique index decides (migration 0007)."""
    mode = check_mode(mode)
    if mode in modes_of(session, run_id, span_id):  # the usual case, without a write
        return None
    note = (note or "").strip() or None
    added = session.scalar(
        insert(Label)
        .values(
            run_id=run_id,
            span_id=span_id,
            failure_mode=mode,
            note=redactor.redact(note).value if note else None,
            source=HUMAN,
            author=author,
        )
        .on_conflict_do_nothing(
            index_elements=[Label.run_id, Label.span_id, Label.failure_mode],
            # literal, not a bound parameter: under a generic plan (psycopg prepares
            # after 5 runs) Postgres can't match `source = $1` to the partial index
            index_where=text("source = 'human'"),
        )
        .returning(Label.id)
    )
    return session.get(Label, added) if added is not None else None


@dataclass(frozen=True)
class ModeCount:
    mode: str
    labels: int
    runs: int
    example: uuid.UUID  # the earliest labelled run


def mode_counts(session: Session, source: str = HUMAN) -> list[ModeCount]:
    """Each failure mode with its labels and runs, most labelled runs first."""
    earliest = func.array_agg(aggregate_order_by(Label.run_id, Label.created_at, Label.id))
    rows = session.execute(
        select(
            Label.failure_mode,
            func.count(),
            func.count(Label.run_id.distinct()),
            earliest[1],
        )
        .where(Label.source == source)
        .group_by(Label.failure_mode)
    ).all()
    counts = [ModeCount(mode, labels, runs, example) for mode, labels, runs, example in rows]
    return sorted(counts, key=lambda c: (-c.runs, -c.labels, c.mode))


def labels_of(session: Session, run_id: uuid.UUID) -> list[Label]:
    return list(
        session.scalars(
            select(Label).where(Label.run_id == run_id).order_by(Label.created_at, Label.id)
        )
    )


def modes_of(session: Session, run_id: uuid.UUID, span_id: uuid.UUID | None = None) -> set[str]:
    """The human modes on the run (span_id None) or on one of its spans."""
    return set(
        session.scalars(
            select(Label.failure_mode).where(
                Label.run_id == run_id,
                Label.span_id.is_not_distinct_from(span_id),
                Label.source == HUMAN,
            )
        )
    )


def labelled_runs(session: Session, run_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    """Those of `run_ids` with at least one human label."""
    return set(
        session.scalars(
            select(Label.run_id).where(Label.run_id.in_(run_ids), Label.source == HUMAN)
        )
    )


def labelled_count(session: Session, source: str = HUMAN) -> int:
    """How many runs have at least one label from `source`."""
    return (
        session.scalar(select(func.count(Label.run_id.distinct())).where(Label.source == source))
        or 0
    )


def rename_mode(session: Session, old: str, new: str) -> tuple[int, int]:
    """Renames (or merges) a mode on human labels. Where a run (or span) has both modes,
    the old label is merged into the new one: its note is appended, marked as from before
    the merge, and the old label deleted. Returns how many labels were renamed and merged.
    Another labeller adding `new` meanwhile makes the rename fail (IntegrityError), whole."""
    old, new = old.strip(), check_mode(new)
    if old == new:  # every label would match itself as "has both" and be merged away
        raise ValueError("the new name is the same as the old")
    kept = aliased(Label)
    pairs = session.execute(
        select(Label.id, Label.note, kept)
        .join(
            kept,
            and_(
                kept.run_id == Label.run_id,
                kept.span_id.is_not_distinct_from(Label.span_id),
                kept.failure_mode == new,
                kept.source == HUMAN,
            ),
        )
        .where(Label.failure_mode == old, Label.source == HUMAN)
        .order_by(Label.id)  # the same lock order in every rename: fewer deadlocks
        .with_for_update()
        .execution_options(populate_existing=True)  # the kept notes as they are now
    ).all()
    for _, note, label in pairs:
        if note:
            label.note = "; ".join(filter(None, [label.note, f"{MERGED.format(old)} {note}"]))
    session.execute(delete(Label).where(Label.id.in_([dropped for dropped, _, _ in pairs])))
    renamed = session.execute(
        update(Label)
        .where(Label.failure_mode == old, Label.source == HUMAN)
        .values(failure_mode=new)
    )
    return typed("CursorResult[Any]", renamed).rowcount, len(pairs)


def remove_labels(session: Session, run_id: uuid.UUID, mode: str | None = None) -> int:
    """Deletes a run's human labels, or only those with `mode`; returns how many."""
    query = delete(Label).where(Label.run_id == run_id, Label.source == HUMAN)
    if mode is not None:
        query = query.where(Label.failure_mode == mode.strip())
    return typed("CursorResult[Any]", session.execute(query)).rowcount
