"""Internal (dnd-services token) read endpoints: session + speaker map."""

import uuid
from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest
from dnd_common import auth as dnd_auth
from dnd_common import db as dnd_db
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models
from app import deps
from app.clients.campaigns import CampaignServiceClient
from app.core.config import get_settings
from app.main import app
from app.models import Session, SpeakerAssignment


class FakeCampaignClient(CampaignServiceClient):
    """Only get_member is used by the internal endpoints under test."""

    def __init__(self):
        super().__init__("http://campaign.test", get_settings())

    async def get_member(self, campaign_id: UUID, member_id: UUID) -> dict:
        return {
            "id": str(member_id),
            "player_name": "Bobby",
            "character_name": "Bobblin",
            "role": "player",
            "user_id": None,
        }


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
async def client(session_factory) -> AsyncIterator[httpx.AsyncClient]:
    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[dnd_db.get_session] = _override_session
    app.dependency_overrides[dnd_auth.current_user] = lambda: {"sub": "someone"}
    # require_service validates a real JWKS-signed token; stub it like the UI tests do.
    app.dependency_overrides[deps.require_service] = lambda: None
    app.dependency_overrides[deps.get_campaign_client] = lambda: FakeCampaignClient()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _seed_session(session_factory, *, status: str = "content_ready") -> Session:
    async with session_factory() as db:
        session = Session(campaign_id=uuid.uuid4(), status=status, title="The lost mine")
        db.add(session)
        await db.flush()
        db.add(
            SpeakerAssignment(
                session_id=session.id,
                speaker_label="SPEAKER_00",
                user_id=uuid.uuid4(),
                status="auto",
            )
        )
        db.add(
            SpeakerAssignment(
                session_id=session.id,
                speaker_label="SPEAKER_01",
                member_id=uuid.uuid4(),
                status="confirmed",
            )
        )
        await db.commit()
        await db.refresh(session)
        return session


async def test_internal_get_session(client, session_factory):
    seeded = await _seed_session(session_factory)
    resp = await client.get(f"/internal/sessions/{seeded.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "content_ready"
    assert body["title"] == "The lost mine"


async def test_internal_get_session_unknown_404(client):
    resp = await client.get(f"/internal/sessions/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_internal_speakers_resolves_display_names(client, session_factory):
    seeded = await _seed_session(session_factory)
    resp = await client.get(f"/internal/sessions/{seeded.id}/speakers")
    assert resp.status_code == 200
    rows = {r["label"]: r for r in resp.json()}
    assert set(rows) == {"SPEAKER_00", "SPEAKER_01"}
    assert rows["SPEAKER_00"]["user_id"] is not None
    assert rows["SPEAKER_01"]["user_id"] is None
    assert rows["SPEAKER_01"]["display_name"] == "Bobby"  # from the member player_name
    assert rows["SPEAKER_01"]["character_name"] == "Bobblin"  # CHARACTER label for the wiki
