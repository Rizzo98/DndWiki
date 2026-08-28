"""Shared fixtures: in-memory SQLite DB, fake external clients, ASGI client."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import pytest
from dnd_common import auth as dnd_auth
from dnd_common import db as dnd_db
from dnd_common.events import Event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app import deps
from app.clients.campaigns import CampaignServiceClient, MembershipUnavailable
from app.core.config import get_settings

# ---------------------------------------------------------------- fakes


class FakePublisher:
    """Records published events instead of talking to RabbitMQ."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def publish(self, event: Event) -> None:
        self.events.append(event)


class FakeStorage:
    """Records uploads and returns canned presigned URLs."""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, bytes, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self.urls: dict[str, str] = {}

    async def put_object_stream(self, bucket: str, key: str, stream, content_type: str) -> None:
        data = stream.read()
        self.uploads.append((bucket, key, data, content_type))

    async def delete_object(self, bucket: str, key: str) -> None:
        self.deleted.append((bucket, key))

    async def presigned_get(self, bucket: str, key: str, expires_sec: int | None = None) -> str:
        return self.urls.get(f"get:{bucket}/{key}", f"https://presigned/{bucket}/{key}")

    async def presigned_put(self, bucket: str, key: str, expires_sec: int = 900) -> str:
        return f"https://presigned-put/{bucket}/{key}"


class FakeCampaignClient(CampaignServiceClient):
    """campaign-service client with in-memory roles + members (no HTTP)."""

    def __init__(
        self,
        roles: dict[tuple[UUID, UUID], str] | None = None,
        members: dict[UUID, dict] | None = None,
        unavailable: bool = False,
    ) -> None:
        super().__init__("http://campaign.test", get_settings())
        self.roles = roles or {}
        self.members = members or {}
        self.unavailable = unavailable

    async def check_membership(self, campaign_id: UUID, user_id: UUID) -> str | None:
        if self.unavailable:
            raise MembershipUnavailable("campaign-service down")
        return self.roles.get((campaign_id, user_id))

    async def get_member(self, campaign_id: UUID, member_id: UUID) -> dict[str, Any]:
        if self.unavailable:
            raise MembershipUnavailable("campaign-service down")
        member = self.members.get(member_id)
        if member is None or member.get("campaign_id") != str(campaign_id):
            raise ValueError("member not found in this campaign")
        return member


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
def fake_publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def fake_campaign() -> FakeCampaignClient:
    return FakeCampaignClient()


@pytest.fixture
def user_id() -> UUID:
    return UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def dm_id() -> UUID:
    return UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
async def client(
    session_factory,
    fake_publisher,
    fake_storage,
    fake_campaign,
    user_id,
) -> AsyncIterator[httpx.AsyncClient]:
    """ASGI client with all external dependencies faked."""
    from app.main import app

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[dnd_db.get_session] = _override_session
    app.dependency_overrides[dnd_auth.current_user] = lambda: {
        "sub": str(user_id),
        "realm_access": {"roles": ["player", "dm"]},
    }
    app.dependency_overrides[deps.get_publisher] = lambda: fake_publisher
    app.dependency_overrides[deps.get_storage] = lambda: fake_storage
    app.dependency_overrides[deps.get_campaign_client] = lambda: fake_campaign
    app.dependency_overrides[deps.require_service] = lambda: None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
