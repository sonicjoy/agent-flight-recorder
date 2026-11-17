"""Check the eval harness's grading against the triager's own grader (#15).

The triager graded each of its 200 corpus runs per field when it ran (`triager.grade.<field>`,
from its own `grading.py`). This script rebuilds those runs as a scratch suite (one case per
run, the run's decisions as its output, its truth as checks), grades it with the harness's
grading code, and prints where the two graders agree and where they don't. The two share no
code, so agreement means something. No model calls, and it only reads the store.

    uv run python scripts/eval_agreement.py

It always exits 0: its output is the evidence, saved in the PR. See docs/evals/harness.md.
"""

from __future__ import annotations

import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import select

from flight_recorder.cli.runs import open_store
from flight_recorder.evals.cases import dump
from flight_recorder.evals.grade import grade_suite, input_hash
from flight_recorder.evals.run import load_suite, read_outputs, write_outputs
from flight_recorder.payloads import NONE_TEXT
from flight_recorder.store.models import Run

GROUPS = ("real-mini", "synthetic-nano", "injected-mini", "real-gemini")  # docs/evals/corpus.md
FIELDS = ("labels", "milestone", "duplicate_of", "regression_of")
SUITE = "agreement"


def decoded(value: Any) -> Any:
    """The recorder's "nothing" as null; null, missing and [] then all mean nothing."""
    return None if value == NONE_TEXT else value


def accepted(field: str, values: list[str]) -> list[Any]:
    """Accepted answers: one string each; an accepted label set is "a, b", split back."""
    if field != "labels":
        return [decoded(v) for v in values]
    return [None if v == NONE_TEXT else [s.strip() for s in v.split(", ") if s.strip()]
            for v in values]  # fmt: skip


def corpus_runs() -> list[tuple[str, dict[str, Any]]]:
    """The latest attempt of each corpus run, as (run id, attributes)."""
    latest: dict[tuple[Any, ...], tuple[int, str, dict[str, Any]]] = {}
    with open_store(readonly=True) as session:
        for run in session.scalars(select(Run)):
            attrs = dict(run.attributes or {})
            if attrs.get("triager.batch.group") not in GROUPS:
                continue
            key = tuple(attrs.get(f"triager.{k}") for k in ("batch.group", "case", "batch.repeat"))
            attempt = int(attrs.get("triager.batch.attempt") or 0)
            if key not in latest or attempt > latest[key][0]:
                latest[key] = (attempt, str(run.id), attrs)
    return sorted((run_id, attrs) for _, run_id, attrs in latest.values())


def case_and_output(run_id: str, attrs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    output: dict[str, Any] = {}
    for field in FIELDS:
        if f"triager.decided.{field}" in attrs:
            output[field] = decoded(attrs[f"triager.decided.{field}"])
        if f"triager.expected.{field}" not in attrs:
            continue
        truth = decoded(attrs[f"triager.expected.{field}"])
        others = accepted(field, attrs.get(f"triager.accepted.{field}") or [])
        if others:
            checks.append({"type": "one_of", "field": field, "values": [truth, *others]})
        else:
            checks.append({"type": "equals", "field": field, "value": truth})
    case = {"id": "r-" + run_id.replace("-", "")[:12], "input": {"run": run_id}, "checks": checks}
    return case, output


def main() -> None:
    runs = corpus_runs()
    graded = [(i, a) for i, a in runs if a.get("triager.grade") in ("correct", "wrong")]
    print(f"corpus runs (latest attempt): {len(runs)}; with a decision: {len(graded)}")
    with_accepted = sum(any(k.startswith("triager.accepted.") for k in a) for _, a in graded)
    print(f"runs with accepted answers: {with_accepted}")

    def consistent(attrs: dict[str, Any]) -> bool:
        marks = [attrs.get(f"triager.grade.{f}") for f in FIELDS if f"triager.grade.{f}" in attrs]
        return (attrs["triager.grade"] == "correct") == all(m == "ok" for m in marks)

    mixed = [i for i, a in graded if not consistent(a)]
    print(f"mixed truth (grade isn't the AND of its fields), left out: {len(mixed)}")
    for run_id in mixed:
        print(f"  {run_id}")
    kept = [(i, a) for i, a in graded if i not in mixed]

    with tempfile.TemporaryDirectory(prefix="afr-agreement-") as tmp:
        folder = Path(tmp) / SUITE
        folder.mkdir()
        attrs_by_case: dict[str, tuple[str, dict[str, Any]]] = {}
        lines: dict[str, dict[str, Any]] = {}
        for run_id, attrs in kept:
            case, output = case_and_output(run_id, attrs)
            (folder / f"{case['id']}.yaml").write_text(dump(case), encoding="utf-8")
            attrs_by_case[case["id"]] = (run_id, attrs)
            lines[case["id"]] = {"case": case["id"], "input_hash": input_hash(case),
                                 "output": output, "error": None}  # fmt: skip
        write_outputs(folder, {"agent": "triager (recorded)"}, lines)
        suite = load_suite(Path(tmp), SUITE)  # through the files, as `afr eval grade` reads them
        _, saved = read_outputs(folder)
        result = grade_suite(suite.cases, saved, include_unreviewed=False)

    agree: Counter[str] = Counter()
    total: Counter[str] = Counter()
    disagreements: list[str] = []
    for case in result.cases:
        run_id, attrs = attrs_by_case[case.id]
        for check in case.checks:
            theirs = attrs.get(f"triager.grade.{check.field}")
            if theirs is None:
                continue
            ours = "ok" if check.passed else "wrong"
            total[check.field] += 1
            if ours == theirs:
                agree[check.field] += 1
            else:
                disagreements.append(f"  {run_id} {check.field}: decided {check.actual!r}, "
                                     f"expected {check.expected!r}; harness {ours}, "
                                     f"triager {theirs}")  # fmt: skip
    print("agreement per field:")
    for field in FIELDS:
        print(f"  {field}: {agree[field]}/{total[field]}")
    print(f"disagreements: {len(disagreements)}")
    print("\n".join(disagreements))


if __name__ == "__main__":
    main()
