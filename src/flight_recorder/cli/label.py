"""`afr label` and `afr labels`: label runs with failure modes by hand (#14, docs/cli.md).

`afr label <run>` shows a run and asks for a failure mode and a note; `afr label --next`
walks a seeded sample of runs, skipping those already labelled. `afr labels` lists, renames
and removes modes. Only the `labels` table is written.
"""

from __future__ import annotations

import getpass
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.text import Text

from flight_recorder.cli import render
from flight_recorder.cli.runs import ATTR_HELP, RUN_HELP, open_store, parse_attrs, resolve_ref

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from flight_recorder.redact import Redactor
    from flight_recorder.store.read import RunSummary

labels_app = typer.Typer(
    help="Failure-mode labels: list, rename and remove them.", no_args_is_help=True
)

SEED = 14
EVERY_RUN = 100_000
SKIP, QUIT = "s", "q"
NUMBER = re.compile(r"[0-9]{1,6}")  # ASCII only (str.isdigit() also takes ²), and short


class Quit(Exception):
    pass


class Skip(Exception):
    pass


def _author(given: str | None) -> str | None:
    if given:
        return given
    try:
        return getpass.getuser()
    except (OSError, KeyError):  # no user name in the environment
        return None


def _mode(value: str) -> str:
    from flight_recorder.store.labels import check_mode

    try:
        return check_mode(value)
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="--mode") from None


def show_run(out: Console, session: Session, summary: RunSummary) -> None:
    """The run, its attributes (decision next to truth), its problem spans and its labels."""
    from flight_recorder.store.labels import labels_of
    from flight_recorder.store.read import span_tree

    roots = span_tree(session, summary.run.id)
    out.print(render.run_header(summary, roots))
    if summary.run.attributes:
        out.print(Text("attributes", style="bold"))
        out.print(render.run_attributes(summary.run.attributes))
    problems = render.keep(roots, None, problems_only=True)
    if problems:
        out.print(Text("problems", style="bold"))
        out.print(render.span_tree(roots, shown=problems))
    existing = labels_of(session, summary.run.id)
    if existing:
        out.print(Text("labels", style="bold"))
        for label in existing:
            line = render.text(f"- {render.one_line(label.failure_mode, 80)}")
            if label.note:
                line.append_text(render.text(f": {render.one_line(label.note, 160)}"))
            who = f"{label.source}{f', {label.author}' if label.author else ''}"
            line.append_text(render.text(f"  ({render.one_line(who, 80)})", "dim"))
            out.print(line)


def _ask(
    out: Console, session: Session, *, queue: bool, note: str | None = None
) -> tuple[str, str | None]:
    """A failure mode (an existing one by number, or a new name) and an optional note,
    unless one was given. `q` quits; in the queue `s` skips the run."""
    from flight_recorder.store.labels import check_mode, mode_counts

    modes = mode_counts(session)
    if modes:
        out.print(Text("modes so far:", style="bold"))
        for i, count in enumerate(modes, 1):
            out.print(render.text(f"  {i}) {render.one_line(count.mode, 80)} ({count.runs})"))
    hint = "number or new name, " + ("s to skip, " if queue else "") + "q to quit"
    while True:
        answer = typer.prompt(f"failure mode ({hint})").strip()
        if answer == QUIT or (queue and answer == SKIP):
            raise Quit if answer == QUIT else Skip
        if NUMBER.fullmatch(answer):
            if 1 <= int(answer) <= len(modes):
                mode = modes[int(answer) - 1].mode
                break
            out.print(f"pick 1 to {len(modes)}, or type a name" if modes else "type a name")
            continue
        try:
            mode = check_mode(answer)
            break
        except ValueError as error:
            out.print(render.text(f"{error}; try again"))
    if note is None:
        note = typer.prompt("note (optional)", default="", show_default=False).strip()
    return mode, note or None


def _save(
    session: Session,
    run_id: uuid.UUID,
    mode: str,
    note: str | None,
    *,
    author: str | None,
    redactor: Redactor,
    span_id: uuid.UUID | None = None,
) -> None:
    from flight_recorder.store.labels import add_label

    add_label(session, run_id, mode, note, author=author, redactor=redactor, span_id=span_id)
    session.commit()


def _queue(
    out: Console,
    session: Session,
    where_attrs: dict[str, tuple[str, ...]],
    agent: str | None,
    size: int | None,
    seed: int,
    by: str | None,
    *,
    author: str | None,
    redactor: Redactor,
) -> None:
    from flight_recorder.store.labels import labelled_runs, sample
    from flight_recorder.store.read import RunFilter, find_runs

    now = datetime.now(UTC)
    where = RunFilter(agent=agent, attrs=where_attrs, limit=EVERY_RUN)
    runs = {str(s.run.id): s for s in find_runs(session, where, now)}
    population = [
        (run_id, str(s.run.attributes.get(by, "")) if by else "") for run_id, s in runs.items()
    ]
    picked = sample(population, size if size is not None else len(population), seed)
    skipped: set[str] = set()
    while True:
        done = {str(r) for r in labelled_runs(session, [uuid.UUID(r) for r in picked])}
        todo = [r for r in picked if r not in done and r not in skipped]
        if not todo:
            message = (
                f"all {len(picked)} labelled"
                if len(done) == len(picked)
                else (f"{len(done)} of {len(picked)} labelled; the rest were skipped")
            )
            out.print(message if picked else "no runs match")
            return
        out.print(Text(f"\n{len(done)} of {len(picked)} labelled", style="bold"))
        summary = runs[todo[0]]
        show_run(out, session, summary)
        try:
            mode, note = _ask(out, session, queue=True)
        except Quit:
            return
        except Skip:
            skipped.add(todo[0])
            continue
        _save(session, summary.run.id, mode, note, author=author, redactor=redactor)


