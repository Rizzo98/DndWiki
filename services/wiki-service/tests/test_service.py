"""Service-layer tests: pages, versions, relations, timeline, visibility."""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app import services
from app.models import PageVersion, WikiPage


async def _seed_page(
    session_factory,
    campaign_id,
    *,
    kind="character",
    title="Aragorn",
    slug=None,
    status="published",
    visibility="public",
):
    async with session_factory() as db:
        page = WikiPage(
            campaign_id=campaign_id,
            kind=kind,
            title=title,
            slug=slug or f"seed-{uuid.uuid4().hex[:8]}",
            status=status,
            visibility=visibility,
        )
        db.add(page)
        await db.commit()
        await db.refresh(page)
        return page


async def _count(session_factory, model):
    async with session_factory() as db:
        return (await db.execute(select(func.count()).select_from(model))).scalar_one()


# ------------------------------------------------------------- pages


async def test_create_page_derives_slug_and_initial_version(session_factory):
    campaign = uuid.uuid4()
    async with session_factory() as db:
        page = await services.create_page(
            db, campaign_id=campaign, kind="character", title="Aragorn  Son of Arathorn"
        )
        assert page.slug == "aragorn-son-of-arathorn"
        assert page.status == "draft"
        assert page.visibility == "public"
        assert page.created_by is None

    assert await _count(session_factory, PageVersion) == 1


async def test_create_page_explicit_slug(session_factory):
    async with session_factory() as db:
        page = await services.create_page(
            db, campaign_id=uuid.uuid4(), kind="item", title="Tales", slug="one-ring"
        )
        assert page.slug == "one-ring"


async def test_create_page_slug_collision_within_campaign(session_factory):
    campaign = uuid.uuid4()
    async with session_factory() as db:
        first = await services.create_page(
            db, campaign_id=campaign, kind="character", title="Aragorn"
        )
        second = await services.create_page(
            db, campaign_id=campaign, kind="character", title="Aragorn"
        )
        assert first.slug == "aragorn"
        assert second.slug == "aragorn-2"


async def test_create_page_same_slug_other_campaign_ok(session_factory):
    async with session_factory() as db:
        first = await services.create_page(
            db, campaign_id=uuid.uuid4(), kind="character", title="Aragorn"
        )
        second = await services.create_page(
            db, campaign_id=uuid.uuid4(), kind="character", title="Aragorn"
        )
        assert first.slug == second.slug == "aragorn"


async def test_slugify():
    assert services.slugify("The One Ring") == "the-one-ring"
    assert services.slugify("  A B C  ") == "a-b-c"
    assert services.slugify("!!!") == "page"


# ------------------------------------------------------------- list/filter


async def test_list_pages_player_sees_only_published_public(session_factory):
    campaign = uuid.uuid4()
    visible = await _seed_page(session_factory, campaign)
    await _seed_page(session_factory, campaign, visibility="dm_only")
    await _seed_page(session_factory, campaign, status="draft")
    await _seed_page(session_factory, campaign, status="pending_review")
    await _seed_page(session_factory, campaign, status="archived")

    async with session_factory() as db:
        pages = await services.list_pages(db, campaign, "player")
        assert [p.id for p in pages] == [visible.id]


async def test_list_pages_dm_sees_everything(session_factory):
    campaign = uuid.uuid4()
    await _seed_page(session_factory, campaign)
    await _seed_page(session_factory, campaign, visibility="dm_only")
    await _seed_page(session_factory, campaign, status="draft")
    await _seed_page(session_factory, campaign, status="pending_review")
    await _seed_page(session_factory, campaign, status="archived")

    async with session_factory() as db:
        pages = await services.list_pages(db, campaign, "dm")
        assert len(pages) == 5


async def test_list_pages_does_not_leak_other_campaigns(session_factory):
    campaign = uuid.uuid4()
    await _seed_page(session_factory, campaign)
    await _seed_page(session_factory, uuid.uuid4(), title="Sauron")

    async with session_factory() as db:
        pages = await services.list_pages(db, campaign, "player")
        assert len(pages) == 1


