"""The `afr` command-line interface."""

import time
from pathlib import Path
from typing import Annotated

import typer

from flight_recorder import __version__

app = typer.Typer(
    name="afr",
    help="Agent Flight Recorder: record, replay and analyse AI agent runs.",
    no_args_is_help=True,
)


db_app = typer.Typer(help="The run store: Postgres migrations.", no_args_is_help=True)
app.add_typer(db_app, name="db")
hook_app = typer.Typer(
    help="Record Claude Code sessions (docs/claude-code.md).", no_args_is_help=True
)
app.add_typer(hook_app, name="hook")
pricing_app = typer.Typer(help="Model prices in pricing.yaml.", no_args_is_help=True)
app.add_typer(pricing_app, name="pricing")


@app.callback()
def main() -> None:
    """Agent Flight Recorder: record, replay and analyse AI agent runs."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command("llm-ping")
def llm_ping(
    model: Annotated[
        str | None, typer.Option(help="Model spec <provider>:<model>; default AFR_LLM_MODEL.")
    ] = None,
    cheap: Annotated[bool, typer.Option(help="Use AFR_LLM_CHEAP_MODEL.")] = False,
    prompt: Annotated[str, typer.Option(help="Prompt to send.")] = "Reply with exactly: pong",
) -> None:
    """Send one prompt to a model and print the reply, tokens and cost."""
    from flight_recorder.llm import MissingCredentialsError, ModelSpecError, chat_model, parse_spec
    from flight_recorder.pricing import cost_usd
    from flight_recorder.settings import Settings

    settings = Settings()
    spec = model or (settings.llm_cheap_model if cheap else settings.llm_model)
    try:
        llm = chat_model(spec, settings=settings)
    except (ModelSpecError, MissingCredentialsError) as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=2) from error

    started = time.perf_counter()
    try:
        reply = llm.invoke(prompt)
    except Exception as error:  # provider SDKs raise their own error types
        typer.echo(f"error: {spec} call failed: {type(error).__name__}: {error}", err=True)
        raise typer.Exit(code=1) from error
    elapsed = time.perf_counter() - started

    usage = reply.usage_metadata
    tokens_in = usage["input_tokens"] if usage else 0
    tokens_out = usage["output_tokens"] if usage else 0
    cost = cost_usd(str(parse_spec(spec)), tokens_in, tokens_out)
    cost_text = "unknown (not in pricing.yaml)" if cost is None else f"${cost:.6f}"

    typer.echo(f"model:   {spec}")
    typer.echo(f"reply:   {reply.text.strip()}")
    typer.echo(f"tokens:  {tokens_in} in / {tokens_out} out")
    typer.echo(f"cost:    {cost_text}")
    typer.echo(f"latency: {elapsed:.2f}s")


@db_app.command("upgrade")
def db_upgrade(revision: Annotated[str, typer.Argument(help="Target revision.")] = "head") -> None:
    """Apply migrations to AFR_DATABASE_URL (default: compose.yaml's local Postgres)."""
    from flight_recorder.store.db import upgrade

    upgrade(revision=revision)
    typer.echo(f"database at {revision}")


@db_app.command("downgrade")
def db_downgrade(
    revision: Annotated[str, typer.Argument(help="Target revision.")],
    yes: Annotated[bool, typer.Option("--yes", help="Confirm that data may be lost.")] = False,
) -> None:
    """Undo migrations down to a revision ("base" removes every table). Can lose data."""
    from flight_recorder.store.db import downgrade

    if not yes:
        typer.echo(
            f"Downgrading to {revision} drops every table and column added after it, with "
            "their data. Back up first (docs/schema.md), then re-run with --yes.",
            err=True,
        )
        raise typer.Exit(code=1)
    downgrade(revision=revision)
    typer.echo(f"database at {revision}")


@db_app.command("current")
def db_current() -> None:
    """Print the database's current revision."""
    from alembic import command

    from flight_recorder.store.db import alembic_config

    command.current(alembic_config())


@db_app.command("revision")
def db_revision(
    message: Annotated[str, typer.Option("-m", help="What the migration does.")],
    rev_id: Annotated[str | None, typer.Option(help="Revision id, e.g. 0002.")] = None,
) -> None:
    """Generate a migration from the difference between the models and the database."""
    from alembic import command

    from flight_recorder.store.db import alembic_config

    command.revision(alembic_config(), message=message, autogenerate=True, rev_id=rev_id)


LOOPBACK = ("127.0.0.1", "localhost", "::1")


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Interface to listen on; loopback only for now.")] = (
        "127.0.0.1"
    ),
    port: Annotated[int, typer.Option(help="4318 is the standard OTLP/HTTP port.")] = 4318,
) -> None:
    """Run the ingest API: OTLP/HTTP traces on POST /v1/traces."""
    import uvicorn

    from flight_recorder.ingest.app import create_app
    from flight_recorder.redact import default_redactor
    from flight_recorder.store.db import make_engine, pending_migrations

    if host not in LOOPBACK:
        typer.echo(
            f"error: refusing to listen on {host}: the ingest API has no authentication "
            "yet (#25). Use 127.0.0.1.",
            err=True,
        )
        raise typer.Exit(code=2)
    engine = make_engine()
    try:
        if pending_migrations(engine):
            typer.echo("error: the database isn't up to date; run `afr db upgrade` first", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"ingest: POST http://{host}:{port}/v1/traces (OTLP protobuf or JSON)")
        uvicorn.run(create_app(engine, default_redactor()), host=host, port=port, log_level="info")
    finally:
        engine.dispose()  # close pooled connections on every way out


