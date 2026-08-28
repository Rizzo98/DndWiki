"""Async SQLAlchemy engine/session factory per service database."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import Settings, get_settings


class Base(DeclarativeBase):
    """Declarative base for all service models."""


_engines: dict[str, object] = {}


def get_engine(settings: Settings | None = None):
    settings = settings or get_settings()
    url = settings.database_url
    if url not in _engines:
        _engines[url] = create_async_engine(url, pool_pre_ping=True)
    return _engines[url]


def session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(settings), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a scoped async session."""
    async with session_factory()() as session:
        yield session