def label(
    run: Annotated[str | None, typer.Argument(help=RUN_HELP, show_default=False)] = None,
    next_run: Annotated[
        bool, typer.Option("--next", help="Walk the sample, skipping runs already labelled.")
    ] = False,
    mode: Annotated[str | None, typer.Option(help="The failure mode; asked if not given.")] = None,
    note: Annotated[
        str | None, typer.Option(help="What happened (redacted before storing).")
    ] = None,
    span: Annotated[
        str | None, typer.Option(help="Label this span of the run (an id prefix).")
    ] = None,
    author: Annotated[
        str | None, typer.Option(help="Who is labelling (default: the OS user).")
    ] = None,
    attr: Annotated[list[str] | None, typer.Option(help=f"With --next: {ATTR_HELP}")] = None,
    agent: Annotated[str | None, typer.Option(help="With --next: the agent name, exactly.")] = None,
    sample_size: Annotated[
        int | None, typer.Option("--sample", min=1, help="With --next: label only N of the runs.")
    ] = None,
    seed: Annotated[int, typer.Option(help="With --next: the sample's seed.")] = SEED,
    by: Annotated[
        str | None,
        typer.Option(help="With --next: sample each value of this attribute in proportion."),
    ] = None,
) -> None:
    """Label a run with a failure mode and a note, or walk a sample of runs with --next."""
    from flight_recorder.redact import default_redactor
    from flight_recorder.store.labels import SpanNotInRunError, span_of_run

    if (run is None) == (not next_run):
        raise typer.BadParameter("give a run, or --next", param_hint="RUN")
    if next_run and (mode is not None or note is not None or span is not None):
        raise typer.BadParameter("--next asks for each run's mode and note", param_hint="--next")
    if mode is not None:
        mode = _mode(mode)
    attrs = parse_attrs(attr or [])
    who = _author(author)
    redactor = default_redactor()
    out = render.console()
    with open_store(readonly=False) as session:
        if next_run:
            _queue(out, session, attrs, agent, sample_size, seed, by, author=who, redactor=redactor)
            return
        assert run is not None
        summary = resolve_ref(session, run)
        span_id = None
        if span is not None:
            try:
                span_id = span_of_run(session, summary.run.id, span)
            except SpanNotInRunError as error:
                typer.echo(f"error: {error}", err=True)
                raise typer.Exit(code=1) from error
        show_run(out, session, summary)
        if mode is None:
            try:
                mode, note = _ask(out, session, queue=False, note=note)
            except Quit:
                return
        _save(session, summary.run.id, mode, note, author=who, redactor=redactor, span_id=span_id)
        out.print(
            f"labelled {str(summary.run.id)[:8]}: {render.one_line(mode, 80)}", highlight=False
        )


@labels_app.command("list")
def labels_list(
    source: Annotated[str, typer.Option(help="human, judge or analyst.")] = "human",
) -> None:
    """Each failure mode with how many runs have it and the first one labelled."""
    from flight_recorder.store.labels import labelled_count, mode_counts
    from flight_recorder.store.models import LABEL_SOURCES

    if source not in LABEL_SOURCES:
        raise typer.BadParameter(f"use one of {', '.join(LABEL_SOURCES)}", param_hint="--source")

    with open_store() as session:
        counts = mode_counts(session, source)
        out = render.console()
        if not counts:
            out.print("no labels yet")
            return
        width = max(len(render.clean(c.mode)) for c in counts)
        out.print(f"{'mode':<{width}}  runs  labels  example", style="dim", highlight=False)
        for c in counts:
            out.print(
                render.text(
                    f"{render.clean(c.mode):<{width}}  {c.runs:>4}  {c.labels:>6}  "
                    f"{str(c.example)[:8]}"
                )
            )
        total = sum(c.labels for c in counts)
        out.print(f"{total} labels on {labelled_count(session, source)} runs", highlight=False)


@labels_app.command("rename")
def labels_rename(
    old: Annotated[str, typer.Argument(help="The mode to rename.")],
    new: Annotated[str, typer.Argument(help="Its new name; an existing mode merges them.")],
) -> None:
    """Rename a failure mode on human labels, or merge it into another."""
    from flight_recorder.store.labels import rename_mode

    new = _mode(new)
    with open_store(readonly=False) as session:
        changed = rename_mode(session, old, new)
        if not changed:
            typer.echo(f"error: no human labels have the mode {old!r}", err=True)
            raise typer.Exit(code=1)
        session.commit()
    typer.echo(f"renamed {changed} label{'s' if changed != 1 else ''}")


@labels_app.command("remove")
def labels_remove(
    run: Annotated[str, typer.Argument(help=RUN_HELP)],
    mode: Annotated[str | None, typer.Option(help="Only labels with this mode.")] = None,
) -> None:
    """Delete a run's human labels."""
    from flight_recorder.store.labels import remove_labels

    with open_store(readonly=False) as session:
        summary = resolve_ref(session, run)
        removed = remove_labels(session, summary.run.id, mode)
        session.commit()
    typer.echo(f"removed {removed} label{'s' if removed != 1 else ''}")
