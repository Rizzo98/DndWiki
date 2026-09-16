"""Regression tests for dnd_common.wait - the container entrypoint's DB guard.

The bug: every entrypoint that owns a schema ran ``alembic upgrade head &&
uvicorn ...``, so a Postgres that answered on TCP while it was still replaying
WAL (SQLSTATE 57P03, surfaced by asyncpg as CannotConnectNowError: "the database
system is starting up") killed the container on a transient condition. With no
restart policy the container stayed dead until someone noticed.

These tests pin the guarantee: a connection failure is retried until the
deadline, and only an expired deadline is fatal.
"""

from __future__ import annotations

import asyncio
from typing import Self

import pytest
from sqlalchemy.exc import OperationalError

from dnd_common import wait as wait_module


def _cannot_connect() -> OperationalError:
    """What asyncpg raises while postgres is still starting up."""
    return OperationalError("SELECT 1", {}, Exception("the database system is starting up"))


class FakeConnection:
    def __init__(self, engine: FakeEngine) -> None:
        self._engine = engine

    async def __aenter__(self) -> Self:
        self._engine.attempts += 1
        if self._engine.failures:
            self._engine.failures -= 1
            raise _cannot_connect()
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def execute(self, statement: object) -> None:
        self._engine.executed.append(str(statement))


class FakeEngine:
    """Refuses the first *failures* connection attempts, then accepts SELECT 1."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.attempts = 0
        self.executed: list[str] = []
        self.disposed = 0

    def connect(self) -> FakeConnection:
        return FakeConnection(self)

    async def dispose(self) -> None:
        self.disposed += 1


class FakeSettings:
    """Just the fields wait_for_database reads - no pydantic, no .env."""

    service_db_name = "dnd_attribution"
    postgres_host = "postgres"
    postgres_port = 5432


def _engine(monkeypatch: pytest.MonkeyPatch, engine: FakeEngine) -> FakeEngine:
    monkeypatch.setattr(wait_module, "get_engine", lambda settings=None: engine)
    return engine


def test_waits_out_a_database_that_is_still_starting_up(monkeypatch):
    engine = _engine(monkeypatch, FakeEngine(failures=3))

    waited = asyncio.run(
        wait_module.wait_for_database(
            FakeSettings(),
            timeout_sec=5.0,
            initial_delay_sec=0.001,
            max_delay_sec=0.002,
        )
    )

    assert engine.attempts == 4  # three refusals, then the attempt that answered
    assert engine.executed == ["SELECT 1"]
    assert engine.disposed == 1
    assert waited >= 0.0


def test_a_ready_database_is_not_polled_twice(monkeypatch):
    engine = _engine(monkeypatch, FakeEngine(failures=0))

    asyncio.run(wait_module.wait_for_database(FakeSettings(), timeout_sec=5.0))

    assert engine.attempts == 1


def test_gives_up_loudly_when_the_database_never_arrives(monkeypatch):
    engine = _engine(monkeypatch, FakeEngine(failures=10_000))

    with pytest.raises(OperationalError):
        asyncio.run(
            wait_module.wait_for_database(
                FakeSettings(),
                timeout_sec=0.05,
                initial_delay_sec=0.005,
                max_delay_sec=0.01,
            )
        )

    assert engine.attempts > 1  # it retried, then refused to pretend it worked
    assert engine.disposed == 1


async def _ready(settings=None, **kwargs: object) -> float:
    return 0.0


async def _never(settings=None, **kwargs: object) -> float:
    raise _cannot_connect()


def test_main_reports_the_outcome_as_an_exit_code(monkeypatch):
    monkeypatch.setattr(wait_module, "wait_for_database", _ready)
    assert wait_module.main() == 0

    monkeypatch.setattr(wait_module, "wait_for_database", _never)
    assert wait_module.main() == 1


def test_the_deadline_is_configurable_from_the_environment(monkeypatch):
    monkeypatch.setenv("DB_WAIT_TIMEOUT_SEC", "12.5")
    assert wait_module._env_float("DB_WAIT_TIMEOUT_SEC", 1.0) == 12.5

    monkeypatch.setenv("DB_WAIT_TIMEOUT_SEC", "soon")
    assert wait_module._env_float("DB_WAIT_TIMEOUT_SEC", 1.0) == 1.0

    monkeypatch.delenv("DB_WAIT_TIMEOUT_SEC")
    assert wait_module._env_float("DB_WAIT_TIMEOUT_SEC", 3.0) == 3.0

