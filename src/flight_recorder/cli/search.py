"""`afr embed` and `afr search`: find runs by meaning and by exact words (docs/search.md)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

import typer
from rich.table import Table
from rich.text import Text

from flight_recorder.cli import render

if TYPE_CHECKING:
    from flight_recorder.search.query import Hit

from flight_recorder.cli.runs import _choice, _store, parse_when  # pyright: ignore[reportPrivateUsage]


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def embed() -> None:
    """Embed finished runs that are new or changed, for `afr search`.

    The first run downloads the embedding model (bge-small, about 67 MB) into AFR_HOME/models.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from flight_recorder.search import embedder as embedders
    from flight_recorder.search.index import embed_runs
    from flight_recorder.store.db import database_url

    with _store():  # the same checks as the read commands: reachable, migrated
        pass
    engine = create_engine(database_url(), connect_args={"connect_timeout": 3})
    try:
        with Session(engine) as session, session.begin():
            stats = embed_runs(session, embedders.default_embedder())
    finally:
        engine.dispose()
    typer.echo(
        f"embedded {_plural(stats.runs, 'run')} ({_plural(stats.vectors, 'vector')}); "
        f"{stats.unchanged} unchanged, {stats.open_runs} still open"
        + (f", {_plural(stats.removed, 'old failure vector')} removed" if stats.removed else "")
    )


def search(
    query: Annotated[
        str, typer.Argument(help="What to look for, in your own words or exact terms.")
    ],
    source: Annotated[str | None, typer.Option(help="otel or claude_code.")] = None,
    agent: Annotated[str | None, typer.Option(help="The agent or service name, exactly.")] = None,
    status: Annotated[
        str | None, typer.Option(help="ok, error, unset, running or unfinished.")
    ] = None,
    since: Annotated[
        str | None, typer.Option(help="Started at or after: 2h, 3d, 2026-11-10...")
    ] = None,
    until: Annotated[str | None, typer.Option(help="Started before: the same forms.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=100, help="How many runs.")] = 10,
) -> None:
    """Runs that match a description: by meaning (embeddings) and by exact words."""
    from flight_recorder.search import embedder as embedders
    from flight_recorder.search.index import pending_runs
    from flight_recorder.search.query import search as run_search
    from flight_recorder.store.read import RUN_STATES, RunFilter

    if not query.strip():
        raise typer.BadParameter("say what to look for", param_hint="QUERY")
    now = datetime.now(UTC)
    where = RunFilter(
        source=source,
        agent=agent,
        status=_choice(status, RUN_STATES, "--status"),  # type: ignore[arg-type]
        since=parse_when(since, now) if since else None,
        until=parse_when(until, now) if until else None,
        limit=limit,
    )
    model = embedders.default_embedder()
    with _store() as session:
        hits = run_search(session, model, query.strip(), where, now)
        missing = pending_runs(session, model.model)
        out = render.console()
        if hits:
            out.print(_table(hits))
        else:
            out.print("no matching runs" if not missing else "nothing is embedded yet")
        if missing:
            out.print(
                Text(
                    f"{_plural(missing, 'finished run')} "
                    f"{'isn' if missing == 1 else 'aren'}'t embedded yet; run `afr embed`",
                    style="yellow",
                )
            )


def _table(hits: list[Hit]) -> Table:
    now = datetime.now().astimezone()
    table = Table(box=None, pad_edge=False, header_style="bold", collapse_padding=True)
    for column in ("id", "started", "agent", "name", "status", "match", "score"):
        table.add_column(
            column, no_wrap=column != "name", justify="right" if column == "score" else "left"
        )
    for hit in hits:
        run = hit.summary.run
        name = Text(render.one_line(run.name, 80))
        if hit.span_name:
            name.append(f"\n  failed: {render.one_line(hit.span_name, 40)}", style="dim")
            if hit.span_error:
                name.append(f": {render.one_line(hit.span_error, 100)}", style="dim")
        table.add_row(
            str(run.id)[:8],
            render.short_time(run.started_at, now),
            render.text(run.agent or run.source),
            name,
            Text(hit.summary.state, style=render.STATE_STYLE.get(hit.summary.state, "")),
            hit.matched,
            f"{hit.similarity:.2f}",
        )
    return table
