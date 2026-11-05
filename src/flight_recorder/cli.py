"""The `afr` command-line interface."""

import time
from typing import Annotated

import typer

from flight_recorder import __version__

app = typer.Typer(
    name="afr",
    help="Agent Flight Recorder: record, replay and analyse AI agent runs.",
    no_args_is_help=True,
)


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
