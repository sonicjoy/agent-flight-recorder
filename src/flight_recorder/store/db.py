"""Engine, sessions and migrations for the run store (sync SQLAlchemy with psycopg 3)."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from importlib.resources import files

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from flight_recorder.settings import Settings


def database_url(settings: Settings | None = None) -> str:
    return (settings or Settings()).database_url.get_secret_value()


def make_engine(url: str | None = None) -> Engine:
    return create_engine(url or database_url(), pool_pre_ping=True)


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


def pending_migrations(engine: Engine) -> bool:
    """True if the database isn't at the newest migration (run `afr db upgrade`)."""
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    heads = set(ScriptDirectory.from_config(alembic_config()).get_heads())
    with engine.connect() as connection:
        current = set(MigrationContext.configure(connection).get_current_heads())
    return current != heads
