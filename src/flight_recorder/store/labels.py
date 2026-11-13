"""Failure-mode labels on runs: write, count, rename, remove, and the sample to label (#14).

Hand labels have source `human`. Notes are redacted before they're stored, like every other
text a person or a model may have copied from a run (docs/schema.md).
"""

from __future__ import annotations

import hashlib
import re
import string
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING, Any, NamedTuple
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
# Words in a mode: split at ASCII punctuation, spaces and control characters, with only A-Z
# lowercased. Everything else (digits, any non-ASCII character) is part of a word, as typed.
# No regex, lower() or locale, so Postgres's afr_mode_words (migration 0007) is the same rule
# exactly; a test compares the two.
SEPARATORS = string.punctuation + "".join(map(chr, [*range(1, 32), 127]))
FOLD = str.maketrans(string.ascii_uppercase + SEPARATORS,
                     string.ascii_lowercase + " " * len(SEPARATORS))  # fmt: skip
TYPED_TAG = "[afr-note v"  # escaped in notes people write, so only afr writes tags
RESERVED = ("s", "q")  # the queue's skip and quit
TAG = "[afr-note v1 at={at} by={by} mode={mode}]"  # a note that came from another label
FRAGMENT = re.compile(r"(?:^|; )(?=\[afr-note v\d+ )")
ENCODED = (("%", "%25"), (" ", "%20"), (";", "%3B"), ("[", "%5B"), ("]", "%5D"))


class SpanNotInRunError(LookupError):
    pass


def check_mode(mode: str) -> str:
    """A failure mode as stored: its words (letters and digits), lowercased and joined with
    `-`, so "Tool Timeout" is `tool-timeout`. What's typed is 1 to 80 printable characters,
    with a letter or digit, not a number (numbers pick an existing mode) and not `s` or `q`."""
    mode = mode.strip()
    if not mode or len(mode) > MAX_MODE or not mode.isprintable():
        raise ValueError(f"a failure mode is a name of 1 to {MAX_MODE} printable characters")
    spelled = "-".join(words(mode))
    if not spelled:
        raise ValueError(f"{mode!r} isn't a mode name: it needs a letter or digit")
    if spelled.isdigit() or spelled in RESERVED:
        raise ValueError(
            f"{mode!r} isn't a mode name: a number picks a listed mode, s and q skip and quit"
        )
    return spelled


def words(mode: str) -> list[str]:
    return [word for word in mode.translate(FOLD).split(" ") if word]


def mode_key(session: Session, mode: str) -> str:
    """The mode's words, sorted: one mode whatever their order, case or separators. Postgres
    computes it (afr_mode_key, migration 0007), for `labels.mode_key` too."""
    return session.scalar(select(func.afr_mode_key(mode))) or ""


def spelling(session: Session, mode: str) -> tuple[str, str]:
    """How a typed mode is stored, and its key: the spelling human labels already use for
    this key (the earliest), or else check_mode's."""
    spelled = check_mode(mode)
    key = mode_key(session, spelled)
    existing = session.scalar(
        select(Label.failure_mode)
        .where(Label.mode_key == key, Label.source == HUMAN)
        .order_by(Label.created_at, Label.id)
        .limit(1)
    )
    return existing or spelled, key


def encoded(value: str) -> str:
    for char, code in ENCODED:
        value = value.replace(char, code)
    return value


def tag(label: Label) -> str:
    """The label as a tag on its note, built from the label alone (migration 0007 too)."""
    at = label.created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return TAG.format(at=at, by=encoded(label.author or "-"), mode=encoded(label.failure_mode))


def split_note(note: str | None) -> tuple[str, set[str]]:
    """A note's own text, and the tagged fragments it took from merged labels."""
    own, *fragments = FRAGMENT.split(note or "")
    return own, set(fragments)


