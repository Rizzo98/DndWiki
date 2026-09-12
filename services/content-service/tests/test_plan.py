"""Tests for the proposed-changes API (read, edit, confirm)."""

from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest
from dnd_common import auth as dnd_auth
from dnd_common import db as dnd_db
from dnd_common.events import Event
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import plan as plan_api
from app.clients.campaign_service import MembershipUnavailable
from app.main import app
from app.planner import build_change_set
from app.services import plans

SESSION_ID = "11111111-1111-1111-1111-111111111111"
CAMPAIGN_ID = "22222222-2222-2222-2222-222222222222"
DM_ID = "829e495c-a552-4f95-81ce-7efa62d0ef53"
SUMMARY_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")

CHANGE_SET = {
    "changes": [
        {
            "id": "c1",
            "action": "create",
            "kind": "character",
            "title": "Aragorn",
            "page_id": None,
            "before": None,
            "after": {
                "title": "Aragorn",
                "content_json": {"physical_look": "Lean.", "facts": ["Speaks the password."]},
                "visibility": "public",
                "confidence": 0.9,
            },
            "timeline": None,
            "dropped": False,
        },
        {
            "id": "c2",
            "action": "update",
            "kind": "event",
            "title": "Entering Moria",
            "page_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "before": {"title": "Entering Moria", "content_json": {"summary": "Old."}},
            "after": {
                "title": "Entering Moria",
                "content_json": {"summary": "The party passes the gate."},
                "visibility": "public",
                "confidence": None,
            },
            "timeline": {"summary": "The party passes the gate.", "in_world_date": None},
            "dropped": False,
        },
    ],
    "relations": [
        {
            "id": "r1",
            "from_title": "Aragorn",
            "to_title": "Fellowship",
            "to_page_id": None,
            "relation_type": "member_of",
            "dropped": False,
        }
    ],
    "skipped": [{"title": "Moria", "kind": "location", "matched_title": "Moria",
                 "reason": "already documented by this campaign"}],
}


class FakeSessionClient:
    def __init__(self, status: str = "wiki_plan_ready"):
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
    app.dependency_overrides[plan_api.get_publisher] = lambda: publisher
    plan_api._session_client = FakeSessionClient()
    plan_api._campaign_client = FakeCampaignClient()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c._publisher = publisher  # type: ignore[attr-defined]
        c._claims = claims  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()


async def _seed_plan(session_factory, *, change_set=None):
    async with session_factory() as db:
        return await plans.save_plan(
            db,
            UUID(SESSION_ID),
            summary_id=SUMMARY_ID,
            generation_job_id=None,
            change_set=change_set or build_change_set(
                {"session_summary": "x", "characters": [], "locations": [],
                 "events": [], "timeline_entries": []},
                CAMPAIGN_ID,
                SESSION_ID,
            ),
            language="en",
        )


async def _seed_custom_plan(session_factory):
    return await _seed_plan(session_factory, change_set=CHANGE_SET)


async def test_get_plan_is_empty_before_generation(client):
    resp = await client.get("/api/content/sessions/" + SESSION_ID + "/plan")
    assert resp.status_code == 200
    assert resp.json()["plan"] is None


async def test_get_plan_returns_the_proposal(client, session_factory):
    await _seed_custom_plan(session_factory)
    resp = await client.get("/api/content/sessions/" + SESSION_ID + "/plan")
    assert resp.status_code == 200
    plan = resp.json()["plan"]
    assert plan["status"] == "draft"
    assert plan["language"] == "en"
    assert [c["title"] for c in plan["changes"]] == ["Aragorn", "Entering Moria"]
    assert plan["changes"][1]["before"]["content_json"] == {"summary": "Old."}
    assert plan["skipped"][0]["title"] == "Moria"
    assert plan["counts"] == {
        "create": 1, "update": 1, "pages": 2, "events": 1, "relations": 1, "dropped": 0,
    }


async def test_get_plan_requires_dm(client, session_factory):
    await _seed_custom_plan(session_factory)
    plan_api._campaign_client = NotDmCampaignClient()
    resp = await client.get("/api/content/sessions/" + SESSION_ID + "/plan")
    assert resp.status_code == 403


async def test_get_plan_campaign_service_down_is_503(client, session_factory):
    await _seed_custom_plan(session_factory)
    plan_api._campaign_client = DownCampaignClient()
    resp = await client.get("/api/content/sessions/" + SESSION_ID + "/plan")
    assert resp.status_code == 503


async def test_put_plan_applies_the_dm_edits(client, session_factory):
    """The DM rewrites an entry and drops another: the set keeps its identity."""
    await _seed_custom_plan(session_factory)
    resp = await client.put(
        "/api/content/sessions/" + SESSION_ID + "/plan",
        json={
            "changes": [
                {
                    "id": "c1",
                    "title": "Aragorn son of Arathorn",
                    "after": {
                        "content_json": {"physical_look": "Lean and weathered.",
                                         "facts": ["Speaks the password.", "Heir of Isildur"]},
                    },
                },
                {"id": "c2", "dropped": True},
            ],
            "relations": [{"id": "r1", "dropped": True}],
        },
    )
    assert resp.status_code == 200
    plan = resp.json()["plan"]
    assert plan["changes"][0]["title"] == "Aragorn son of Arathorn"
    assert plan["changes"][0]["after"]["title"] == "Aragorn son of Arathorn"
    assert plan["changes"][0]["after"]["content_json"]["facts"] == [
        "Speaks the password.", "Heir of Isildur",
    ]
    assert plan["changes"][1]["dropped"] is True
    assert plan["relations"][0]["dropped"] is True
    assert plan["counts"]["pages"] == 1
    assert plan["counts"]["dropped"] == 2
    # identity/provenance stay pipeline-owned
    assert plan["changes"][1]["page_id"] == "cccccccc-cccc-cccc-cccc-cccccccccccc"
    assert plan["changes"][1]["before"] == {"title": "Entering Moria",
                                            "content_json": {"summary": "Old."}}


