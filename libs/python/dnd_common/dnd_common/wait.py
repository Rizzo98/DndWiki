"""Wait for a dependency instead of treating its absence as fatal.

Why this exists
---------------
``depends_on: postgres: condition: service_healthy`` is not enough on its own.
A Postgres that has just started **answers on TCP while it is still replaying
WAL**, and refuses every query with SQLSTATE 57P03 -
``asyncpg.exceptions.CannotConnectNowError: the database system is starting up``.
Observed on 2026-09-16: attribution-service ran ``alembic upgrade head`` 1.1 s
after the postgres container started, postgres became ready 0.8 s later, and the
service had already exited. With no restart policy the container stayed down.

The dependency condition only orders the containers *Compose itself* starts.
Anything else that starts containers - Docker Desktop after a reboot, ``docker
start``, ``docker compose start`` - bypasses it, so a service must not die on a
dependency that is merely late. ``connect_rabbitmq`` already applies this to the
broker (``fail_fast=False``); this is the same treatment for the service database::

    python -m dnd_common.wait && alembic upgrade head && exec uvicorn ...

It retries ``SELECT 1`` with capped exponential backoff until the database answers
or DB_WAIT_TIMEOUT_SEC (default 120) expires, then exits non-zero with the last
error - so a genuine misconfiguration (wrong host, wrong password, missing
database) still fails loudly instead of looping forever.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from sqlalchemy import text

from .config import Settings, get_settings
from .db import get_engine

#: Fixed name so `python -m dnd_common.wait` logs as dnd_common.wait, not as
#: __main__ - the entrypoint's log line is read more often than this file.
LOGGER_NAME = "dnd_common.wait"

logger = logging.getLogger(LOGGER_NAME)

DEFAULT_TIMEOUT_SEC = 120.0
DEFAULT_INITIAL_DELAY_SEC = 0.5
DEFAULT_MAX_DELAY_SEC = 5.0


def _env_float(name: str, default: float) -> float:
    """Read a float from the environment, falling back to *default*."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def _describe(exc: BaseException) -> str:
    """One line naming the failure, without SQLAlchemy's multi-line wrapper."""
    message = str(exc).strip()
    first_line = message.splitlines()[0] if message else ""
    return f"{type(exc).__name__}: {first_line}" if first_line else type(exc).__name__


async def wait_for_database(
    settings: Settings | None = None,
    *,
    timeout_sec: float | None = None,
    initial_delay_sec: float = DEFAULT_INITIAL_DELAY_SEC,
    max_delay_sec: float = DEFAULT_MAX_DELAY_SEC,
) -> float:
    """Block until the service database answers ``SELECT 1``.

    Returns the seconds spent waiting. Re-raises the last connection error once
    the deadline passes, so the caller still fails loudly on a real
    misconfiguration. Every attempt is logged: a service that waited 6 s says so.
    """
    settings = settings or get_settings()
    if timeout_sec is None:
        timeout_sec = _env_float("DB_WAIT_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC)
    engine = get_engine(settings)
    loop = asyncio.get_running_loop()
    started = loop.time()
    delay = initial_delay_sec
    attempt = 0

    while True:
        attempt += 1
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception as exc:
            waited = loop.time() - started
            if waited >= timeout_sec:
                logger.error(
                    "database %s not ready after %.0fs: %s",
                    settings.service_db_name,
                    waited,
                    _describe(exc),
                )
                await engine.dispose()
                raise
            logger.info(
                "database %s not ready (attempt %d, %.1fs of %.0fs): %s - retrying in %.1fs",
                settings.service_db_name,
                attempt,
                waited,
                timeout_sec,
                _describe(exc),
                delay,
            )
            await asyncio.sleep(max(0.0, min(delay, timeout_sec - waited)))
            delay = min(delay * 2, max_delay_sec)
            continue

        waited = loop.time() - started
        if attempt > 1:
            logger.info(
                "database %s ready after %d attempts (%.1fs)",
                settings.service_db_name,
                attempt,
                waited,
            )
        await engine.dispose()
        return waited


def main() -> int:
    """``python -m dnd_common.wait`` - the container entrypoint guard."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    settings = get_settings()
    logger.info(
        "waiting for database %s at %s:%s",
        settings.service_db_name,
        settings.postgres_host,
        settings.postgres_port,
    )
    try:
        asyncio.run(wait_for_database(settings))
    except Exception as exc:  # noqa: BLE001 - reported as the process exit code
        logger.error(
            "giving up on database %s at %s:%s - %s",
            settings.service_db_name,
            settings.postgres_host,
            settings.postgres_port,
            _describe(exc),
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