async def test_list_pages_kind_and_q_filters(session_factory):
    campaign = uuid.uuid4()
    await _seed_page(session_factory, campaign, kind="character", title="Aragorn")
    await _seed_page(session_factory, campaign, kind="location", title="Rivendell")

    async with session_factory() as db:
        chars = await services.list_pages(db, campaign, "dm", kind="character")
        assert [p.title for p in chars] == ["Aragorn"]

        search = await services.list_pages(db, campaign, "dm", q="riven")
        assert [p.title for p in search] == ["Rivendell"]

        drafts = await services.list_pages(db, campaign, "dm", status="draft")
        assert drafts == []


async def test_list_pages_pagination(session_factory):
    campaign = uuid.uuid4()
    for i in range(5):
        await _seed_page(session_factory, campaign, title=f"Page {i}")

    async with session_factory() as db:
        page1 = await services.list_pages(db, campaign, "dm", limit=2, offset=0)
        page2 = await services.list_pages(db, campaign, "dm", limit=2, offset=2)
        assert len(page1) == 2 and len(page2) == 2


# ------------------------------------------------------------- update


async def test_update_page_writes_version_on_content_change(session_factory):
    campaign = uuid.uuid4()
    async with session_factory() as db:
        page = await services.create_page(
            db, campaign_id=campaign, kind="character", title="Aragorn", content_json={"body": "v1"}
        )
        updated = await services.update_page(
            db, page.id, content_json={"body": "v2"}, change_note="rewrote", updated_by=uuid.uuid4()
        )
        assert updated.content_json == {"body": "v2"}
        assert updated.updated_by is not None

    assert await _count(session_factory, PageVersion) == 2

    # title-only edits do not snapshot
    async with session_factory() as db:
        await services.update_page(db, updated.id, title="Aragorn II", updated_by=uuid.uuid4())
    assert await _count(session_factory, PageVersion) == 2


async def test_update_page_slug_unique_excludes_self(session_factory):
    async with session_factory() as db:
        page = await services.create_page(db, campaign_id=uuid.uuid4(), kind="item", title="Ring")
        updated = await services.update_page(db, page.id, slug=page.slug)
        assert updated.slug == page.slug


async def test_get_page_or_404(session_factory):
    async with session_factory() as db:
        with pytest.raises(HTTPException) as exc:
            await services.get_page_or_404(db, uuid.uuid4())
        assert exc.value.status_code == 404


# ------------------------------------------------------------- approval


async def test_approve_draft_publishes_and_emits(session_factory, fake_publisher):
    async with session_factory() as db:
        page = await services.create_page(
            db, campaign_id=uuid.uuid4(), kind="character", title="Aragorn", status="pending_review"
        )
        approved = await services.approve_page(
            db, page.id, approved_by=uuid.uuid4(), publisher=fake_publisher
        )
        assert approved.status == "published"
        assert approved.updated_by is not None

    assert fake_publisher.events[-1].type == "wiki.published"
    assert fake_publisher.events[-1].payload["page_id"] == str(page.id)


async def test_approve_already_published_409(session_factory, fake_publisher):
    async with session_factory() as db:
        page = await services.create_page(db, campaign_id=uuid.uuid4(), kind="character", title="A")
        await services.approve_page(db, page.id, approved_by=uuid.uuid4(), publisher=fake_publisher)
        with pytest.raises(HTTPException) as exc:
            await services.approve_page(
                db, page.id, approved_by=uuid.uuid4(), publisher=fake_publisher
            )
        assert exc.value.status_code == 409


async def test_approve_archived_409(session_factory, fake_publisher):
    async with session_factory() as db:
        page = await services.create_page(db, campaign_id=uuid.uuid4(), kind="character", title="A")
        await services.archive_page(db, page.id, updated_by=uuid.uuid4(), publisher=fake_publisher)
        with pytest.raises(HTTPException) as exc:
            await services.approve_page(
                db, page.id, approved_by=uuid.uuid4(), publisher=fake_publisher
            )
        assert exc.value.status_code == 409


