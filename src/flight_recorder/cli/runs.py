"""`afr runs list`, `afr runs show` and `afr replay`: look at recorded runs (docs/cli.md).

The store is imported inside the commands, so other `afr` commands start without loading
SQLAlchemy.
"""

from __future__ import annotations

import errno
import os
import re
import sys
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.console import Console
from rich.text import Text

from flight_recorder.cli import render

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from flight_recorder.store.models import Run
    from flight_recorder.store.read import RunSummary, SpanNode

runs_app = typer.Typer(help="Recorded runs: list them and show one.", no_args_is_help=True)

KINDS = ("agent", "llm", "tool", "step", "other")
RELATIVE = re.compile(r"(\d+)([mhdw])")
UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
TIME_FORMS = "use 30m, 2h, 3d or 1w ago, a date (2026-11-10) or 2026-11-10T14:30"
RUN_HELP = "last, a run id prefix, or the session or trace id."


def parse_when(value: str, now: datetime) -> datetime:
    """`30m`, `2h`, `3d` or `1w` ago; a date (local midnight); or a local date and time."""
    value = value.strip()
    try:
        if match := RELATIVE.fullmatch(value):
            return now - timedelta(**{UNITS[match[2]]: int(match[1])})
        try:
            day = date.fromisoformat(value)
        except ValueError:
            at = datetime.fromisoformat(value)
            return at if at.tzinfo else at.astimezone()
        return datetime.combine(day, time()).astimezone()
    except ValueError:
        raise typer.BadParameter(f"{value!r}: {TIME_FORMS}") from None
    except (OverflowError, OSError):  # too far back or ahead (OSError: Windows, pre-1970)
        raise typer.BadParameter(f"{value!r} is out of range") from None


def _choice(value: str | None, allowed: Sequence[str], option: str) -> str | None:
    if value is not None and value not in allowed:
        raise typer.BadParameter(f"{value!r}: use one of {', '.join(allowed)}", param_hint=option)
    return value


ATTR_HELP = "KEY=VALUE: a run attribute equals VALUE. Repeat a key for any of several values."


def parse_attrs(values: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """`--attr KEY=VALUE` options: each key with the values it may have."""
    attrs: dict[str, tuple[str, ...]] = {}
    for value in values:
        key, equals, wanted = value.partition("=")
        if not equals or not key:
            raise typer.BadParameter(f"{value!r}: use KEY=VALUE", param_hint="--attr")
        attrs[key] = (*attrs.get(key, ()), wanted)
    return attrs


@contextmanager
def _quiet_when_piped_reader_leaves() -> Generator[None]:
    """`afr ... | head` closes the pipe early: stop quietly instead of a traceback."""
    try:
        yield
    except OSError as error:
        if error.errno not in (errno.EPIPE, errno.EINVAL):  # EINVAL: a closed pipe on Windows
            raise
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())  # no flush error at exit
        raise typer.Exit(code=1) from None


@contextmanager
def open_store(*, readonly: bool = True) -> Generator[Session]:
    """A session on AFR_DATABASE_URL (read-only unless asked), with friendly errors for the
    usual problems. A writable session is the caller's to commit."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    from flight_recorder.store.db import database_url, pending_migrations

    # Fail in seconds when Postgres is down, not after the OS's connect retries (2 minutes)
    engine = create_engine(database_url(), connect_args={"connect_timeout": 3})
    try:
        try:
            behind = pending_migrations(engine)
        except OperationalError as error:
            typer.echo(
                "error: can't reach the run store; is Postgres running (`docker compose up -d`)?",
                err=True,
            )
            raise typer.Exit(code=2) from error
        if behind:
            typer.echo("error: the database isn't up to date; run `afr db upgrade` first", err=True)
            raise typer.Exit(code=2)
        with _quiet_when_piped_reader_leaves(), Session(engine) as session:
            session.connection(execution_options={"postgresql_readonly": readonly})
            yield session
    finally:
        engine.dispose()


def _summaries(session: Session, runs: Sequence[Run]) -> list[RunSummary]:
    from flight_recorder.store.read import RunFilter, find_runs

    where = RunFilter(limit=len(runs))
    return find_runs(session, where, datetime.now(UTC), ids=[r.id for r in runs])


def resolve_ref(session: Session, ref: str) -> RunSummary:
    from flight_recorder.store.read import AmbiguousRunError, RunNotFoundError, resolve_run

    try:
        run = resolve_run(session, ref)
    except AmbiguousRunError as error:
        typer.echo(f"error: {error}; use more of the id:", err=True)
        out = render.console()
        out.stderr = True
        out.print(render.runs_table(_summaries(session, error.matches), out.width))
        raise typer.Exit(code=2) from error
    except RunNotFoundError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=1) from error
    (summary,) = _summaries(session, [run])
    return summary


@runs_app.command("list")
def runs_list(
    source: Annotated[str | None, typer.Option(help="otel or claude_code.")] = None,
    agent: Annotated[str | None, typer.Option(help="The agent or service name, exactly.")] = None,
    status: Annotated[
        str | None,
        typer.Option(
            help="ok, error or unset; running (no end yet, changed in the last 30 minutes) "
            "or unfinished (no end, older)."
        ),
    ] = None,
    since: Annotated[
        str | None, typer.Option(help="Started at or after: 2h, 3d, 2026-11-10...")
    ] = None,
    until: Annotated[str | None, typer.Option(help="Started before: the same forms.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=1000, help="How many runs.")] = 20,
    attr: Annotated[list[str] | None, typer.Option(help=ATTR_HELP)] = None,
) -> None:
    """Recorded runs, newest first."""
    from flight_recorder.store.read import RUN_STATES, RunFilter, find_runs

    now = datetime.now(UTC)
    where = RunFilter(
        source=source,
        agent=agent,
        status=_choice(status, RUN_STATES, "--status"),  # type: ignore[arg-type]
        since=parse_when(since, now) if since else None,
        until=parse_when(until, now) if until else None,
        limit=limit + 1,  # one more says whether there are more
        attrs=parse_attrs(attr or []),
    )
    with open_store() as session:
        summaries = find_runs(session, where, now)
        out = render.console()
        if not summaries:
            out.print("no runs match")
            return
        out.print(render.runs_table(summaries[:limit], out.width))
        if len(summaries) > limit:
            out.print(Text(f"the newest {limit}; --limit shows more", style="dim"))


@runs_app.command("show")
def runs_show(
    run: Annotated[str, typer.Argument(help=RUN_HELP)],
    kind: Annotated[
        str | None, typer.Option(help="Only spans of this kind, with their parents.")
    ] = None,
    errors: Annotated[
        bool, typer.Option("--errors", help="Only error and rejected spans, with their parents.")
    ] = False,
    depth: Annotated[
        int | None, typer.Option(min=1, help="Levels of the tree to show (at most 40).")
    ] = None,
    all_spans: Annotated[
        bool, typer.Option("--all", help="Don't fold long lists of spans.")
    ] = False,
) -> None:
    """A run's span tree, with tokens, cost and time for each span."""
    from flight_recorder.store.read import span_tree

    kind = _choice(kind, KINDS, "--kind")
    with open_store() as session:
        summary = resolve_ref(session, run)
        roots = span_tree(session, summary.run.id)
        out = render.console()
        out.print(render.run_header(summary, roots))
        shown = render.keep(roots, kind, errors)
        if shown is not None and not shown:
            out.print("no spans match")
            return
        out.print(render.span_tree(roots, depth=depth, shown=shown, fold=not all_spans))


