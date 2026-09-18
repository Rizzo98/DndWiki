"""Tests for the summary review API (rewrite with feedback + confirm)."""

from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest
from dnd_common import auth as dnd_auth
from dnd_common import db as dnd_db
from dnd_common.events import Event
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import summary_review as review
from app.clients.campaign_service import MembershipUnavailable
from app.main import app
from app.services import summaries

SESSION_ID = "11111111-1111-1111-1111-111111111111"
CAMPAIGN_ID = "22222222-2222-2222-2222-222222222222"
DM_ID = "829e495c-a552-4f95-81ce-7efa62d0ef53"
JOB_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

SUMMARY_BODY = {
    "edits": [
        {
            "targets": ["Character A was going to the city center."],
            "instruction": "It wasn't Character A, it was Character B",
        }
    ]
}


class FakeSessionClient:
    def __init__(self, status: str = "summary_ready"):
        self.status = status

    async def get_session(self, session_id: str) -> dict:
        return {"id": session_id, "campaign_id": CAMPAIGN_ID, "status": self.status}


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
async def client(session_factory) -> AsyncIterator[httpx.AsyncClient]:
    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    claims = {"sub": DM_ID}
    publisher = FakePublisher()
    app.dependency_overrides[dnd_db.get_session] = _override_session
    app.dependency_overrides[dnd_auth.current_user] = lambda: claims
    app.dependency_overrides[review.get_publisher] = lambda: publisher
    review._session_client = FakeSessionClient()
    review._campaign_client = FakeCampaignClient()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c._publisher = publisher  # type: ignore[attr-defined]
        c._claims = claims  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()


async def _seed_summary(session_factory, *, session_summary="Character A was going to the city center."):
    async with session_factory() as db:
        return await summaries.save_summary(
            db,
            UUID(SESSION_ID),
            generation_job_id=JOB_ID,
            merged={"session_summary": session_summary, "confidence": 0.5},
            llm_provider="deepseek",
            llm_model="deepseek/deepseek-chat",
            prompt_version="v11",
        )


