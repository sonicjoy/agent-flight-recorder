"""`afr eval run` and `afr eval grade`: an agent against a suite of cases (#15, docs/cli.md)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import typer
from rich.table import Table

from flight_recorder.cli import render
from flight_recorder.cli.case import DEFAULT_ROOT

if TYPE_CHECKING:
    from rich.console import Console

    from flight_recorder.evals.grade import Changes, SuiteResult
    from flight_recorder.evals.run import Suite

eval_app = typer.Typer(help="Run an agent against eval cases and grade it.", no_args_is_help=True)

SuiteOpt = Annotated[str, typer.Option(help="The suite folder under --root.", show_default=False)]
RootOpt = Annotated[Path, typer.Option(help="The cases folder.")]
UnreviewedOpt = Annotated[
    bool, typer.Option("--include-unreviewed", help="Grade unreviewed cases too (never baselined).")
]
AcceptOpt = Annotated[
    bool, typer.Option("--accept", help="Write baseline.json from these results.")
]
JsonOpt = Annotated[bool, typer.Option("--json", help="Print one JSON object instead.")]
OUTCOMES = ("pass", "fail", "error", "stale", "skipped")
ROWS = (  # the gate table, in order: (Changes field, heading)
    ("regressions", "regressions"),
    ("improvements", "improvements"),
    ("changed", "changed checks"),
    ("new", "new cases"),
    ("removed", "removed cases"),
    ("skipped_in_baseline", "skipped (in baseline)"),
    ("stale", "stale"),
)


@eval_app.command("run")
def run(
    suite: SuiteOpt,
    agent: Annotated[
        str | None, typer.Option(help="module:function, instead of suite.yaml's agent.")
    ] = None,
    root: RootOpt = DEFAULT_ROOT,
    include_unreviewed: UnreviewedOpt = False,
    accept: AcceptOpt = False,
    as_json: JsonOpt = False,
) -> None:
    """Call the agent once per case, save outputs.jsonl, and grade it. Fails on regressions,
    edited checks and stale cases."""
    _evaluate("run", suite, root, agent, include_unreviewed, accept, as_json)


@eval_app.command("grade")
def grade(
    suite: SuiteOpt,
    root: RootOpt = DEFAULT_ROOT,
    include_unreviewed: UnreviewedOpt = False,
    accept: AcceptOpt = False,
    as_json: JsonOpt = False,
) -> None:
    """Grade the saved outputs.jsonl, calling nothing. Fails on any difference from the
    baseline: only the grading code or the cases can have changed."""
    _evaluate("grade", suite, root, None, include_unreviewed, accept, as_json)


def _evaluate(
    command: Literal["run", "grade"],
    name: str,
    root: Path,
    agent: str | None,
    include_unreviewed: bool,
    accept: bool,
    as_json: bool,
) -> None:
    from flight_recorder.evals.cases import CaseError
    from flight_recorder.evals.grade import StaleError, baseline_from, compare, exit_code
    from flight_recorder.evals.grade import grade_suite as grade_cases
    from flight_recorder.evals.run import (
        BASELINE,
        load_suite,
        read_baseline,
        read_outputs,
        run_suite,
        write_baseline,
        write_outputs,
    )

    out = render.console()
    try:
        suite = load_suite(root, name, agent=agent)
        if command == "run":
            from flight_recorder.redact import default_redactor

            try:
                redactor = default_redactor()
            except ValueError as error:  # a bad AFR_REDACTION_KEY
                raise CaseError(f"redaction key: {error}") from error
            header, lines = run_suite(suite, redactor=redactor,
                                      include_unreviewed=include_unreviewed,
                                      now=datetime.now(UTC))  # fmt: skip
            write_outputs(suite.folder, header, lines)
        else:
            header, lines = read_outputs(suite.folder)
        result = grade_cases(suite.cases, lines, include_unreviewed=include_unreviewed)
        baseline = read_baseline(suite.folder)
        changes = compare(result, baseline)
        code = exit_code(changes, command)
        notes: list[str] = []
        if accept:
            try:
                write_baseline(suite.folder, baseline_from(result))
            except StaleError as error:
                notes.append(f"not accepted: stale cases ({', '.join(error.ids)}); run the suite")
                code = 1
            else:
                notes.append(f"wrote {suite.folder / BASELINE}")
                code = 0
        elif baseline is None:
            notes.append("no baseline.json yet: rerun with --accept to write one")
    except (CaseError, OSError) as error:
        message = (
            f"{error.filename}: {error.strerror}"
            if isinstance(error, OSError) and error.strerror and error.filename
            else error
        )
        out.print(render.text(f"error: {message}"), soft_wrap=True)
        raise typer.Exit(code=2) from None
    if as_json:
        # ASCII only: no control, bidi or C1 character from an output reaches the terminal
        typer.echo(json.dumps(_as_json(suite, header, result, changes, code), ensure_ascii=True))
        for note in notes:
            typer.echo(render.clean(note), err=True)
    else:
        _report(out, suite, header, result, changes, notes)
    raise typer.Exit(code=code)


def _counts(result: SuiteResult) -> dict[str, int]:
    return {o: sum(c.outcome == o for c in result.cases) for o in OUTCOMES}


def _as_json(
    suite: Suite, header: dict[str, Any] | None, result: SuiteResult, changes: Changes, code: int
) -> dict[str, Any]:
    return {
        "suite": suite.name,
        "header": header,
        "counts": _counts(result),
        "fields": {f: {"passed": p, "graded": g} for f, (p, g) in result.fields.items()},
        "cases": [
            {
                "id": c.id,
                "outcome": c.outcome,
                "note": c.note,
                "unreviewed": c.unreviewed,
                "error": c.error,
                "checks": [vars(r) for r in c.checks],
            }
            for c in result.cases
        ],
        "changes": {field: getattr(changes, field) for field, _ in ROWS}
        | {"orphaned": result.orphaned},
        "exit_code": code,
    }


def _value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _report(
    out: Console,
    suite: Suite,
    header: dict[str, Any] | None,
    result: SuiteResult,
    changes: Changes,
    notes: list[str],
) -> None:
    run = header or {}
    version = f" {run['agent_version']}" if run.get("agent_version") else ""
    out.print(render.text(
        f"suite {suite.name} · agent {run.get('agent') or suite.agent or '?'}{version} · "
        f"model {run.get('model') or '-'} · git {run.get('git_sha') or '-'} · "
        f"ran {run.get('ran_at') or 'never'}", "bold"), soft_wrap=True)  # fmt: skip
    if run.get("agent") and suite.agent and run["agent"] != suite.agent:
        out.print(render.text(f"warning: outputs.jsonl was made by {run['agent']}, "
                              f"but suite.yaml names {suite.agent}", "yellow"),
                  soft_wrap=True)  # fmt: skip
    counts = _counts(result)
    out.print(
        f"{counts['pass']}/{len(result.cases)} passed · {counts['fail']} failed · "
        f"{counts['error']} error · {counts['stale']} stale · {counts['skipped']} skipped"
    )
    if result.fields:
        table = Table("field", "passed", "graded", box=None, pad_edge=False)
        for field, (passed, graded) in sorted(result.fields.items()):
            table.add_row(render.text(field), str(passed), str(graded))
        out.print(table)
    for field, heading in ROWS:
        if items := getattr(changes, field):
            style = "dim" if field == "skipped_in_baseline" else "bold"
            out.print(render.text(f"{heading}: {', '.join(items)}", style), soft_wrap=True)
    if result.orphaned:
        out.print(render.text(f"orphaned lines (no case): {', '.join(result.orphaned)}", "dim"),
                  soft_wrap=True)  # fmt: skip
    for case in result.cases:
        mark = " (unreviewed)" if case.unreviewed else ""
        if case.outcome == "error":
            out.print(render.text(f"{case.id}{mark}: error: {case.error}", "red"), soft_wrap=True)
        elif case.outcome in ("stale", "skipped") and case.note:
            out.print(render.text(f"{case.id}{mark}: {case.outcome}: {case.note}", "dim"),
                      soft_wrap=True)  # fmt: skip
        for check in case.checks if case.outcome == "fail" else []:
            if not check.passed:
                line = (f"{case.id}{mark} {check.key}: expected {_value(check.expected)}, "
                        f"got {_value(check.actual)} ({check.reason})")  # fmt: skip
                out.print(render.text(line, "red"), soft_wrap=True)
    for note in notes:
        out.print(render.text(note), soft_wrap=True)


# --- the LLM judge (#17, docs/designs/llm-judge.md) ---------------------------------------


def _fail(out: Console, error: Exception) -> typer.Exit:
    message = (
        f"{error.filename}: {error.strerror}"
        if isinstance(error, OSError) and error.strerror and error.filename
        else error
    )
    out.print(render.text(f"error: {message}"), soft_wrap=True)
    return typer.Exit(code=2)


@eval_app.command("judge-suite")
def judge_suite(
    items: Annotated[Path, typer.Option(help="The items file ({items: [...]}).")],
    rubric: Annotated[Path, typer.Option(help="The rubric file.")],
    suite: SuiteOpt,
    root: RootOpt = DEFAULT_ROOT,
) -> None:
    """Write a judge suite from an items file and a rubric: one case per item, with the hand
    grade as its only check. Calls no model; never writes a baseline."""
    from flight_recorder.evals.cases import CaseError, suite_dir
    from flight_recorder.evals.judge_suite import (
        ItemsError,
        build_suite,
        load_items,
        load_rubric,
        write_suite,
    )

    out = render.console()
    try:
        folder = suite_dir(root, suite, local=False)
        if suite_dir(root, suite, local=True).exists():
            raise CaseError(f"suite {suite!r} also exists under local/: keep one")
        files = build_suite(load_items(items), load_rubric(rubric))
        write_suite(folder, files)
    except ItemsError as error:
        for item, problem in error.problems:
            out.print(render.text(f"{item}: {problem}", "red"), soft_wrap=True)
        raise typer.Exit(code=1) from None
    except (CaseError, OSError) as error:
        raise _fail(out, error) from None
    out.print(render.text(f"wrote {len(files) - 1} cases to {folder}"), soft_wrap=True)


def _rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


@eval_app.command("agreement")
def agreement(
    suite: SuiteOpt,
    field: Annotated[str, typer.Option(help="The graded field, e.g. grade.", show_default=False)],
    root: RootOpt = DEFAULT_ROOT,
) -> None:
    """How a judge agrees with hand grades, from a judge suite's saved outputs: counts,
    percent agreement, Cohen's kappa and catch rate, overall and per tag. Calls no model.
    Exits 1 when a case was stale or errored (the numbers are partial)."""
    from flight_recorder.evals.agreement import agreement as measure
    from flight_recorder.evals.agreement import incomplete, pairs_from
    from flight_recorder.evals.cases import CaseError
    from flight_recorder.evals.grade import grade_suite
    from flight_recorder.evals.run import load_suite, read_outputs

    out = render.console()
    try:
        loaded = load_suite(root, suite)
        header, lines = read_outputs(loaded.folder)
        if header is None:
            raise CaseError("no outputs.jsonl: run the suite")
        result = grade_suite(loaded.cases, lines, include_unreviewed=False)
    except (CaseError, OSError) as error:
        raise _fail(out, error) from None
    pairs, by_tag, left_out = pairs_from(result, loaded.cases, field)
    table = Table("rows", "n", "agree", "kappa", "catch", "pass", "both pass", "both fail",
                  "hand pass / judge fail", "hand fail / judge pass", box=None,
                  pad_edge=False)  # fmt: skip
    for name, rows in [("all", pairs), *sorted(by_tag.items())]:
        a = measure(rows)
        kappa = "n/a" if a.kappa is None else f"{a.kappa:.2f}"
        table.add_row(render.text(name), str(a.n), _rate(a.percent), kappa, _rate(a.catch_rate),
                      _rate(a.pass_rate), str(a.both_pass), str(a.both_fail),
                      str(a.hand_pass_judge_fail), str(a.hand_fail_judge_pass))  # fmt: skip
    out.print(table)
    for case_id, reason in left_out:
        out.print(render.text(f"left out {case_id}: {reason}", "dim"), soft_wrap=True)
    raise typer.Exit(code=1 if incomplete(left_out) else 0)
