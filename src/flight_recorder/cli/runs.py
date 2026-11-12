"""`afr runs list`, `afr runs show` and `afr replay`: look at recorded runs (docs/cli.md)."""

from __future__ import annotations

import errno
import os
import re
import sys
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.text import Text

from flight_recorder.cli import render
from flight_recorder.store.read import (
    RUN_STATES,
    AmbiguousRunError,
    RunFilter,
    RunNotFoundError,
    RunSummary,
    SpanNode,
)

runs_app = typer.Typer(help="Recorded runs: list them and show one.", no_args_is_help=True)

KINDS = ("agent", "llm", "tool", "step", "other")
RELATIVE = re.compile(r"(\d+)([mhdw])")
UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_when(value: str, now: datetime) -> datetime:
    """`30m`, `2h`, `3d` or `1w` ago; a date (local midnight); or a local date and time."""
    value = value.strip()
    if match := RELATIVE.fullmatch(value):
        return now - timedelta(**{UNITS[match[2]]: int(match[1])})
    try:
        day = date.fromisoformat(value)
    except ValueError:
        pass
    else:
        return datetime.combine(day, time()).astimezone()
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter(
            f"{value!r}: use 30m, 2h, 3d or 1w ago, a date (2026-11-10) or 2026-11-10T14:30"
        ) from None
    return at if at.tzinfo else at.astimezone()


@contextmanager
def _store() -> Generator[Any]:
    """A read-only session on AFR_DATABASE_URL, with friendly errors for the usual problems."""
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
        with Session(engine) as session:
            session.connection(execution_options={"postgresql_readonly": True})
            yield session
    except OSError as error:  # the reader went away: `afr ... | head`
        if error.errno not in (errno.EPIPE, errno.EINVAL):  # EINVAL: a closed pipe on Windows
            raise
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())  # no flush error at exit
        raise typer.Exit(code=1) from None
    finally:
        engine.dispose()


def _resolve(session: Any, ref: str) -> RunSummary:
    from flight_recorder.store.read import find_runs, resolve_run, run_state

    try:
        run = resolve_run(session, ref)
    except AmbiguousRunError as error:
        typer.echo(f"error: {error}; use more of the id:", err=True)
        out = render.console()
        out.stderr = True
        out.print(render.runs_table(_summaries(session, error.matches)))
        raise typer.Exit(code=2) from error
    except RunNotFoundError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=1) from error
    now = datetime.now(UTC)
    found = find_runs(session, RunFilter(limit=1), now, ids=[run.id])
    return found[0] if found else RunSummary(run, run_state(run, now), 0, None, False)


def _summaries(session: Any, runs: list[Any]) -> list[RunSummary]:
    from flight_recorder.store.read import find_runs

    return find_runs(
        session, RunFilter(limit=len(runs)), datetime.now(UTC), ids=[r.id for r in runs]
    )


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
) -> None:
    """Recorded runs, newest first."""
    from flight_recorder.store.read import find_runs

    if status is not None and status not in RUN_STATES:
        raise typer.BadParameter(
            f"{status!r}: use one of {', '.join(RUN_STATES)}", param_hint="--status"
        )
    now = datetime.now(UTC)
    where = RunFilter(
        source=source,
        agent=agent,
        status=status,  # type: ignore[arg-type]  # checked against RUN_STATES above
        since=parse_when(since, now) if since else None,
        until=parse_when(until, now) if until else None,
        limit=limit,
    )
    with _store() as session:
        summaries = find_runs(session, where, now)
        out = render.console()
        if not summaries:
            out.print("no runs match")
            return
        out.print(render.runs_table(summaries))
        if len(summaries) == limit:
            out.print(Text(f"the newest {limit}; --limit shows more", style="dim"))


@runs_app.command("show")
def runs_show(
    run: Annotated[str, typer.Argument(help="last, a run id prefix, or the session or trace id.")],
    kind: Annotated[
        str | None, typer.Option(help="Only spans of this kind, with their parents.")
    ] = None,
    errors: Annotated[
        bool, typer.Option("--errors", help="Only error and rejected spans, with their parents.")
    ] = False,
    depth: Annotated[int | None, typer.Option(min=1, help="Levels of the tree to show.")] = None,
    all_spans: Annotated[
        bool, typer.Option("--all", help="Don't fold long lists of spans.")
    ] = False,
) -> None:
    """A run's span tree, with tokens, cost and time for each span."""
    from flight_recorder.store.read import span_tree

    if kind is not None and kind not in KINDS:
        raise typer.BadParameter(f"{kind!r}: use one of {', '.join(KINDS)}", param_hint="--kind")
    with _store() as session:
        summary = _resolve(session, run)
        roots = span_tree(session, summary.run.id)
        out = render.console()
        out.print(render.run_header(summary, roots))
        shown = render.keep(roots, kind, errors)
        if shown is not None and not shown:
            out.print("no spans match")
            return
        out.print(render.span_tree(roots, depth=depth, shown=shown, fold=not all_spans))


NEXT = {"n", " ", "\r", "\n", "j", "\xe0M", "\x00M", "\x1b[C"}
BACK = {"p", "b", "k", "\xe0K", "\x00K", "\x1b[D"}
QUIT = {"q", "\x1b", "\x03"}


def replay_steps(
    out: Console,
    nodes: list[SpanNode],
    load: Callable[[SpanNode], tuple[Any, Any]],
    keys: Iterator[str],
    *,
    full: bool = False,
    clear: bool = True,
) -> int:
    """Show one step at a time, moving on each key; returns how many steps were seen."""
    started = nodes[0].started_at
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
                started,
                load(node),
                full=full,
                max_lines=lines,
                max_chars=chars,
            )
        )
        out.print(Text("n/Enter next · p back · q quit", style="dim"))
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
    run: Annotated[str, typer.Argument(help="last, a run id prefix, or the session or trace id.")],
    kind: Annotated[str | None, typer.Option(help="Only step through spans of this kind.")] = None,
    full: Annotated[
        bool, typer.Option("--full", help="Don't cut long inputs and outputs.")
    ] = False,
    content: Annotated[
        bool, typer.Option("--content", help="Off a terminal: print inputs and outputs too.")
    ] = False,
) -> None:
    """Step through a run's spans in time order, with each one's input and output.

    In a terminal: Enter or n for the next step, p to go back, q to quit. Piped, it prints
    a line per step; --content adds inputs and outputs. Recorded content is untrusted:
    escape codes are shown, not run, but text can still say anything.
    """
    from flight_recorder.store.read import span_content, span_tree, steps

    if kind is not None and kind not in KINDS:
        raise typer.BadParameter(f"{kind!r}: use one of {', '.join(KINDS)}", param_hint="--kind")
    with _store() as session:
        summary = _resolve(session, run)
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

        if out.is_terminal and sys.stdin.isatty():
            seen = replay_steps(out, nodes, load, _keys(), full=full)
            out.print(Text(f"{seen} of {len(nodes)} steps seen", style="dim"))
            return
        out.print(render.run_header(summary, roots))
        started = nodes[0].started_at
        for i, node in enumerate(nodes, 1):
            out.print(
                render.step(
                    node, i, len(nodes), started, load(node) if content else None, full=full
                )
            )
