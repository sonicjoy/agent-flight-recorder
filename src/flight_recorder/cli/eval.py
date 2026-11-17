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

            header, lines = run_suite(suite, redactor=default_redactor(),
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
                notes.append(f"wrote {suite.folder / 'baseline.json'}")
                code = 0
        elif baseline is None:
            notes.append("no baseline.json yet: rerun with --accept to write one")
    except (CaseError, OSError) as error:
        message = error.strerror if isinstance(error, OSError) and error.strerror else error
        out.print(render.text(f"error: {message}"), soft_wrap=True)
        raise typer.Exit(code=2) from None
    if as_json:
        typer.echo(json.dumps(_as_json(suite, header, result, changes, code), ensure_ascii=False))
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
