"""Tests for the internal change-set apply endpoint (the wiki write phase)."""

import uuid

from sqlalchemy import select

from app import deps
from app.main import app
from app.models import PageVersion, TimelineEvent, WikiPage

CAMPAIGN = uuid.UUID("22222222-2222-2222-2222-222222222222")
SESSION = uuid.UUID("11111111-1111-1111-1111-111111111111")
DM = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _service_auth(claims) -> None:
    """Internal endpoints verify a dnd-services token (stubbed in tests)."""
    app.dependency_overrides[deps.require_service] = lambda: None
    claims["sub"] = "dnd-services"
    claims["azp"] = "dnd-services"


def _change(**overrides) -> dict:
    change = {
        "change_id": "c1",
        "action": "create",
        "kind": "character",
        "title": "Aragorn",
        "content_json": {"physical_look": "Lean.", "facts": ["Speaks the password."],
                         "attributes": {"character_type": "player"}},
        "visibility": "public",
        "confidence": 0.9,
    }
    change.update(overrides)
    return change


async def _seed_page(session_factory, *, kind="event", title="Entering Moria", content=None):
    async with session_factory() as db:
        page = WikiPage(
            campaign_id=CAMPAIGN,
            kind=kind,
            title=title,
            slug=f"seed-{uuid.uuid4().hex[:8]}",
            status="published",
            visibility="public",
            content_json=content or {"summary": "Old."},
        )
        db.add(page)
        await db.commit()
        await db.refresh(page)
        return page


async def _pages(session_factory) -> list[WikiPage]:
    async with session_factory() as db:
        return list((await db.execute(select(WikiPage))).scalars().all())