async def test_regenerate_summary_queues_the_feedback(client, session_factory):
    await _seed_summary(session_factory)
    resp = await client.post(
        "/api/content/sessions/" + SESSION_ID + "/summary/regenerate", json=SUMMARY_BODY
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["queued"] is True
    assert body["revision"] == 1
    assert body["edits"] == 1

    events = client._publisher.events  # type: ignore[attr-defined]
    assert [ev.type for ev in events] == ["summary.regenerate"]
    payload = events[0].payload
    assert payload["session_id"] == SESSION_ID
    assert payload["campaign_id"] == CAMPAIGN_ID
    assert payload["requested_by"] == DM_ID
    assert payload["edits"][0]["instruction"] == "It wasn't Character A, it was Character B"
    assert payload["edits"][0]["targets"] == ["Character A was going to the city center."]


async def test_regenerate_summary_passes_the_displayed_lines(client, session_factory):
    await _seed_summary(session_factory)
    body = dict(SUMMARY_BODY, summary_text="hand edited narrative")
    resp = await client.post(
        "/api/content/sessions/" + SESSION_ID + "/summary/regenerate", json=body
    )
    assert resp.status_code == 200
    events = client._publisher.events  # type: ignore[attr-defined]
    assert events[0].payload["summary_text"] == "hand edited narrative"


async def test_regenerate_summary_requires_at_least_one_edit(client, session_factory):
    await _seed_summary(session_factory)
    resp = await client.post(
        "/api/content/sessions/" + SESSION_ID + "/summary/regenerate", json={"edits": []}
    )
    assert resp.status_code == 422


async def test_regenerate_summary_rejects_blank_instructions(client, session_factory):
    await _seed_summary(session_factory)
    resp = await client.post(
        "/api/content/sessions/" + SESSION_ID + "/summary/regenerate",
        json={"edits": [{"targets": [], "instruction": "   "}]},
    )
    assert resp.status_code == 422


async def test_regenerate_summary_409_without_a_summary(client):
    resp = await client.post(
        "/api/content/sessions/" + SESSION_ID + "/summary/regenerate", json=SUMMARY_BODY
    )
    assert resp.status_code == 409
    assert "no summary" in resp.json()["detail"]


async def test_regenerate_summary_409_when_not_reviewable(client, session_factory):
    await _seed_summary(session_factory)
    review._session_client = FakeSessionClient(status="content_ready")
    resp = await client.post(
        "/api/content/sessions/" + SESSION_ID + "/summary/regenerate", json=SUMMARY_BODY
    )
    assert resp.status_code == 409


async def test_regenerate_summary_requires_dm(client, session_factory):
    await _seed_summary(session_factory)
    review._campaign_client = NotDmCampaignClient()
    resp = await client.post(
        "/api/content/sessions/" + SESSION_ID + "/summary/regenerate", json=SUMMARY_BODY
    )
    assert resp.status_code == 403


async def test_regenerate_summary_campaign_service_down_is_503(client, session_factory):
    await _seed_summary(session_factory)
    review._campaign_client = DownCampaignClient()
    resp = await client.post(
        "/api/content/sessions/" + SESSION_ID + "/summary/regenerate", json=SUMMARY_BODY
    )
    assert resp.status_code == 503


async def test_regenerate_summary_dev_role_bypasses_campaign_check(client, session_factory):
    await _seed_summary(session_factory)
    client._claims["realm_access"] = {"roles": ["dev"]}  # type: ignore[attr-defined]
    review._campaign_client = DownCampaignClient()
    resp = await client.post(
        "/api/content/sessions/" + SESSION_ID + "/summary/regenerate", json=SUMMARY_BODY
    )
    assert resp.status_code == 200


async def test_confirm_summary_queues_the_wiki_phase(client, session_factory):
    await _seed_summary(session_factory)
    resp = await client.post("/api/content/sessions/" + SESSION_ID + "/summary/confirm")
    assert resp.status_code == 200
    assert resp.json()["queued"] is True
    events = client._publisher.events  # type: ignore[attr-defined]
    assert [ev.type for ev in events] == ["summary.confirmed"]
    assert events[0].payload["confirmed_by"] == DM_ID
    # the confirmation stamp is written by the endpoint: the summary is the
    # DM's, and generation refuses to start without that stamp
    async with session_factory() as db:
        row = await summaries.latest_summary_for_session(db, UUID(SESSION_ID))
    assert row.review_status == "confirmed"
    assert row.confirmed_by == UUID(DM_ID)
    assert row.confirmed_at is not None


async def test_confirm_summary_409_without_a_summary(client):
    resp = await client.post("/api/content/sessions/" + SESSION_ID + "/summary/confirm")
    assert resp.status_code == 409


async def test_confirm_summary_requires_dm(client, session_factory):
    await _seed_summary(session_factory)
    review._campaign_client = NotDmCampaignClient()
    resp = await client.post("/api/content/sessions/" + SESSION_ID + "/summary/confirm")
    assert resp.status_code == 403


async def test_confirm_summary_allowed_for_a_failed_session(client, session_factory):
    """A wiki-phase failure keeps the reviewed summary: the DM retries it."""
    await _seed_summary(session_factory)
    review._session_client = FakeSessionClient(status="failed")
    resp = await client.post("/api/content/sessions/" + SESSION_ID + "/summary/confirm")
    assert resp.status_code == 200


async def test_review_routes_expose_expected_openapi(client):
    resp = await client.get("/openapi.json")
    paths = resp.json()["paths"]
    regenerate = paths["/api/content/sessions/{session_id}/summary/regenerate"]["post"]
    confirm = paths["/api/content/sessions/{session_id}/summary/confirm"]["post"]
    param_names = [p["name"] for p in regenerate.get("parameters", [])]
    assert param_names == ["session_id"]
    assert "requestBody" in regenerate
    assert [p["name"] for p in confirm.get("parameters", [])] == ["session_id"]
    assert "requestBody" not in confirm