def merged_note(kept: str | None, dropped: Label) -> str | None:
    """`kept`'s own text, then every fragment of both and `dropped`'s own text tagged, sorted.
    So merging labels gives the same note in any order, and a chain (a into b, b into c)
    the same as merging each straight into c."""
    own, fragments = split_note(kept)
    dropped_own, dropped_fragments = split_note(dropped.note)
    fragments |= dropped_fragments
    if dropped_own:
        fragments.add(f"{tag(dropped)} {dropped_own}")
    return "; ".join(filter(None, [own, *sorted(fragments)])) or None


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
    """A human label, or None when the run (or span) already has this mode, in any spelling,
    even one another labeller added a moment ago: the unique index decides (migration 0007)."""
    mode, key = spelling(session, mode)
    if key in modes_of(session, run_id, span_id):  # the usual case, without a write
        return None
    note = (note or "").strip().replace(TYPED_TAG, "\\" + TYPED_TAG) or None
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
            index_elements=[Label.run_id, Label.span_id, Label.mode_key],
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
    """Each failure mode (by key, in its earliest spelling) with its labels and runs, most
    labelled runs first."""
    in_order = (Label.created_at, Label.id)
    earliest = func.array_agg(aggregate_order_by(Label.run_id, *in_order))
    spelled = func.array_agg(aggregate_order_by(Label.failure_mode, *in_order))
    rows = session.execute(
        select(
            spelled[1],
            func.count(),
            func.count(Label.run_id.distinct()),
            earliest[1],
        )
        .where(Label.source == source)
        .group_by(Label.mode_key)
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
    """The keys of the human modes on the run (span_id None) or on one of its spans."""
    keys = session.scalars(
        select(Label.mode_key).where(
            Label.run_id == run_id,
            Label.span_id.is_not_distinct_from(span_id),
            Label.source == HUMAN,
        )
    )
    return {key for key in keys if key is not None}  # never None: failure_mode is NOT NULL


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


class Renamed(NamedTuple):
    renamed: int  # given the new mode
    merged: int  # into a label the run (or span) already had with the new mode
    respelled: int  # same words, new spelling


def rename_mode(session: Session, old: str, new: str) -> Renamed:
    """Renames a mode (found whatever its spelling) on human labels, merges it into another,
    or respells it when the words are the same. A merged label's note joins the kept one as
    a tagged fragment (merged_note), and the label is deleted. Another labeller adding the
    new mode meanwhile makes the rename fail whole (IntegrityError)."""
    spelled = check_mode(new)
    old_key, new_key = mode_key(session, old), mode_key(session, spelled)
    mine = and_(Label.mode_key == old_key, Label.source == HUMAN)
    if old_key == new_key:
        if session.scalar(select(func.count()).where(mine)) == 0:
            return Renamed(0, 0, 0)
        respelled = session.execute(
            update(Label).where(mine, Label.failure_mode != spelled).values(failure_mode=spelled)
        )
        if not (count := typed("CursorResult[Any]", respelled).rowcount):
            raise ValueError(f"the mode is already spelled {spelled!r}")
        return Renamed(0, 0, count)
    target, _ = spelling(session, spelled)
    dropped, kept = aliased(Label), aliased(Label)
    pairs = session.execute(
        select(dropped, kept)
        .join(
            kept,
            and_(
                kept.run_id == dropped.run_id,
                kept.span_id.is_not_distinct_from(dropped.span_id),
                kept.mode_key == new_key,
                kept.source == HUMAN,
            ),
        )
        .where(dropped.mode_key == old_key, dropped.source == HUMAN)
        .order_by(dropped.id)  # the same lock order in every rename: fewer deadlocks
        .with_for_update()
        .execution_options(populate_existing=True)  # the kept notes as they are now
    ).all()
    for gone, label in pairs:
        label.note = merged_note(label.note, gone)
    session.execute(delete(Label).where(Label.id.in_([gone.id for gone, _ in pairs])))
    renamed = session.execute(update(Label).where(mine).values(failure_mode=target))
    return Renamed(typed("CursorResult[Any]", renamed).rowcount, len(pairs), 0)


def remove_labels(session: Session, run_id: uuid.UUID, mode: str | None = None) -> int:
    """Deletes a run's human labels, or only those with `mode`; returns how many."""
    query = delete(Label).where(Label.run_id == run_id, Label.source == HUMAN)
    if mode is not None:  # whatever its spelling
        query = query.where(Label.mode_key == func.afr_mode_key(mode))
    return typed("CursorResult[Any]", session.execute(query)).rowcount
