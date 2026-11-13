"""`afr label` and `afr labels`: label runs with failure modes by hand (#14, docs/cli.md).

`afr label <run>` shows a run and asks for its failure modes, each with a note; `afr label --next`
walks a seeded sample of runs, skipping those already labelled. `afr labels` lists, renames
and removes modes. Only the `labels` table is written.
"""

from __future__ import annotations

import getpass
import re
import uuid
from collections.abc import Callable
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
    from flight_recorder.store.labels import ModeCount
    from flight_recorder.store.read import RunSummary, SpanContent

labels_app = typer.Typer(
    help="Failure-mode labels: list, rename and remove them.", no_args_is_help=True
)

SEED = 14
# another labeller got there first: unique violation, deadlock, serialization failure
RACED = ("23505", "40P01", "40001")
EVERY_RUN = 100_000
SKIP, QUIT = "s", "q"
NUMBER = re.compile(r"[0-9]{1,6}")  # ASCII only (str.isdigit() also takes ²), and short


class Quit(Exception):
    pass


class Skip(Exception):
    pass


class Done(Exception):
    """No more modes for this run, and stop: `q` or end of input after a mode was saved."""


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
    """The run, read top to bottom: the agent's decision next to the truth (and where the
    truth came from), what it was asked, its model and tool calls, its problem spans, its
    other attributes and its labels."""
    from flight_recorder.cli import story
    from flight_recorder.store.labels import labels_of
    from flight_recorder.store.read import span_content, span_tree

    roots = span_tree(session, summary.run.id)
    attributes = summary.run.attributes or {}

    def content(span_id: uuid.UUID) -> SpanContent:
        return span_content(session, span_id)

    out.print(render.run_header(summary, roots))
    rows = render.decision_rows(attributes)
    if rows:
        heading = Text("agent vs truth", style="bold")
        source = render.truth_source(attributes)
        if source is not None:
            heading.append(" · ")
            heading.append_text(source)
        out.print(heading)
        out.print(rows)
    for title, section in (("task", story.task(roots, content)),
                           ("steps", story.steps(roots, content))):  # fmt: skip
        if section is not None:
            out.print(Text(title, style="bold"))
            out.print(section)
    problems = render.keep(roots, None, problems_only=True)
    if problems:
        out.print(Text("problems", style="bold"))
        out.print(render.span_tree(roots, shown=problems))
    rest = render.other_attributes(attributes)
    if rest:
        out.print(Text("attributes", style="bold"))
        out.print(rest)
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


def _pick(
    out: Console,
    modes: list[ModeCount],
    already: Callable[[str], bool],
    *,
    queue: bool,
    first: bool,
) -> str | None:
    """A failure mode: an existing one by number, or a new name; None when an answer after
    the first is empty. `q` quits (Done after the first); in the queue `s` skips the run
    (first answer only)."""
    from flight_recorder.store.labels import check_mode

    skip = queue and first
    if first:
        hint = "number or new name, " + ("s to skip, " if skip else "") + "q to quit"
        question = f"failure mode ({hint})"
    else:
        question = "another mode (number or new name, Enter if none, q to quit)"
    while True:
        try:
            answer = typer.prompt(question, default=None if first else "", show_default=False)
        except typer.Abort:  # end of input or Ctrl-C: keep what's saved, say so, stop
            if first:
                raise
            raise Done from None
        answer = answer.strip()
        if not first:
            if not answer:
                return None
            if answer == QUIT:
                raise Done
            if answer == SKIP:
                why = "s skips only before the first mode" if queue else "s isn't a mode name"
                out.print(f"{why}; press Enter for no more modes")
                continue
        elif answer == QUIT:
            raise Quit
        elif skip and answer == SKIP:
            raise Skip
        if NUMBER.fullmatch(answer):
            if not 1 <= int(answer) <= len(modes):
                out.print(f"pick 1 to {len(modes)}, or type a name" if modes else "type a name")
                continue
            mode = modes[int(answer) - 1].mode
        else:
            try:
                mode = check_mode(answer)
            except ValueError as error:
                out.print(render.text(f"{error}; try again"))
                continue
        if already(mode):  # in any spelling
            out.print(render.text(f"already labelled {render.one_line(mode, 80)}; pick another"))
            continue
        return mode


