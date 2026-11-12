"""`afr hook`: record Claude Code sessions."""

from pathlib import Path
from typing import Annotated

import typer

hook_app = typer.Typer(
    help="Record Claude Code sessions (docs/claude-code.md).", no_args_is_help=True
)


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
