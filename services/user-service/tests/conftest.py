"""Shared fixtures: in-memory SQLite DB, mutable JWT claims, ASGI client.

External integrations (MinIO, Qdrant, SpeechBrain, campaign-service) are
replaced by in-memory fakes wired through dependency overrides, so the whole
suite runs without Docker or the heavy ML dependencies.
"""

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
from app.models import User

# ---------------------------------------------------------------- fakes


class FakeStorage:
    """Records uploads; presigned URLs are deterministic and never real."""

    def __init__(self) -> None:
        self.uploads: list[dict] = []

    async def put_object_stream(self, bucket: str, key: str, stream, content_type: str) -> None:
        data = stream.read() if hasattr(stream, "read") else stream
        self.uploads.append(
            {"bucket": bucket, "key": key, "content_type": content_type, "size": len(data)}
        )

    async def presigned_get(self, bucket: str, key: str, expires_sec: int | None = None) -> str:
        return f"http://presigned/{bucket}/{key}"


class FakeVoiceprints:
    """In-memory Qdrant: point_id -> (vector, payload)."""

    def __init__(self) -> None:
        self.points: dict[str, tuple[list[float], dict]] = {}

    async def ensure_collection(self) -> None:
        pass

    async def upsert(self, point_id: str, vector: list[float], payload: dict) -> None:
        self.points[point_id] = (vector, payload)

    async def delete(self, point_id: str) -> None:
        self.points.pop(point_id, None)


class FakeEmbedder:
    """Deterministic 192-d embedding; duration is configurable per test."""

    def __init__(self, duration: float = 15.0) -> None:
        self.duration = duration
        self.calls: list[tuple[bytes, str]] = []

    async def embed_bytes(self, data: bytes, suffix: str = ".wav") -> tuple[list[float], float]:
        self.calls.append((data, suffix))
        return [0.1] * 192, self.duration


class FakeCampaignClient:
    """Membership map {(campaign_id, user_id): role}; no network."""

    def __init__(self, membership: dict) -> None:
        self._membership = membership

    async def assert_member(self, campaign_id: UUID, user_id: UUID) -> str:
        role = self._membership.get((campaign_id, user_id))
        if role is None:
            raise ValueError("not a member of this campaign")
        return role


class Fakes:
    def __init__(self, membership: dict) -> None:
        self.membership = membership
        self.storage = FakeStorage()
        self.voiceprints = FakeVoiceprints()
        self.embedder = FakeEmbedder()
        self.campaign_client = FakeCampaignClient(membership)


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
def other_user_id() -> UUID:
    return UUID("99999999-9999-9999-9999-999999999999")


@pytest.fixture
def membership() -> dict:
    """Role map consumed by the FakeCampaignClient (see Fakes)."""
    return {}


@pytest.fixture
def fakes(membership) -> Fakes:
    return Fakes(membership)


@pytest.fixture
def claims(user_id: UUID) -> dict:
    """Mutable JWT claims; tests swap identity by mutating this dict."""
    return {
        "sub": str(user_id),
        "email": "player@test.local",
        "preferred_username": "player",
        "realm_access": {"roles": ["player", "dm"]},
    }


@pytest.fixture
def seed_user(session_factory):
    """Insert a user row directly (as Keycloak would have after provisioning).

    ``id`` defaults to a generated uuid; pass it explicitly to mirror
    get_or_create_user, which sets id == Keycloak subject.
    """

    async def _seed(
        *,
        id: UUID | None = None,
        keycloak_sub: str = "22222222-2222-2222-2222-222222222222",
        email: str | None = "seeded@test.local",
        display_name: str = "Seeded",
    ) -> User:
        kwargs: dict = {}
        if id is not None:
            kwargs["id"] = id
        async with session_factory() as db:
            user = User(
                keycloak_sub=keycloak_sub,
                email=email,
                display_name=display_name,
                **kwargs,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user

    return _seed


@pytest.fixture
async def client(
    session_factory,
    claims,
    fakes,
) -> AsyncIterator[httpx.AsyncClient]:
    """ASGI client with the DB session, JWT auth and all integrations faked."""
    from app.main import app

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[dnd_db.get_session] = _override_session
    app.dependency_overrides[dnd_auth.current_user] = lambda: claims
    app.dependency_overrides[deps.require_service] = lambda: None
    app.dependency_overrides[deps.get_storage] = lambda: fakes.storage
    app.dependency_overrides[deps.get_voiceprint_store] = lambda: fakes.voiceprints
    app.dependency_overrides[deps.get_embedder] = lambda: fakes.embedder
    app.dependency_overrides[deps.get_campaign_client] = lambda: fakes.campaign_client
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
