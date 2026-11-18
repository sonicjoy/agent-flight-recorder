"""Engine, sessions and migrations for the run store (sync SQLAlchemy with psycopg 3)."""

from __future__ import annotations

import functools
from collections.abc import Generator
from contextlib import contextmanager
from importlib.resources import files

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from flight_recorder.settings import Settings


def database_url(settings: Settings | None = None) -> str:
    return (settings or Settings()).database_url.get_secret_value()


CONNECT_TIMEOUT = 3  # seconds: the CLI and the MCP server fail fast when Postgres is down
UNREACHABLE = "can't reach the run store; is Postgres up (`docker compose up -d db`)?"


class StoreUnreachable(Exception):
    """Postgres didn't answer (it's down, or the URL is wrong)."""


class StoreBehind(Exception):
    """The database isn't at this code's newest migration, in either direction."""


def make_engine(url: str | None = None, *, connect_timeout: int | None = None) -> Engine:
    """`connect_timeout` (seconds) makes a down Postgres fail fast instead of after the
    OS's connect retries (2 minutes)."""
    connect_args = {} if connect_timeout is None else {"connect_timeout": connect_timeout}
    return create_engine(url or database_url(), pool_pre_ping=True, connect_args=connect_args)


@contextmanager
def session_scope(engine: Engine) -> Generator[Session]:
    """A session that commits on success and rolls back on error."""
    with sessionmaker(engine)() as session, session.begin():
        yield session


def alembic_config(url: str | None = None) -> Config:
    """Alembic config built in code, so no alembic.ini is needed (also in a deployed container)."""
    config = Config()
    config.set_main_option("script_location", str(files("flight_recorder.store") / "migrations"))
    # `%` is Alembic's interpolation character; a URL-encoded password may contain it.
    config.set_main_option("sqlalchemy.url", (url or database_url()).replace("%", "%%"))
    return config


def upgrade(url: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str | None = None, revision: str = "base") -> None:
    command.downgrade(alembic_config(url), revision)


@functools.cache
def _script_heads() -> frozenset[str]:
    """The newest migrations this code knows: read from files once per process."""
    from alembic.script import ScriptDirectory

    # any URL: only the scripts are read, so no settings are needed
    return frozenset(ScriptDirectory.from_config(alembic_config("postgresql://")).get_heads())


def pending_migrations(engine: Engine) -> bool:
    """True if the database isn't at the newest migration (run `afr db upgrade`), or is at
    one this code doesn't know (a newer branch migrated it)."""
    from alembic.runtime.migration import MigrationContext

    with engine.connect() as connection:
        current = set(MigrationContext.configure(connection).get_current_heads())
    return frozenset(current) != _script_heads()


def check_ready(engine: Engine) -> None:
    """Raises StoreUnreachable or StoreBehind unless the store can be used."""
    try:
        behind = pending_migrations(engine)
    except OperationalError as error:
        raise StoreUnreachable from error
    if behind:
        raise StoreBehind


@contextmanager
def readonly_session(engine: Engine) -> Generator[Session]:
    """A session that can't write (Postgres refuses it), rolled back at the end."""
    with Session(engine) as session:
        session.connection(execution_options={"postgresql_readonly": True})
        try:
            yield session
        finally:
            session.rollback()
