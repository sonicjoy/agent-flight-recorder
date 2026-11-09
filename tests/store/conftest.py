"""Fixtures for tests that need a real Postgres.

Each test session creates throwaway databases on the server from AFR_TEST_DATABASE_URL
(default: compose.yaml's Postgres) and drops them at the end. Without a server the tests
skip, unless AFR_REQUIRE_DB is set (as in CI), where a missing server is a failure.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Generator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from flight_recorder.store.db import upgrade

SERVER_URL = os.environ.get(
    "AFR_TEST_DATABASE_URL", "postgresql+psycopg://afr:afr@127.0.0.1:5433/afr"
)

pytestmark = pytest.mark.db


def _admin_engine() -> Engine:
    url = make_url(SERVER_URL).set(database="postgres")
    return create_engine(url, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3})


@pytest.fixture(scope="session")
def admin() -> Generator[Engine]:
    engine = _admin_engine()
    try:
        with engine.connect():
            pass
    except OperationalError:
        where = make_url(SERVER_URL).render_as_string()
        message = f"No Postgres at {where}: start it with `docker compose up -d`"
        if os.environ.get("AFR_REQUIRE_DB"):
            pytest.fail(message)
        pytest.skip(message)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def make_database(admin: Engine) -> Generator[Callable[[], str]]:
    """Creates empty databases on demand; returns each one's URL. All are dropped at the end."""
    created: list[str] = []

    def make() -> str:
        name = f"afr_test_{uuid.uuid4().hex[:8]}"
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        created.append(name)
        return make_url(SERVER_URL).set(database=name).render_as_string(hide_password=False)

    yield make
    with admin.connect() as connection:
        for name in created:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture(scope="session")
def migrated_url(make_database: Callable[[], str]) -> str:
    """One database migrated to head, shared by the session."""
    url = make_database()
    upgrade(url)
    return url


@pytest.fixture(scope="session")
def engine(migrated_url: str) -> Generator[Engine]:
    engine = create_engine(migrated_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Generator[Session]:
    """A session inside a transaction that is rolled back after the test.

    Commits inside the test become savepoints, so tests never see each other's data.
    """
    with engine.connect() as connection:
        transaction = connection.begin()
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            yield session
        transaction.rollback()
