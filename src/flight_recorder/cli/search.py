"""`afr embed` and `afr search`: find runs by meaning and by exact words (docs/search.md)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.table import Table
from rich.text import Text

from flight_recorder.cli import render
from flight_recorder.cli.runs import _choice, open_store, parse_when  # pyright: ignore[reportPrivateUsage]

if TYPE_CHECKING:
    from flight_recorder.search.query import Hit

SEARCH_STATES = ("ok", "error", "unset", "unfinished")
"""Running runs aren't embedded until they settle, so they can't be searched."""
COMMIT_EVERY = 50  # runs: an interrupted `afr embed` keeps what it did


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def embed() -> None:
    """Embed settled runs (finished, or quiet for 30 minutes) that are new or changed.

    The first run downloads the embedding model (bge-small, about 67 MB) into AFR_HOME/models.
    """
    from flight_recorder.search import embedder as embedders
    from flight_recorder.search.index import embed_runs

    with open_store(readonly=False) as session:
        stats = embed_runs(
            session, embedders.default_embedder(), commit_every=COMMIT_EVERY, commit=session.commit
        )
        session.commit()
    typer.echo(
        f"embedded {_plural(stats.runs, 'run')} ({_plural(stats.vectors, 'vector')}); "
        f"{stats.unchanged} unchanged, {stats.open_runs} still running"
        + (f", {_plural(stats.removed, 'old failure vector')} removed" if stats.removed else "")
    )


def search(
    query: Annotated[
        str, typer.Argument(help="What to look for, in your own words or exact terms.")
    ],
    source: Annotated[str | None, typer.Option(help="otel or claude_code.")] = None,
    agent: Annotated[str | None, typer.Option(help="The agent or service name, exactly.")] = None,
    status: Annotated[
        str | None,
        typer.Option(help="ok, error, unset or unfinished (running runs aren't embedded yet)."),
    ] = None,
    since: Annotated[
        str | None, typer.Option(help="Started at or after: 2h, 3d, 2026-11-10...")
    ] = None,
    until: Annotated[str | None, typer.Option(help="Started before: the same forms.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=100, help="How many runs.")] = 10,
) -> None:
    """Runs that match a description: by meaning (embeddings) and by exact words."""
    from flight_recorder.search import embedder as embedders
    from flight_recorder.search.index import has_vectors, pending_runs
    from flight_recorder.search.query import search as run_search
    from flight_recorder.store.read import RunFilter

    if not query.strip():
        raise typer.BadParameter("say what to look for", param_hint="QUERY")
    now = datetime.now(UTC)
    where = RunFilter(
        source=source,
        agent=agent,
        status=_choice(status, SEARCH_STATES, "--status"),  # type: ignore[arg-type]
        since=parse_when(since, now) if since else None,
        until=parse_when(until, now) if until else None,
        limit=limit,
    )
    model = embedders.default_embedder()
    with open_store() as session:
        hits = run_search(session, model, query.strip(), where, now)
        missing = pending_runs(session, model.model)
        out = render.console()
        if hits:
            out.print(_table(hits))
        elif has_vectors(session, model.model):
            out.print("no matching runs")
        else:
            out.print("nothing is embedded yet")
        if missing:
            verb = "isn't" if missing == 1 else "aren't"
            out.print(
                Text(
                    f"{_plural(missing, 'settled run')} {verb} embedded yet; run `afr embed`",
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


def search_check(
    queries: Annotated[
        Path, typer.Argument(help="YAML: a list of {query, runs: [trace id, ...]}.", dir_okay=False)
    ],
    compare: Annotated[
        str | None,
        typer.Option(help="Another fastembed model, ranked in memory (downloaded on first use)."),
    ] = None,
    k: Annotated[int, typer.Option("-k", min=1, max=50, help="Recall at this many runs.")] = 5,
    agent: Annotated[str | None, typer.Option(help="Search only this agent's runs.")] = None,
) -> None:
    """Recall@k of `afr search` on hand-written queries, and optionally of another model."""
    from flight_recorder.search import embedder as embedders
    from flight_recorder.search.retrieval import (
        MemoryIndex,
        load_queries,
        missing_runs,
        recall_at_k,
        stored_ranking,
    )
    from flight_recorder.store.read import RunFilter

    try:
        cases = load_queries(queries)
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="QUERIES") from error
    where = RunFilter(agent=agent)
    stored = embedders.default_embedder()
    with open_store() as session:
        if missing := missing_runs(session, cases):
            typer.echo(
                f"error: these right runs aren't in the store: {', '.join(missing)}", err=True
            )
            raise typer.Exit(code=1)
        other = (
            MemoryIndex(
                session,
                stored_model=stored.model,
                embedder=embedders.named_embedder(compare),
                where=where,
            )
            if compare
            else None
        )
        table = Table(box=None, pad_edge=False, header_style="bold", collapse_padding=True)
        table.add_column("query")
        table.add_column("right", justify="right")
        table.add_column(f"afr search ({stored.model})", justify="right")
        if other:
            table.add_column(other.embedder.model, justify="right")
        totals = [0.0, 0.0]
        for case in cases:
            mine = recall_at_k(stored_ranking(session, stored, case.text, k, where), case.runs, k)
            row = [render.text(render.one_line(case.text, 70)), str(len(case.runs)), f"{mine:.2f}"]
            totals[0] += mine
            if other:
                theirs = recall_at_k(other.ranking(case.text, k), case.runs, k)
                totals[1] += theirs
                row.append(f"{theirs:.2f}")
            table.add_row(*row)
        n = len(cases)
        table.add_row(
            Text(f"mean recall@{k} ({n} queries)", style="bold"),
            "",
            f"{totals[0] / n:.2f}",
            *([f"{totals[1] / n:.2f}"] if other else []),
        )
        render.console().print(table)
