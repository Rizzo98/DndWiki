"""Shared fixtures: in-memory SQLite DB, mutable JWT claims, ASGI client."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest
from dnd_common import auth as dnd_auth
from dnd_common import db as dnd_db
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app import deps
from app.models import Campaign, CampaignMember

# ---------------------------------------------------------------- fixtures


@pytest.fixture
async def engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(dnd_db.Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def user_id() -> UUID:
    return UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def dm_id() -> UUID:
    return UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
def claims(user_id: UUID) -> dict:
    """Mutable JWT claims; tests swap identity by mutating this dict."""
    return {
        "sub": str(user_id),
        "email": "player@test.local",
        "realm_access": {"roles": ["player", "dm"]},
    }


@pytest.fixture
def seed_campaign(session_factory):
    """Insert a campaign plus its DM member row (creator invariant)."""

    async def _seed(
        *,
        dm_id: UUID,
        name: str = "The Fellowship",
        slug: str = "the-fellowship",
        status: str = "active",
        settings: dict | None = None,
    ) -> Campaign:
        async with session_factory() as db:
            campaign = Campaign(
                name=name,
                slug=slug,
                dm_user_id=dm_id,
                status=status,
                settings=settings or {},
            )
            db.add(campaign)
            await db.flush()
            db.add(CampaignMember(campaign_id=campaign.id, user_id=dm_id, role="dm"))
            await db.commit()
            await db.refresh(campaign)
            return campaign

    return _seed


@pytest.fixture
async def client(
    session_factory,
    claims,
) -> AsyncIterator[httpx.AsyncClient]:
    """ASGI client with the DB session and JWT auth faked."""
    from app.main import app

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[dnd_db.get_session] = _override_session
    app.dependency_overrides[dnd_auth.current_user] = lambda: claims
    app.dependency_overrides[deps.require_service] = lambda: None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
