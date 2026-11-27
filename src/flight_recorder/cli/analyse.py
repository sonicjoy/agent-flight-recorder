"""`afr analyse`: an agent's failed runs, grouped by how they went wrong, as eval cases you keep
(#22, docs/analyst.md)."""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.text import Text

from flight_recorder.cli import render
from flight_recorder.cli.case import DEFAULT_ROOT, DEFAULT_SUITE
from flight_recorder.cli.runs import ATTR_HELP, open_store, parse_attrs

MAX_GROUPS = 10
MAX_SHOWN = 5


def interactive() -> bool:
    """Whether a person can answer: approval is never automatic. Tests replace it."""
    return render.console().is_terminal and sys.stdin.isatty()


def _kept(answer: str, count: int) -> set[int] | None:
    """`1,3,4` as group numbers; empty keeps none; None for anything else."""
    if not answer.strip():
        return set()
    parts = [part.strip() for part in answer.split(",")]
    try:
        picked = {int(part) for part in parts if part}  # "1 0" is refused, not read as 10
    except ValueError:
        return None
    return picked if picked and all(1 <= n <= count for n in picked) else None


def analyse(
    agent: Annotated[
        str, typer.Option(help="The agent or service name, exactly.", show_default=False)
    ],
    attr: Annotated[
        list[str] | None,
        typer.Option(
            help=f"{ATTR_HELP} Runs matching every one count as failed, as do error "
            "runs; for the triager: triager.grade=wrong."
        ),
    ] = None,
    suite: Annotated[str, typer.Option(help="The suite folder for the cases.")] = DEFAULT_SUITE,
    root: Annotated[Path, typer.Option(help="The cases folder.")] = DEFAULT_ROOT,
    model: Annotated[
        str | None,
        typer.Option(help="The model that names groups [default: AFR_LLM_CHEAP_MODEL]."),
    ] = None,
) -> None:
    """Group an agent's failed runs by exactly how they went wrong, name each group with a
    cheap model, and write a case for each representative run of the groups you keep."""
    from flight_recorder.analyst.core import VERSION
    from flight_recorder.analyst.core import analyse as run_core
    from flight_recorder.analyst.items import items_for
    from flight_recorder.analyst.select import select_failed
    from flight_recorder.analyst.write import LOG_DIR, WriteFailure, write_groups, write_log
    from flight_recorder.evals.cases import CaseError, check_id, draft_case, preview
    from flight_recorder.llm import PROVIDERS, ModelSpecError, api_key_for, parse_spec
    from flight_recorder.redact import default_redactor
    from flight_recorder.settings import Settings

    def fail(message: str, code: int) -> typer.Exit:
        typer.echo(f"error: {message}", err=True)
        return typer.Exit(code=code)

    if not interactive():
        raise fail("afr analyse asks which groups to keep: run it in a terminal", 2)
    try:
        check_id(suite, "suite")
    except CaseError as error:
        raise typer.BadParameter(str(error), param_hint="--suite") from None
    attrs = parse_attrs(attr or [])
    settings = Settings()
    spec = model or settings.llm_cheap_model
    try:
        parsed = parse_spec(spec)
    except ModelSpecError as error:
        raise typer.BadParameter(str(error), param_hint="--model") from None
    if api_key_for(parsed.provider, settings) is None:
        raise fail(f"{parsed} needs {PROVIDERS[parsed.provider][1]} in the environment or .env", 2)
    redactor = default_redactor()
    out = render.console()
    now = datetime.now(UTC)
    with open_store() as session:
        selection = select_failed(session, agent, attrs, now)
        counts = ", ".join(f"{n} by {reason}" for reason, n in sorted(selection.counts.items()))
        out.print(render.text(f"{len(selection.failed)} failed runs ({counts or 'none'}); "
                              f"{selection.vetoed} left out for a truth marked bad"))  # fmt: skip
        if not selection.failed:
            raise fail(f"no failed runs for {agent!r}", 1)
        items, empty = items_for(session, selection.failed, redactor)
        if empty:
            out.print(render.text(f"{empty} failed runs record no difference to group"))
        if not items:
            raise fail("nothing to group", 1)
        params = {"model": str(parsed), "max_groups": MAX_GROUPS, "max_shown": MAX_SHOWN}
        result = run_core({"version": VERSION, "items": items, "params": params})
        groups: list[dict[str, Any]] = result["groups"]
        for group in groups:  # a provider's error text is not ours to print or log as is
            if group["error"]:
                group["error"] = str(redactor.redact(group["error"]).value)
        run_of = {entry["id"]: entry["run"] for entry in items}
        for number, group in enumerate(groups, 1):
            head = Text(f"\n{number}. ", style="bold")
            head.append_text(render.text(f"{group['name']}  ({group['signature']}, "
                                         f"{len(group['members'])} items)", "bold"))  # fmt: skip
            out.print(head)
            if group["error"]:
                out.print(render.text(f"   not named: {group['error']}", "yellow"))
            elif group["cause"]:
                out.print(render.text(f"   {group['cause']}"))
            for item_id in group["representatives"]:
                try:
                    draft = draft_case(session, uuid.UUID(run_of[item_id]), suite=suite,
                                       redactor=redactor)  # fmt: skip
                except CaseError as error:
                    out.print(render.text(f"   (no case: {error})", "dim"))
                    continue
                out.print(Text(preview(draft.case), style="dim"))
        if result["dropped_groups"]:
            out.print(render.text(f"\n{result['dropped_groups']} smaller groups "
                                  f"({result['dropped_items']} items) not shown"))  # fmt: skip
        while (kept := _kept(typer.prompt("Keep which groups? (e.g. 1,3; empty keeps none)",
                                          default="", show_default=False),
                             len(groups))) is None:  # fmt: skip
            out.print(f"give group numbers from 1 to {len(groups)}, comma-separated")
        chosen = [g for n, g in enumerate(groups, 1) if n in kept]
        for group in chosen:
            if not group["representatives"]:
                out.print(render.text(f"group {group['name']} has no representatives (it wasn't "
                                      "named), so it writes no case", "yellow"))  # fmt: skip
        try:
            written = write_groups(session, chosen, run_of, root=root, suite=suite,
                                   redactor=redactor)  # fmt: skip
            failure = None
        except WriteFailure as error:
            written, failure = error.written, error
    for path in written.paths:
        out.print(render.text(f"wrote {path.as_posix()}"))
    for run_id, reason in written.skipped:
        out.print(render.text(f"skipped run {run_id[:8]}: {reason}", "dim"))
    try:
        log = write_log(LOG_DIR, params=params, groups=groups, kept=kept, written=written,
                        now=now)  # fmt: skip
    except OSError as error:
        raise fail(f"can't write the decisions log in {LOG_DIR.as_posix()}: "
                   f"{error.strerror or error}", 1) from None  # fmt: skip
    out.print(render.text(f"log: {log.as_posix()}"))
    if failure is not None:
        raise fail(str(failure), 1)