@hook_app.command("run")
def hook_run() -> None:
    """Called by Claude Code with one hook event on stdin. Always exits 0, prints nothing."""
    import sys

    from flight_recorder.hook.client import run

    run(sys.stdin.buffer.read().decode("utf-8", errors="replace"))


@hook_app.command("install")
def hook_install(
    project: Annotated[
        Path, typer.Option(help="Project folder to record [default: the current folder]")
    ] = Path(),
) -> None:
    """Add the recorder's hooks to a project's .claude/settings.local.json (never user-level)."""
    from flight_recorder.hook.install import InstallError, install

    try:
        path = install(project)
    except InstallError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=2) from error
    typer.echo(f"hooks installed in {path}")
    typer.echo("Start `afr serve` before Claude Code; events are spooled while it's down.")


@hook_app.command("uninstall")
def hook_uninstall(
    project: Annotated[
        Path, typer.Option(help="Project folder [default: current folder]")
    ] = Path(),
) -> None:
    """Remove the recorder's hooks from a project; other hooks are kept."""
    from flight_recorder.hook.install import uninstall

    typer.echo("hooks removed" if uninstall(project) else "no recorder hooks found")


@pricing_app.command("check")
def pricing_check(
    write: Annotated[
        bool, typer.Option("--write", help="Update pricing.yaml and its version to match.")
    ] = False,
    file: Annotated[
        Path | None, typer.Option(help="pricing.yaml to check [default: the package's own]")
    ] = None,
    page: Annotated[
        Path | None, typer.Option(help="A saved copy of the page's Markdown, instead of fetching")
    ] = None,
) -> None:
    """Compare Claude rates with Anthropic's pricing page. Exits 1 if they differ (unless
    --write), 2 if the page can't be read or no longer parses."""
    from flight_recorder import pricing_check as pc

    path = file or Path(pc.__file__).with_name("pricing.yaml")
    text = path.read_bytes().decode("utf-8")  # bytes: keep the file's line endings as they are
    try:
        markdown = page.read_text(encoding="utf-8") if page else pc.fetch_page()
        report = pc.check(text, markdown)
    except (OSError, pc.PageFormatError) as error:
        typer.echo(f"error: {pc.MARKDOWN_URL if page is None else page}: {error}", err=True)
        raise typer.Exit(code=2) from error

    typer.echo(f"source: {pc.PRICING_URL if page is None else page}")
    for note in report.notes:
        typer.echo(f"note: {note}")
    if not report.changes:
        typer.echo(f"{path.name}: Claude rates match the page")
        return
    typer.echo(f"{len(report.changes)} rate(s) differ from the page:")
    for change in report.changes:
        typer.echo(f"  {change}")
    if not write:
        raise typer.Exit(code=1)
    version = pc.today()
    path.write_bytes(pc.apply(text, report.changes, version).encode("utf-8"))
    typer.echo(f"updated {path}, version {version}")


if __name__ == "__main__":  # python -m flight_recorder.cli, as the installed hook runs it
    app()
