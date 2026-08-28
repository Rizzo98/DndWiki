"""Internal (dnd-services token) pipeline API tests."""

import uuid

from app import deps
from app.main import app
from app.models import WikiPage


async def _seed_page(
    session_factory,
    campaign_id,
    *,
    kind="character",
    title="Aragorn",
    slug=None,
    status="published",
    content=None,
):
    async with session_factory() as db:
        page = WikiPage(
            campaign_id=campaign_id,
            kind=kind,
            title=title,
            slug=slug or f"seed-{uuid.uuid4().hex[:8]}",
            status=status,
            visibility="public",
            content_json=content or {"body": "text"},
        )
        db.add(page)
        await db.commit()
        await db.refresh(page)
        return page


async def test_internal_pages_rejects_anonymous(client):
    campaign = uuid.uuid4()
    resp = await client.get(f"/internal/wiki/pages?campaign_id={campaign}")
    assert resp.status_code == 401  # no bearer token at all


async def test_internal_pages_lists_with_aliases(client, claims, session_factory):
    campaign = uuid.uuid4()
    # The client fixture fakes *user* auth only; internal endpoints verify a
    # dnd-services token via the realm JWKS, which does not exist in tests -
    # stub the dependency the same way conftest stubs current_user.
    app.dependency_overrides[deps.require_service] = lambda: None
    claims["sub"] = "dnd-services"
    claims["azp"] = "dnd-services"

    await _seed_page(
        session_factory,
        campaign,
        title="Fatumastra",
        slug="fatumastra",
        kind="location",
        content={"summary": "s", "aliases": ["Città libera"]},
    )
    archived = await _seed_page(
        session_factory,
        campaign,
        title="Old Junk",
        slug="old-junk",
        status="archived",
    )

    resp = await client.get(f"/internal/wiki/pages?campaign_id={campaign}")
    assert resp.status_code == 200
    pages = resp.json()
    assert [p["title"] for p in pages] == ["Fatumastra"]  # archived excluded
    assert pages[0]["aliases"] == ["Città libera"]
    assert set(pages[0]) == {
        "id", "title", "slug", "kind", "status", "aliases", "content_json",
    }
    assert str(archived.id) not in {p["id"] for p in pages}


async def _seed_event_page(session_factory, campaign_id, title="The Siege"):
    async with session_factory() as db:
        from app.models import WikiPage

        page = WikiPage(
            campaign_id=campaign_id,
            kind="event",
            title=title,
            slug=f"event-{uuid.uuid4().hex[:8]}",
            status="pending_review",
        )
        db.add(page)
        await db.commit()
        await db.refresh(page)
        return page


async def test_internal_timeline_lists_all_entries(client, claims, session_factory):
    app.dependency_overrides[deps.require_service] = lambda: None
    claims["sub"] = "dnd-services"
    claims["azp"] = "dnd-services"
    campaign = uuid.uuid4()
    async with session_factory() as db:
        from app.models import TimelineEvent

        db.add_all(
            [
                TimelineEvent(campaign_id=campaign, summary="approved", approved=True),
                TimelineEvent(campaign_id=campaign, summary="pending", approved=False),
            ]
        )
        await db.commit()

    resp = await client.get(f"/internal/wiki/timeline?campaign_id={campaign}")
    assert resp.status_code == 200
    body = resp.json()
    assert {e["summary"] for e in body} == {"approved", "pending"}


async def test_internal_timeline_upsert_creates_pending(client, claims, session_factory):
    app.dependency_overrides[deps.require_service] = lambda: None
    claims["sub"] = "dnd-services"
    claims["azp"] = "dnd-services"
    campaign = uuid.uuid4()
    page = await _seed_event_page(session_factory, campaign)

    resp = await client.post(
        "/internal/wiki/timeline/upsert",
        json={
            "campaign_id": str(campaign),
            "page_id": str(page.id),
            "in_world_date": "17 Ches 1492 DR",
            "summary": "The horde breaks against the walls.",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is True
    assert body["event"]["page_id"] == str(page.id)
    assert body["event"]["approved"] is False
    assert body["event"]["in_world_date"] == "17 Ches 1492 DR"


async def test_internal_timeline_upsert_updates_preserving_approval(
    client, claims, session_factory
):
    app.dependency_overrides[deps.require_service] = lambda: None
    claims["sub"] = "dnd-services"
    claims["azp"] = "dnd-services"
    campaign = uuid.uuid4()
    page = await _seed_event_page(session_factory, campaign)
    async with session_factory() as db:
        from app.models import TimelineEvent

        event = TimelineEvent(
            campaign_id=campaign,
            page_id=page.id,
            summary="old summary",
            approved=True,  # already DM-approved: must survive the refresh
        )
        db.add(event)
        await db.commit()
        await db.refresh(event)

    resp = await client.post(
        "/internal/wiki/timeline/upsert",
        json={
            "campaign_id": str(campaign),
            "page_id": str(page.id),
            "summary": "new summary with more detail",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is False
    assert body["event"]["summary"] == "new summary with more detail"
    assert body["event"]["approved"] is True  # DM approval not overwritten