async def test_archive_emits_and_is_terminal(session_factory, fake_publisher):
    async with session_factory() as db:
        page = await services.create_page(db, campaign_id=uuid.uuid4(), kind="character", title="A")
        archived = await services.archive_page(
            db, page.id, updated_by=uuid.uuid4(), publisher=fake_publisher
        )
        assert archived.status == "archived"
        with pytest.raises(HTTPException) as exc:
            await services.archive_page(
                db, page.id, updated_by=uuid.uuid4(), publisher=fake_publisher
            )
        assert exc.value.status_code == 409

    assert fake_publisher.events[-1].type == "wiki.archived"


async def test_set_visibility_published_emits_updated(session_factory, fake_publisher):
    async with session_factory() as db:
        page = await services.create_page(db, campaign_id=uuid.uuid4(), kind="character", title="A")
        await services.approve_page(db, page.id, approved_by=uuid.uuid4(), publisher=fake_publisher)
        hidden = await services.set_visibility(
            db, page.id, "hidden", updated_by=uuid.uuid4(), publisher=fake_publisher
        )
        assert hidden.visibility == "hidden"

    assert fake_publisher.events[-1].type == "wiki.updated"


async def test_set_visibility_draft_no_event(session_factory, fake_publisher):
    async with session_factory() as db:
        page = await services.create_page(db, campaign_id=uuid.uuid4(), kind="character", title="A")
        await services.set_visibility(
            db, page.id, "dm_only", updated_by=uuid.uuid4(), publisher=fake_publisher
        )
    assert fake_publisher.events == []


async def test_visible_to():
    from app.models import WikiPage

    page = WikiPage(campaign_id=uuid.uuid4(), kind="character", title="A", slug="a")
    page.status, page.visibility = "published", "public"
    assert services.visible_to(page, "player")
    assert services.visible_to(page, "dm")
    page.visibility = "dm_only"
    assert not services.visible_to(page, "player")
    assert services.visible_to(page, "dm")
    page.status = "archived"
    assert not services.visible_to(page, "player")


# ------------------------------------------------------------- versions


async def test_list_versions_newest_first(session_factory):
    async with session_factory() as db:
        page = await services.create_page(db, campaign_id=uuid.uuid4(), kind="character", title="A")
        await services.update_page(db, page.id, content_json={"v": 2}, updated_by=uuid.uuid4())
        await services.update_page(db, page.id, content_json={"v": 3}, updated_by=uuid.uuid4())
        versions = await services.list_versions(db, page.id)
        assert [v.version_no for v in versions] == [3, 2, 1]
        assert versions[0].content_json == {"v": 3}  # newest snapshot first
        assert versions[1].content_json == {"v": 2}
        assert versions[2].content_json == {}  # initial snapshot


# ------------------------------------------------------------- relations


async def test_create_and_list_relations(session_factory):
    campaign = uuid.uuid4()
    async with session_factory() as db:
        page = await services.create_page(
            db, campaign_id=campaign, kind="character", title="Aragorn"
        )
        related = await services.create_page(
            db, campaign_id=campaign, kind="faction", title="Fellowship"
        )
        rel = await services.create_relation(db, page.id, related.id, "member_of")

        rows = await services.list_relations(db, page.id)
        assert len(rows) == 1
        (row, title, slug) = rows[0]
        assert row.id == rel.id
        assert title == "Fellowship"
        assert slug == "fellowship"


async def test_create_relation_duplicate_409(session_factory):
    campaign = uuid.uuid4()
    async with session_factory() as db:
        page = await services.create_page(db, campaign_id=campaign, kind="character", title="A")
        related = await services.create_page(db, campaign_id=campaign, kind="faction", title="B")
        await services.create_relation(db, page.id, related.id, "member_of")
        with pytest.raises(HTTPException) as exc:
            await services.create_relation(db, page.id, related.id, "member_of")
        assert exc.value.status_code == 409


