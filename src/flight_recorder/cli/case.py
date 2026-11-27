"""`afr case`: turn a recorded run into an eval case (#59, docs/cli.md)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.text import Text

from flight_recorder.cli import render
from flight_recorder.cli.runs import RUN_HELP, open_store, resolve_ref

case_app = typer.Typer(help="Eval cases from recorded runs.", no_args_is_help=True)

DEFAULT_SUITE = "drafts"  # cases from runs wait here until they're reviewed and moved
DEFAULT_ROOT = Path("evals/cases")
WARNING = (
    "Read it before it goes into git: redaction catches secret shapes, not everything "
    "personal (home paths, names, hostnames, passwords written in prose)."
)


@case_app.command("new")
def new(
    from_run: Annotated[str, typer.Option("--from-run", help=RUN_HELP, show_default=False)],
    suite: Annotated[str, typer.Option(help="The suite folder.")] = DEFAULT_SUITE,
    case_id: Annotated[
        str | None, typer.Option("--id", help="The case id (default: run-<first 8 of its id>).")
    ] = None,
    root: Annotated[Path, typer.Option(help="The cases folder.")] = DEFAULT_ROOT,
    fake: Annotated[
        bool, typer.Option("--fake", help="Swap each redacted value for a fixed fake.")
    ] = False,
    keep: Annotated[
        bool,
        typer.Option(
            "--keep-placeholders",
            help="Keep redacted values, without fingerprints; marks the case.",
        ),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Write without asking.")] = False,
) -> None:
    """Write a run as an eval case: its input, the right answer from its labels (else its
    recorded truth, else what the agent did, marked unreviewed), and a check per field. Prints
    it first."""
    from flight_recorder.evals.cases import (
        CaseError,
        check_id,
        draft_case,
        faked,
        kept,
        preview,
        problems,
        write_case,
    )
    from flight_recorder.redact import default_redactor

    if fake and keep:
        raise typer.BadParameter("use one of them", param_hint="--fake / --keep-placeholders")
    try:  # before opening the store: a bad id is a usage error (exit 2), not a failure
        check_id(suite, "suite")
        if case_id is not None:
            check_id(case_id, "case id")
    except CaseError as error:
        raise typer.BadParameter(str(error)) from None
    out = render.console()
    with open_store() as session:
        summary = resolve_ref(session, from_run)
        try:
            draft = draft_case(session, summary.run.id, suite=suite, case_id=case_id,
                               redactor=default_redactor())  # fmt: skip
        except CaseError as error:
            typer.echo(f"error: {error}", err=True)
            raise typer.Exit(code=1) from error
    if fake:
        draft = draft.with_case(faked(draft.case))
    elif keep:
        draft = draft.with_case(kept(draft.case))
    try:
        path = draft.path(root)
    except CaseError as error:
        out.print(render.text(f"error: {error}"), soft_wrap=True)
        raise typer.Exit(code=1) from error
    heading = render.text(f"case {path}", "bold")
    if draft.local:
        heading.append(" (a Claude Code session: local/ holds a .gitignore of its own)", "yellow")
    out.print(heading, soft_wrap=True)
    out.print(Text(preview(draft.case)), soft_wrap=True)
    out.print(WARNING, style="dim", highlight=False)
    if found := problems(draft.case):
        out.print(Text("redacted values in the case:", style="bold red"))
        for value in found:
            out.print(render.text(f"  {value}"), soft_wrap=True)
        out.print("rerun with --fake (a fixed fake for each) or --keep-placeholders, "
                  "or write the case by hand", highlight=False)  # fmt: skip
        raise typer.Exit(code=1)
    if not yes:
        try:
            confirmed = typer.confirm("write it?", default=False)
        except typer.Abort:
            confirmed = False
        if not confirmed:
            out.print("not written")
            raise typer.Exit(code=1)
    try:
        path = write_case(root, draft)
    except CaseError as error:
        out.print(render.text(f"error: {error}"), soft_wrap=True)
        raise typer.Exit(code=1) from error
    out.print(render.text(f"wrote {path}"), soft_wrap=True)
