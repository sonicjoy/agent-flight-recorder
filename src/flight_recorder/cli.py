"""The `afr` command-line interface."""

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