def _ask_and_save(
    out: Console,
    session: Session,
    run_id: uuid.UUID,
    *,
    queue: bool,
    author: str | None,
    redactor: Redactor,
    first_note: str | None = None,
    span_id: uuid.UUID | None = None,
) -> tuple[list[str], bool]:
    """Asks for failure modes, each with an optional note (`first_note` is the first one's,
    if given), and saves each as it's answered, until an empty answer. Returns the modes
    saved, and whether to stop (`q` or end of input after a mode)."""
    from flight_recorder.store.labels import mode_counts, mode_key, modes_of

    modes = mode_counts(session)
    if modes:
        out.print(Text("modes so far:", style="bold"))
        for i, count in enumerate(modes, 1):
            out.print(render.text(f"  {i}) {render.one_line(count.mode, 80)} ({count.runs})"))
    have = modes_of(session, run_id, span_id)  # keys

    def already(mode: str) -> bool:
        return mode_key(session, mode) in have

    saved: list[str] = []
    while True:
        try:
            mode = _pick(out, modes, already, queue=queue, first=not saved)
        except Done:
            return saved, True
        if mode is None:
            return saved, False
        note = first_note if not saved else None
        if note is None:
            note = typer.prompt("note (optional)", default="", show_default=False).strip()
        have.add(mode_key(session, mode))
        stored = _save(session, run_id, mode, note, author=author, redactor=redactor,
                       span_id=span_id)  # fmt: skip
        if stored is not None:
            saved.append(stored)
        else:  # another labeller added it since the check
            out.print(render.text(f"someone just added {render.one_line(mode, 80)} to this "
                                  "run; yours wasn't saved"))  # fmt: skip


def _save(
    session: Session,
    run_id: uuid.UUID,
    mode: str,
    note: str | None,
    *,
    author: str | None,
    redactor: Redactor,
    span_id: uuid.UUID | None = None,
) -> str | None:
    """The mode as stored, or None when the run (or span) already had it."""
    from flight_recorder.store.labels import add_label

    added = add_label(session, run_id, mode, note, author=author, redactor=redactor,
                      span_id=span_id)  # fmt: skip
    session.commit()
    return added.failure_mode if added is not None else None


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
            _, stop = _ask_and_save(out, session, summary.run.id, queue=True, author=author,
                                    redactor=redactor)  # fmt: skip
        except Quit:
            return
        except Skip:
            skipped.add(todo[0])
            continue
        if stop:
            return


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
        short = str(summary.run.id)[:8]
        if mode is None:
            try:
                saved, _ = _ask_and_save(out, session, summary.run.id, queue=False, author=who,
                                         redactor=redactor, first_note=note,
                                         span_id=span_id)  # fmt: skip
            except Quit:
                return
        else:
            added = _save(session, summary.run.id, mode, note, author=who, redactor=redactor,
                          span_id=span_id)  # fmt: skip
            if added is None:
                where = f"span {span}" if span is not None else short
                out.print(render.text(f"{where} already has {render.one_line(mode, 80)}; "
                                      "not added"))  # fmt: skip
                return
            saved = [added]
        modes = ", ".join(render.one_line(m, 80) for m in saved)
        out.print(render.text(f"labelled {short}: {modes}"))


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
    from sqlalchemy.exc import DBAPIError

    from flight_recorder.store.labels import rename_mode

    new = _mode(new)
    with open_store(readonly=False) as session:
        try:
            done = rename_mode(session, old, new)
            session.flush()
        except ValueError as error:
            raise typer.BadParameter(str(error), param_hint="NEW") from None
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) not in RACED:
                raise
            session.rollback()
            typer.echo("error: the labels changed while renaming; nothing renamed, try again",
                       err=True)  # fmt: skip
            raise typer.Exit(code=1) from None
        if not sum(done):
            typer.echo(f"error: no human labels have the mode {old!r}", err=True)
            raise typer.Exit(code=1)
        session.commit()

    def labels(n: int) -> str:
        return f"{n} label{'s' if n != 1 else ''}"

    if done.respelled:
        typer.echo(f"respelled {labels(done.respelled)} {new}")
        return
    message = f"renamed {labels(done.renamed)}"
    if done.merged:
        message += (f"; {done.merged} merged into labels their runs already had (their notes "
                    "are kept there, each tagged afr-note v1)")  # fmt: skip
    typer.echo(message)


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
