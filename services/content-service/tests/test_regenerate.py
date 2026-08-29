"""Tests for the debug regenerate endpoint (auth, state gating, event)."""

from collections.abc import AsyncIterator

import httpx
import pytest
from dnd_common import auth as dnd_auth
from dnd_common import db as dnd_db
from dnd_common.events import Event
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import regenerate as regen
from app.clients.campaign_service import MembershipUnavailable
from app.main import app

SESSION_ID = "11111111-1111-1111-1111-111111111111"
CAMPAIGN_ID = "22222222-2222-2222-2222-222222222222"
DM_ID = "829e495c-a552-4f95-81ce-7efa62d0ef53"


class FakeSessionClient:
    def __init__(self, status: str = "content_ready", speakers: list[dict] | None = None):
        self.status = status
        self.speakers = speakers if speakers is not None else [
            {"label": "SPEAKER_00", "user_id": DM_ID, "display_name": None},
            {"label": "SPEAKER_01", "user_id": None, "display_name": "Bobby"},
        ]

    async def get_session(self, session_id: str) -> dict:
        return {"id": session_id, "campaign_id": CAMPAIGN_ID, "status": self.status}

    async def list_speakers(self, session_id: str) -> list[dict]:
        return self.speakers


class FakeCampaignClient:
    async def assert_dm(self, campaign_id, user_id) -> None:
        pass


class NotDmCampaignClient(FakeCampaignClient):
    async def assert_dm(self, campaign_id, user_id) -> None:
        raise PermissionError("dm role required")


class DownCampaignClient(FakeCampaignClient):
    async def assert_dm(self, campaign_id, user_id) -> None:
        raise MembershipUnavailable("campaign-service unreachable")


class FakePublisher:
    def __init__(self):
        self.events: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.events.append(event)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield object()  # the endpoint never touches the db

    claims = {"sub": DM_ID}
    publisher = FakePublisher()
    app.dependency_overrides[dnd_db.get_session] = _override_session
    app.dependency_overrides[dnd_auth.current_user] = lambda: claims
    app.dependency_overrides[regen.get_publisher] = lambda: publisher
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c._publisher = publisher  # type: ignore[attr-defined]
        c._claims = claims  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()


async def test_regenerate_happy_path(client):
    regen._session_client = FakeSessionClient(status="content_ready")
    regen._campaign_client = FakeCampaignClient()
    resp = await client.post(f"/api/content/sessions/{SESSION_ID}/regenerate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["queued"] is True
    assert body["campaign_id"] == CAMPAIGN_ID
    assert body["speakers"] == 2

    events = client._publisher.events  # type: ignore[attr-defined]
    assert len(events) == 1
    ev = events[0]
    # reuses the existing content.generate binding (speakers.identified only)
    assert ev.type == "speakers.identified"
    assert ev.payload["pending_assignment"] is False
    labels = {s["label"] for s in ev.payload["speakers"]}
    assert labels == {"SPEAKER_00", "SPEAKER_01"}
    bobby = next(s for s in ev.payload["speakers"] if s["label"] == "SPEAKER_01")
    assert bobby["display_name"] == "Bobby"


async def test_regenerate_allows_failed_status(client):
    """A failed session may retry wiki generation from the same transcript."""
    regen._session_client = FakeSessionClient(status="failed")
    regen._campaign_client = FakeCampaignClient()
    resp = await client.post(f"/api/content/sessions/{SESSION_ID}/regenerate")
    assert resp.status_code == 200
    assert resp.json()["queued"] is True
    events = client._publisher.events  # type: ignore[attr-defined]
    assert len(events) == 1
    assert events[0].type == "speakers.identified"


async def test_regenerate_rejects_non_ready_status(client):
    regen._session_client = FakeSessionClient(status="published")
    regen._campaign_client = FakeCampaignClient()
    resp = await client.post(f"/api/content/sessions/{SESSION_ID}/regenerate")
    assert resp.status_code == 409


async def test_regenerate_requires_dm(client):
    regen._session_client = FakeSessionClient()
    regen._campaign_client = NotDmCampaignClient()
    resp = await client.post(f"/api/content/sessions/{SESSION_ID}/regenerate")
    assert resp.status_code == 403


async def test_regenerate_dev_role_bypasses_campaign_check(client):
    """A 'dev' realm-role user may regenerate without campaign membership."""
    client._claims["realm_access"] = {"roles": ["dev"]}  # type: ignore[attr-defined]
    regen._session_client = FakeSessionClient(status="content_ready")
    regen._campaign_client = DownCampaignClient()  # would raise MembershipUnavailable
    resp = await client.post(f"/api/content/sessions/{SESSION_ID}/regenerate")
    assert resp.status_code == 200
    assert resp.json()["queued"] is True


async def test_regenerate_campaign_service_down_is_503(client):
    regen._session_client = FakeSessionClient()
    regen._campaign_client = DownCampaignClient()
    resp = await client.post(f"/api/content/sessions/{SESSION_ID}/regenerate")
    assert resp.status_code == 503


async def test_regenerate_requires_named_speakers(client):
    regen._session_client = FakeSessionClient(
        speakers=[{"label": "SPEAKER_00", "user_id": DM_ID, "display_name": None}]
    )
    regen._campaign_client = FakeCampaignClient()
    resp = await client.post(f"/api/content/sessions/{SESSION_ID}/regenerate")
    assert resp.status_code == 200


async def test_get_publisher_dependency_has_no_query_params():
    """Regression: 'request' typed as Any made FastAPI require a ?request=
    query parameter, so every production call failed with 422 (tests never
    caught it because they override this dependency)."""
    from fastapi.dependencies.utils import get_dependant

    dependant = get_dependant(path="/", call=regen.get_publisher)
    assert dependant.query_params == []
    assert dependant.body_params == []


async def test_regenerate_route_openapi_has_no_extra_required_params(client):
    """End-to-end guard: the route schema must not demand anything beyond the
    path parameter (the earlier bug surfaced exactly there)."""
    resp = await client.get("/openapi.json")
    operation = resp.json()["paths"]["/api/content/sessions/{session_id}/regenerate"]["post"]
    params = [p["name"] for p in operation.get("parameters", [])]
    assert params == ["session_id"]
    assert "requestBody" not in operation
