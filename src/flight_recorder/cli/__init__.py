"""The `afr` command-line interface."""

import time
from typing import Annotated

import typer

from flight_recorder import __version__
from flight_recorder.cli.case import case_app
from flight_recorder.cli.db import db_app
from flight_recorder.cli.eval import eval_app
from flight_recorder.cli.hook import hook_app
from flight_recorder.cli.label import label, labels_app
from flight_recorder.cli.pricing import pricing_app
from flight_recorder.cli.runs import replay, runs_app
from flight_recorder.cli.search import embed, search, search_check

app = typer.Typer(
    name="afr",
    help="Agent Flight Recorder: record, replay and analyse AI agent runs.",
    no_args_is_help=True,
)
app.add_typer(case_app, name="case")
app.add_typer(db_app, name="db")
app.add_typer(eval_app, name="eval")
app.add_typer(hook_app, name="hook")
app.add_typer(pricing_app, name="pricing")
app.add_typer(runs_app, name="runs")
app.add_typer(labels_app, name="labels")
app.command()(label)
app.command()(replay)
app.command()(embed)
app.command()(search)
app.command("search-check")(search_check)


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


@app.command()
def serve(
    host: Annotated[
        str,
        typer.Option(
            help="Interface to listen on; beyond loopback needs API keys (docs/ingest.md)."
        ),
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="4318 is the standard OTLP/HTTP port.")] = 4318,
) -> None:
    """Run the ingest API: OTLP/HTTP traces on POST /v1/traces."""
    import os

    import uvicorn
    from pydantic import ValidationError

    from flight_recorder.ingest.app import create_app
    from flight_recorder.ingest.auth import AuthConfigError, auth_mode
    from flight_recorder.redact import default_redactor
    from flight_recorder.settings import Settings
    from flight_recorder.store.db import database_url, make_engine, pending_migrations

    # Decide on auth before touching the database: a missing secret is reported first.
    try:
        settings = Settings()
    except ValidationError as error:
        fields = sorted({str(e["loc"][0]) for e in error.errors(include_input=False) if e["loc"]})
        typer.echo(f"error: invalid setting: {', '.join(fields)}", err=True)
        raise typer.Exit(code=2) from None
    secret = settings.api_key_hashes
    hashes = secret.get_secret_value() if secret is not None else None  # "" stays "": refused
    try:
        mode = auth_mode(
            hashes, host, insecure=settings.insecure_no_auth, on_cloud_run="K_SERVICE" in os.environ
        )
    except AuthConfigError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(f"auth: {'on' if mode.auth is not None else 'off'} ({mode.reason})")

    engine = make_engine(database_url(settings))
    try:
        if pending_migrations(engine):
            typer.echo("error: the database isn't up to date; run `afr db upgrade` first", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"ingest: POST http://{host}:{port}/v1/traces (OTLP protobuf or JSON)")
        app_ = create_app(engine, default_redactor(settings), auth=mode.auth)
        uvicorn.run(app_, host=host, port=port, log_level="info")
    finally:
        engine.dispose()  # close pooled connections on every way out