NEXT = {"n", " ", "\r", "\n", "j", "\xe0M", "\x00M", "\x1b[C", "\x1bOC"}
BACK = {"p", "b", "k", "\xe0K", "\x00K", "\x1b[D", "\x1bOD"}
QUIT = {"q", "\x1b", "\x03"}


def replay_steps(
    out: Console,
    nodes: list[SpanNode],
    load: Callable[[SpanNode], tuple[Any, Any]],
    keys: Iterator[str],
    *,
    started: datetime | None = None,
    full: bool = False,
    clear: bool = True,
) -> int:
    """Show one step at a time, moving on each key; returns how many steps were seen.
    `started` (the run's start) is where step times count from."""
    began = started or nodes[0].started_at
    # Fit a step on one screen, so its header doesn't scroll away: input and output share
    # what's left after the header, the span and the key hint
    lines = max(5, (out.height - 12) // 2)
    chars = min(render.CONTENT_CHARS, lines * max(20, out.width - 4))
    at, seen = 0, {0}
    while True:
        if clear:
            out.clear()
        node = nodes[at]
        out.print(
            render.step(
                node,
                at + 1,
                len(nodes),
                began,
                load(node),
                full=full,
                max_lines=lines,
                max_chars=chars,
            )
        )
        out.print(Text("n/Enter/→ next · p/← back · q quit", style="dim"))
        key = next(keys, "q")
        if key in QUIT:
            return len(seen)
        if key in BACK:
            at = max(0, at - 1)
        elif key in NEXT:
            if at == len(nodes) - 1:
                return len(seen)
            at += 1
        seen.add(at)


def _keys() -> Iterator[str]:
    import click

    while True:
        try:
            yield click.getchar()
        except (KeyboardInterrupt, EOFError):
            yield "q"


def replay(
    run: Annotated[str, typer.Argument(help=RUN_HELP)],
    kind: Annotated[str | None, typer.Option(help="Only step through spans of this kind.")] = None,
    full: Annotated[
        bool, typer.Option("--full", help="Don't cut long inputs and outputs.")
    ] = False,
    content: Annotated[
        bool, typer.Option("--content", help="Off a terminal: print inputs and outputs too.")
    ] = False,
) -> None:
    """Step through a run's spans in time order, with each one's input and output.

    In a terminal: Enter, n or → for the next step, p or ← to go back, q to quit. Piped,
    it prints a line per step; --content adds inputs and outputs. Recorded content is
    untrusted: escape codes are shown, not run, but text can still say anything.
    """
    from flight_recorder.store.read import span_content, span_tree, steps

    kind = _choice(kind, KINDS, "--kind")
    with open_store() as session:
        summary = resolve_ref(session, run)
        roots = span_tree(session, summary.run.id)
        nodes = steps(roots)
        if kind:
            nodes = [n for n in nodes if n.kind == kind]
        out = render.console()
        if not nodes:
            out.print("no spans to replay")
            return

        def load(node: SpanNode) -> tuple[Any, Any]:
            found = span_content(session, node.id)
            return found.input, found.output

        started = summary.run.started_at
        if out.is_terminal and sys.stdin.isatty():
            seen = replay_steps(out, nodes, load, _keys(), started=started, full=full)
            out.print(Text(f"{seen} of {len(nodes)} steps seen", style="dim"))
            return
        out.print(render.run_header(summary, roots))
        for i, node in enumerate(nodes, 1):
            shown = load(node) if content else None
            out.print(render.step(node, i, len(nodes), started, shown, full=full))
