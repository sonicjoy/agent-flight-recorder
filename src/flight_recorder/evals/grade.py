"""Grading saved outputs and gating them against a baseline (#15, docs/evals/harness.md).

Each case gets one outcome: skipped, stale, error, pass or fail. `baseline.json` records each
reviewed, graded case's check results, keyed by check, and `compare` sorts every difference
into the rows of the gate table. `run` asks "did the agent get worse?", so it fails only on
regressions, edited checks and stale cases. `grade` re-grades saved outputs, where any
difference can only come from the grading code or the cases, so it fails on any difference.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal

from flight_recorder.evals.checks import CheckResult, canonical, run_checks

GRADED = ("pass", "fail", "error")
NO_LINE_UNREVIEWED = "no output: run it with --include-unreviewed"


def input_hash(case: dict[str, Any]) -> str:
    """What makes a saved output stale: a hash of the case's input, and nothing else, so
    editing a check never forces a rerun."""
    return "sha256:" + hashlib.sha256(canonical(case.get("input")).encode()).hexdigest()


@dataclass(frozen=True)
class CaseResult:
    id: str
    outcome: str  # skipped, stale, error, pass or fail
    note: str | None
    unreviewed: bool
    checks: list[CheckResult]
    error: str | None


@dataclass(frozen=True)
class SuiteResult:
    cases: list[CaseResult]
    orphaned: list[str]  # lines for cases that no longer exist
    fields: dict[str, tuple[int, int]]  # field -> (passed, graded)


def _grade_case(case: dict[str, Any], line: dict[str, Any] | None, include: bool) -> CaseResult:
    unreviewed = case.get("unreviewed") is True
    checks: list[dict[str, Any]] = case["checks"]

    def result(outcome: str, note: str | None = None, **kw: Any) -> CaseResult:
        return CaseResult(case["id"], outcome, note, unreviewed, kw.get("checks", []),
                          kw.get("error"))  # fmt: skip

    if unreviewed and not include:
        return result("skipped", "unreviewed")
    if not checks:
        return result("skipped", "no checks")
    if line is None:  # run skips an unreviewed case on purpose unless asked
        return result("skipped", NO_LINE_UNREVIEWED) if unreviewed else result("stale", "no output")
    if line.get("input_hash") != input_hash(case):
        return result("stale", "input changed")
    if line.get("error") is not None:
        return result("error", checks=run_checks(checks, None), error=line["error"])
    results = run_checks(checks, line.get("output"))
    return result("pass" if all(r.passed for r in results) else "fail", checks=results)


def grade_suite(
    cases: list[dict[str, Any]], lines: dict[str, dict[str, Any]], *, include_unreviewed: bool
) -> SuiteResult:
    graded = [_grade_case(c, lines.get(c["id"]), include_unreviewed)
              for c in sorted(cases, key=lambda c: c["id"])]  # fmt: skip
    fields: dict[str, tuple[int, int]] = {}
    for case in (c for c in graded if c.outcome in GRADED):
        for name in dict.fromkeys(r.field for r in case.checks):
            ok = case.outcome != "error" and all(r.passed for r in case.checks if r.field == name)
            passed, total = fields.get(name, (0, 0))
            fields[name] = (passed + ok, total + 1)
    orphaned = sorted(set(lines) - {c["id"] for c in cases})
    return SuiteResult(graded, orphaned, fields)


# --- the baseline and the gate --------------------------------------------------------


def _states(case: CaseResult) -> dict[str, str]:
    if case.outcome == "error":
        return {r.key: "error" for r in case.checks}
    return {r.key: "pass" if r.passed else "fail" for r in case.checks}


@dataclass(frozen=True)
class Changes:
    """The gate table's rows: check rows as "<case> <key>", case rows as case ids."""

    regressions: list[str] = field(default_factory=list[str])
    improvements: list[str] = field(default_factory=list[str])
    changed: list[str] = field(default_factory=list[str])
    new: list[str] = field(default_factory=list[str])
    removed: list[str] = field(default_factory=list[str])
    skipped_in_baseline: list[str] = field(default_factory=list[str])
    stale: list[str] = field(default_factory=list[str])


def compare(result: SuiteResult, baseline: dict[str, dict[str, str]] | None) -> Changes:
    """How the results differ from the baseline. With no baseline only stale cases count.
    Unreviewed cases, graded only on request, are never compared."""
    changes = Changes()
    for case in result.cases:
        if case.outcome == "stale":
            changes.stale.append(case.id)
        if baseline is None or (case.unreviewed and case.outcome != "skipped"):
            continue
        if case.outcome == "skipped":
            if case.id in baseline:
                changes.skipped_in_baseline.append(case.id)
        elif case.outcome in GRADED:
            if case.id not in baseline:
                changes.new.append(case.id)
                continue
            old, now = baseline[case.id], _states(case)
            if old.keys() != now.keys():
                changes.changed.append(case.id)
            for key in sorted(old.keys() & now.keys()):
                if old[key] == "pass" and now[key] != "pass":
                    changes.regressions.append(f"{case.id} {key}")
                elif old[key] != "pass" and now[key] == "pass":
                    changes.improvements.append(f"{case.id} {key}")
    if baseline is not None:
        changes.removed.extend(sorted(baseline.keys() - {c.id for c in result.cases}))
    return changes


def exit_code(changes: Changes, command: Literal["run", "grade"]) -> int:
    fails = [changes.regressions, changes.changed, changes.stale]
    if command == "grade":
        fails += [changes.improvements, changes.new, changes.removed]
    return 1 if any(fails) else 0


class StaleError(Exception):
    def __init__(self, ids: list[str]) -> None:
        super().__init__("stale: " + ", ".join(ids))
        self.ids = ids


def baseline_from(result: SuiteResult) -> dict[str, dict[str, str]]:
    """The baseline the results make: reviewed, graded cases only. Refuses (StaleError)
    while any case is stale, since its outputs say nothing about its checks."""
    if stale := [c.id for c in result.cases if c.outcome == "stale"]:
        raise StaleError(stale)
    return {c.id: _states(c) for c in result.cases if c.outcome in GRADED and not c.unreviewed}
