"""The `afr` command-line interface."""

import time
from typing import Annotated

import typer

from flight_recorder import __version__
from flight_recorder.cli.db import db_app
from flight_recorder.cli.hook import hook_app
from flight_recorder.cli.pricing import pricing_app
from flight_recorder.cli.runs import replay, runs_app
from flight_recorder.cli.search import embed, search

app = typer.Typer(
    name="afr",
    help="Agent Flight Recorder: record, replay and analyse AI agent runs.",
    no_args_is_help=True,
)
app.add_typer(db_app, name="db")
app.add_typer(hook_app, name="hook")
app.add_typer(pricing_app, name="pricing")
app.add_typer(runs_app, name="runs")
app.command()(replay)
app.command()(embed)
app.command()(search)


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
