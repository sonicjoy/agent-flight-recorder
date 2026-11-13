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

from sqlalchemy import String, cast, delete, func, select, update
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import Session

from flight_recorder.redact import Redactor
from flight_recorder.store.models import Label, Span

if TYPE_CHECKING:
    from sqlalchemy import CursorResult

HUMAN = "human"
MAX_MODE = 80


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
) -> Label:
    note = (note or "").strip() or None
    label = Label(
        run_id=run_id,
        span_id=span_id,
        failure_mode=check_mode(mode),
        note=redactor.redact(note).value if note else None,
        source=HUMAN,
        author=author,
    )
    session.add(label)
    session.flush()
    return label


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


def rename_mode(session: Session, old: str, new: str) -> int:
    """Renames (or merges) a mode on human labels; returns how many labels changed."""
    result = session.execute(
        update(Label)
        .where(Label.failure_mode == old.strip(), Label.source == HUMAN)
        .values(failure_mode=check_mode(new))
    )
    return typed("CursorResult[Any]", result).rowcount


def remove_labels(session: Session, run_id: uuid.UUID, mode: str | None = None) -> int:
    """Deletes a run's human labels, or only those with `mode`; returns how many."""
    query = delete(Label).where(Label.run_id == run_id, Label.source == HUMAN)
    if mode is not None:
        query = query.where(Label.failure_mode == mode.strip())
    return typed("CursorResult[Any]", session.execute(query)).rowcount
