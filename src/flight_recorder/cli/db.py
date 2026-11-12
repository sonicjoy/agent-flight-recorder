"""`afr db`: the run store's migrations."""

from typing import Annotated

import typer

db_app = typer.Typer(help="The run store: Postgres migrations.", no_args_is_help=True)


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