async def test_apply_creates_published_pages(client, claims, session_factory, fake_publisher):
    """Generated pages land PUBLISHED: the DM confirmed the change set, so
    nothing pipeline-generated ever sits in 'pending review'."""
    _service_auth(claims)
    resp = await client.post(
        "/internal/wiki/changes/apply",
        json={
            "campaign_id": str(CAMPAIGN),
            "session_id": str(SESSION),
            "confirmed_by": str(DM),
            "changes": [_change()],
            "relations": [],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"][0]["title"] == "Aragorn"
    assert body["created"][0]["change_id"] == "c1"
    assert body["updated"] == []
    assert body["skipped"] == []

    pages = await _pages(session_factory)
    assert len(pages) == 1
    page = pages[0]
    assert page.status == "published"
    assert page.source_session_id == SESSION
    assert page.content_json["facts"] == ["Speaks the password."]
    assert page.content_json["attributes"]["character_type"] == "player"
    # the change set is traceable on the page and versioned like any edit
    async with session_factory() as db:
        versions = list((await db.execute(select(PageVersion))).scalars().all())
    assert len(versions) == 1
    assert "Confirmed change set" in (versions[0].change_note or "")
    # search-service indexes it: a published page emits wiki.published
    assert [ev.type for ev in fake_publisher.events] == ["wiki.published"]


async def test_apply_never_duplicates_a_documented_page(
    client, claims, session_factory, fake_publisher
):
    """A create the campaign already documents is SKIPPED, not duplicated."""
    _service_auth(claims)
    await _seed_page(session_factory, kind="character", title="Aragorn", content={})

    resp = await client.post(
        "/internal/wiki/changes/apply",
        json={
            "campaign_id": str(CAMPAIGN),
            "session_id": str(SESSION),
            "changes": [_change()],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] == []
    assert body["skipped"][0]["reason"] == "the campaign already documents this page"
    assert len(await _pages(session_factory)) == 1
    assert fake_publisher.events == []


async def test_apply_is_idempotent_on_a_retried_message(
    client, claims, session_factory, fake_publisher
):
    """Applying the same set twice writes the page once."""
    _service_auth(claims)
    payload = {
        "campaign_id": str(CAMPAIGN),
        "session_id": str(SESSION),
        "changes": [_change()],
    }
    first = await client.post("/internal/wiki/changes/apply", json=payload)
    second = await client.post("/internal/wiki/changes/apply", json=payload)
    assert first.json()["created"][0]["page_id"]
    assert second.json()["created"] == []
    assert len(await _pages(session_factory)) == 1


async def test_apply_updates_an_existing_event_page(client, claims, session_factory, fake_publisher):
    """An update rewrites the page content and its timeline entry."""
    _service_auth(claims)
    page = await _seed_page(session_factory)
    resp = await client.post(
        "/internal/wiki/changes/apply",
        json={
            "campaign_id": str(CAMPAIGN),
            "session_id": str(SESSION),
            "confirmed_by": str(DM),
            "changes": [
                _change(
                    change_id="c2",
                    action="update",
                    kind="event",
                    title="Entering Moria",
                    content_json={
                        "summary": "The party passes the gate.",
                        "attributes": {"participants": ["Aragorn"]},
                    },
                    timeline={"summary": "The party passes the gate.",
                              "in_world_date": "17 Ches 1492 DR"},
                    page_id=str(page.id),
                )
            ],
        },
    )
    assert resp.status_code == 200
    assert [c["action"] for c in resp.json()["updated"]] == ["update"]
    pages = await _pages(session_factory)
    assert pages[0].content_json["summary"] == "The party passes the gate."
    assert pages[0].content_json["attributes"]["participants"] == ["Aragorn"]
    async with session_factory() as db:
        events = list((await db.execute(select(TimelineEvent))).scalars().all())
    assert len(events) == 1
    # created from a CONFIRMED change set: it does not wait for approval
    assert events[0].approved is True
    assert events[0].page_id == page.id
    assert events[0].in_world_date == "17 Ches 1492 DR"
    assert [ev.type for ev in fake_publisher.events] == ["wiki.updated"]


async def test_apply_keeps_the_approval_of_an_existing_timeline_entry(
    client, claims, session_factory
):
    """A refreshed entry never flips the DM's own approval decision."""
    _service_auth(claims)
    page = await _seed_page(session_factory)
    async with session_factory() as db:
        db.add(
            TimelineEvent(
                campaign_id=CAMPAIGN,
                page_id=page.id,
                summary="Old summary.",
                approved=False,
            )
        )
        await db.commit()

    await client.post(
        "/internal/wiki/changes/apply",
        json={
            "campaign_id": str(CAMPAIGN),
            "session_id": str(SESSION),
            "changes": [
                _change(
                    change_id="c2",
                    action="update",
                    kind="event",
                    title="Entering Moria",
                    content_json={"summary": "New."},
                    timeline={"summary": "New.", "in_world_date": None},
                    page_id=str(page.id),
                )
            ],
        },
    )
    async with session_factory() as db:
        events = list((await db.execute(select(TimelineEvent))).scalars().all())
    assert len(events) == 1
    assert events[0].approved is False  # untouched
    assert events[0].summary == "New."


async def test_apply_creates_relations_resolved_by_title(
    client, claims, session_factory, fake_publisher
):
    """Cross-references resolve titles against what the set creates/exists."""
    _service_auth(claims)
    await _seed_page(session_factory, kind="faction", title="The Fellowship", content={})
    resp = await client.post(
        "/internal/wiki/changes/apply",
        json={
            "campaign_id": str(CAMPAIGN),
            "session_id": str(SESSION),
            "changes": [_change()],
            "relations": [
                {
                    "relation_id": "r1",
                    "from_title": "Aragorn",
                    "to_title": "The Fellowship",
                    "relation_type": "member_of",
                }
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["relations_created"] == 1

    from app.models import PageRelation

    async with session_factory() as db:
        relations = list((await db.execute(select(PageRelation))).scalars().all())
    assert len(relations) == 1
    assert relations[0].relation_type == "member_of"


async def test_apply_skips_an_update_whose_page_is_gone(client, claims, session_factory):
    """A page deleted between the review and the confirmation is reported."""
    _service_auth(claims)
    resp = await client.post(
        "/internal/wiki/changes/apply",
        json={
            "campaign_id": str(CAMPAIGN),
            "session_id": str(SESSION),
            "changes": [
                _change(
                    change_id="c3",
                    action="update",
                    kind="event",
                    title="Ghost",
                    page_id=str(uuid.uuid4()),
                    content_json={"summary": "x"},
                )
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["skipped"][0]["reason"] == "the page no longer exists"


async def test_apply_validates_the_payload(client, claims, session_factory):
    _service_auth(claims)
    resp = await client.post(
        "/internal/wiki/changes/apply",
        json={"campaign_id": str(CAMPAIGN), "session_id": str(SESSION), "changes": [
            {"change_id": "c1", "action": "create", "kind": "dragon", "title": "Smaug"}
        ]},
    )
    assert resp.status_code == 422


async def test_apply_requires_a_service_token(client, session_factory):
    resp = await client.post(
        "/internal/wiki/changes/apply",
        json={"campaign_id": str(CAMPAIGN), "session_id": str(SESSION), "changes": []},
    )
    assert resp.status_code == 401