async def test_put_plan_edits_the_timeline_entry(client, session_factory):
    await _seed_custom_plan(session_factory)
    resp = await client.put(
        "/api/content/sessions/" + SESSION_ID + "/plan",
        json={
            "changes": [
                {"id": "c2", "timeline": {"summary": "The gate opens at last.",
                                          "in_world_date": "17 Ches 1492 DR"}}
            ]
        },
    )
    assert resp.status_code == 200
    timeline = resp.json()["plan"]["changes"][1]["timeline"]
    assert timeline == {"summary": "The gate opens at last.", "in_world_date": "17 Ches 1492 DR"}


async def test_put_plan_rejects_unknown_change_ids(client, session_factory):
    await _seed_custom_plan(session_factory)
    resp = await client.put(
        "/api/content/sessions/" + SESSION_ID + "/plan",
        json={"changes": [{"id": "nope", "dropped": True}]},
    )
    assert resp.status_code == 422
    assert "unknown change id" in resp.json()["detail"]


async def test_put_plan_rejects_an_empty_title(client, session_factory):
    await _seed_custom_plan(session_factory)
    resp = await client.put(
        "/api/content/sessions/" + SESSION_ID + "/plan",
        json={"changes": [{"id": "c1", "title": "   "}]},
    )
    assert resp.status_code == 422


async def test_put_plan_rejects_a_bad_visibility(client, session_factory):
    await _seed_custom_plan(session_factory)
    resp = await client.put(
        "/api/content/sessions/" + SESSION_ID + "/plan",
        json={"changes": [{"id": "c1", "after": {"visibility": "secret"}}]},
    )
    assert resp.status_code == 422


async def test_put_plan_is_locked_while_applying(client, session_factory):
    await _seed_custom_plan(session_factory)
    async with session_factory() as db:
        await plans.mark_applying(db, UUID(SESSION_ID))
    resp = await client.put(
        "/api/content/sessions/" + SESSION_ID + "/plan",
        json={"changes": [{"id": "c1", "dropped": True}]},
    )
    assert resp.status_code == 422
    assert "only be edited while" in resp.json()["detail"]


async def test_put_plan_409_when_not_under_review(client, session_factory):
    await _seed_custom_plan(session_factory)
    plan_api._session_client = FakeSessionClient(status="content_ready")
    resp = await client.put(
        "/api/content/sessions/" + SESSION_ID + "/plan", json={"changes": []}
    )
    assert resp.status_code == 409


async def test_confirm_plan_queues_the_apply(client, session_factory):
    await _seed_custom_plan(session_factory)
    resp = await client.post("/api/content/sessions/" + SESSION_ID + "/plan/confirm")
    assert resp.status_code == 200
    body = resp.json()
    assert body["queued"] is True
    assert body["pages"] == 2
    events = client._publisher.events  # type: ignore[attr-defined]
    assert [ev.type for ev in events] == ["plan.confirmed"]
    assert events[0].payload["confirmed_by"] == DM_ID
    assert events[0].payload["campaign_id"] == CAMPAIGN_ID
    # the confirmation is stamped right away so the UI shows it
    async with session_factory() as db:
        row = await plans.get_plan(db, UUID(SESSION_ID))
    assert row.confirmed_by == UUID(DM_ID)
    assert row.confirmed_at is not None


async def test_confirm_plan_409_when_not_under_review(client, session_factory):
    await _seed_custom_plan(session_factory)
    plan_api._session_client = FakeSessionClient(status="applying_wiki")
    resp = await client.post("/api/content/sessions/" + SESSION_ID + "/plan/confirm")
    assert resp.status_code == 409


async def test_confirm_plan_409_without_a_plan(client):
    resp = await client.post("/api/content/sessions/" + SESSION_ID + "/plan/confirm")
    assert resp.status_code == 409


async def test_confirm_plan_requires_dm(client, session_factory):
    await _seed_custom_plan(session_factory)
    plan_api._campaign_client = NotDmCampaignClient()
    resp = await client.post("/api/content/sessions/" + SESSION_ID + "/plan/confirm")
    assert resp.status_code == 403


async def test_confirm_plan_retries_a_failed_apply(client, session_factory):
    """A failed apply leaves the set in review: the DM confirms it again."""
    await _seed_custom_plan(session_factory)
    async with session_factory() as db:
        await plans.mark_draft(db, UUID(SESSION_ID), error="wiki down")
    plan_api._session_client = FakeSessionClient(status="failed")
    resp = await client.post("/api/content/sessions/" + SESSION_ID + "/plan/confirm")
    assert resp.status_code == 200


async def test_plan_routes_expose_expected_openapi(client):
    resp = await client.get("/openapi.json")
    paths = resp.json()["paths"]
    get_op = paths["/api/content/sessions/{session_id}/plan"]["get"]
    put_op = paths["/api/content/sessions/{session_id}/plan"]["put"]
    confirm_op = paths["/api/content/sessions/{session_id}/plan/confirm"]["post"]
    assert [p["name"] for p in get_op.get("parameters", [])] == ["session_id"]
    assert [p["name"] for p in put_op.get("parameters", [])] == ["session_id"]
    assert "requestBody" in put_op
    assert [p["name"] for p in confirm_op.get("parameters", [])] == ["session_id"]
    assert "requestBody" not in confirm_op