async def test_create_relation_self_400(session_factory):
    async with session_factory() as db:
        page = await services.create_page(db, campaign_id=uuid.uuid4(), kind="character", title="A")
        with pytest.raises(HTTPException) as exc:
            await services.create_relation(db, page.id, page.id, "member_of")
        assert exc.value.status_code == 400


async def test_create_relation_cross_campaign_400(session_factory):
    async with session_factory() as db:
        page = await services.create_page(db, campaign_id=uuid.uuid4(), kind="character", title="A")
        other = await services.create_page(db, campaign_id=uuid.uuid4(), kind="faction", title="B")
        with pytest.raises(HTTPException) as exc:
            await services.create_relation(db, page.id, other.id, "member_of")
        assert exc.value.status_code == 400


async def test_delete_relation(session_factory):
    campaign = uuid.uuid4()
    async with session_factory() as db:
        page = await services.create_page(db, campaign_id=campaign, kind="character", title="A")
        related = await services.create_page(db, campaign_id=campaign, kind="faction", title="B")
        rel = await services.create_relation(db, page.id, related.id, "member_of")
        await services.delete_relation(db, page.id, rel.id)
        assert await services.list_relations(db, page.id) == []


# ------------------------------------------------------------- timeline


async def test_create_timeline_event(session_factory):
    campaign = uuid.uuid4()
    async with session_factory() as db:
        event = await services.create_timeline_event(
            db,
            campaign_id=campaign,
            in_world_date="17 Ches 1492 DR",
            summary="The Fellowship departs Rivendell",
            approved=True,
        )
        assert event.campaign_id == campaign
        assert event.approved is True
        assert event.created_at is not None


async def test_create_timeline_event_page_in_campaign(session_factory):
    campaign = uuid.uuid4()
    async with session_factory() as db:
        page = await services.create_page(db, campaign_id=campaign, kind="quest", title="Departure")
        event = await services.create_timeline_event(
            db, campaign_id=campaign, page_id=page.id, summary="Departure"
        )
        assert event.page_id == page.id


async def test_create_timeline_event_page_other_campaign_400(session_factory):
    async with session_factory() as db:
        page = await services.create_page(
            db, campaign_id=uuid.uuid4(), kind="quest", title="Elsewhere"
        )
        with pytest.raises(HTTPException) as exc:
            await services.create_timeline_event(
                db, campaign_id=uuid.uuid4(), page_id=page.id, summary="Nope"
            )
        assert exc.value.status_code == 400


async def test_list_timeline_events_role_filter(session_factory):
    campaign = uuid.uuid4()
    async with session_factory() as db:
        await services.create_timeline_event(
            db, campaign_id=campaign, summary="approved", approved=True
        )
        await services.create_timeline_event(
            db, campaign_id=campaign, summary="pending", approved=False
        )
        await services.create_timeline_event(
            db, campaign_id=uuid.uuid4(), summary="other", approved=True
        )

        player_events = await services.list_timeline_events(db, campaign, "player")
        assert [e.summary for e in player_events] == ["approved"]

        dm_events = await services.list_timeline_events(db, campaign, "dm")
        assert {e.summary for e in dm_events} == {"approved", "pending"}


async def test_update_timeline_event(session_factory):
    campaign = uuid.uuid4()
    async with session_factory() as db:
        event = await services.create_timeline_event(
            db, campaign_id=campaign, summary="v1", in_world_date="1 Mirtul 1492 DR", approved=False
        )
        updated = await services.update_timeline_event(
            db, event.id, fields={"summary": "v2", "approved": True, "in_world_date": None}
        )
        assert updated.summary == "v2"
        assert updated.approved is True
        assert updated.in_world_date is None  # explicit null clears the date


async def test_get_timeline_event_or_404(session_factory):
    async with session_factory() as db:
        with pytest.raises(HTTPException) as exc:
            await services.get_timeline_event_or_404(db, uuid.uuid4())
        assert exc.value.status_code == 404
