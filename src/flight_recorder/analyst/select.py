"""Which runs failed (docs/designs/analyst.md §1): one agent's error runs, and with `--attr`,
its runs matching every attribute, never a Claude Code session or a run whose recorded truth
a human marked as bad."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from flight_recorder.evals.cases import LOCAL_SOURCES
from flight_recorder.store.labels import has_truth_problem
from flight_recorder.store.read import RunFilter, RunSummary, find_runs

EVERY_RUN = 100_000  # as `afr label --next`: selection is never cut at a page size


@dataclass(frozen=True)
class Failed:
    run: RunSummary
    reasons: frozenset[str]  # "error", "attr"


@dataclass
class Selection:
    failed: list[Failed] = field(default_factory=list[Failed])
    counts: dict[str, int] = field(default_factory=dict[str, int])
    vetoed: int = 0


def select_failed(
    session: Session, agent: str, attrs: Mapping[str, tuple[str, ...]], now: datetime
) -> Selection:
    """The agent's failed runs, newest first, each with the reasons it counts."""
    branches = [("error", RunFilter(agent=agent, status="error", limit=EVERY_RUN))]
    if attrs:
        branches.append(("attr", RunFilter(agent=agent, attrs=attrs, limit=EVERY_RUN)))
    runs: dict[object, RunSummary] = {}
    reasons: dict[object, set[str]] = {}
    for reason, where in branches:
        for summary in find_runs(session, where, now):
            if summary.run.source in LOCAL_SOURCES:
                continue
            runs.setdefault(summary.run.id, summary)
            reasons.setdefault(summary.run.id, set()).add(reason)
    selection = Selection()
    for run_id, summary in sorted(runs.items(), key=lambda kv: kv[1].run.started_at,
                                  reverse=True):  # fmt: skip
        if has_truth_problem(session, summary.run.id):
            selection.vetoed += 1
            continue
        selection.failed.append(Failed(summary, frozenset(reasons[run_id])))
        for reason in reasons[run_id]:
            selection.counts[reason] = selection.counts.get(reason, 0) + 1
    return selection
