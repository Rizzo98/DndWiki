"""GET /internal/wiki/sessions/{id}/content: what a session wrote (guard).

session-service deletes a session only while this listing is empty: pages and
timeline entries attributed to a session must never outlive it.
"""

import uuid
from datetime import UTC, datetime

from app import deps
from app.main import app
from app.models import TimelineEvent, WikiPage


async def _seed_page(session_factory, campaign_id, session_id, *, status="published", title="Aragorn"):
    async with session_factory() as db:
        page = WikiPage(
            campaign_id=campaign_id,
            kind="character",
            title=title,
            slug=f"seed-{uuid.uuid4().hex[:8]}",
            status=status,
            visibility="public",
            content_json={"body": "text"},
            source_session_id=session_id,
        )
        db.add(page)
        await db.commit()
        await db.refresh(page)
        return page


async def _seed_timeline(session_factory, campaign_id, session_id, *, approved=True):
    async with session_factory() as db:
        event = TimelineEvent(
            campaign_id=campaign_id,
            summary="The gate opens.",
            approved=approved,
            source_session_id=session_id,
            created_at=datetime.now(UTC),
        )
        db.add(event)
        await db.commit()
        await db.refresh(event)
        return event


async def test_session_content_lists_its_pages_and_timeline(
    client, claims, session_factory
):
    app.dependency_overrides[deps.require_service] = lambda: None
    claims["sub"] = "dnd-services"
    claims["azp"] = "dnd-services"

    session = uuid.uuid4()
    campaign = uuid.uuid4()
    page = await _seed_page(session_factory, campaign, session)
    await _seed_page(session_factory, campaign, session, title="Moria", status="draft")
    await _seed_page(session_factory, campaign, None)  # no session: not counted
    event = await _seed_timeline(session_factory, campaign, session)

    resp = await client.get(f"/internal/wiki/sessions/{session}/content")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == str(session)
    assert sorted(p["title"] for p in body["pages"]) == ["Aragorn", "Moria"]
    assert body["published"] == 1
    assert [e["summary"] for e in body["timeline"]] == ["The gate opens."]
    assert str(page.id) in {p["id"] for p in body["pages"]}
    assert body["timeline"][0]["id"] == str(event.id)
    assert body["pages"][0]["kind"] == "character"


async def test_session_content_is_empty_for_an_unwritten_session(
    client, claims, session_factory
):
    app.dependency_overrides[deps.require_service] = lambda: None
    claims["sub"] = "dnd-services"
    claims["azp"] = "dnd-services"

    campaign = uuid.uuid4()
    await _seed_page(session_factory, campaign, uuid.uuid4())

    resp = await client.get(f"/internal/wiki/sessions/{uuid.uuid4()}/content")
    assert resp.status_code == 200
    assert resp.json() == {
        "session_id": resp.json()["session_id"],
        "pages": [],
        "timeline": [],
        "published": 0,
    }


async def test_session_content_requires_a_service_token(client):
    resp = await client.get(f"/internal/wiki/sessions/{uuid.uuid4()}/content")
    assert resp.status_code == 401
