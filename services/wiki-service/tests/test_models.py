"""Model roundtrip tests against in-memory SQLite."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import PageRelation, PageVersion, TimelineEvent, WikiPage


async def _page(session_factory, campaign_id=None, slug="aragorn", title="Aragorn"):
    async with session_factory() as db:
        page = WikiPage(
            campaign_id=campaign_id or uuid.uuid4(),
            kind="character",
            title=title,
            slug=slug,
        )
        db.add(page)
        await db.commit()
        await db.refresh(page)
        return page


async def test_page_defaults(session_factory):
    async with session_factory() as db:
        page = WikiPage(
            campaign_id=uuid.uuid4(), kind="location", title="Rivendell", slug="rivendell"
        )
        db.add(page)
        await db.commit()
        await db.refresh(page)

        assert page.status == "draft"
        assert page.visibility == "public"
        assert page.content_json == {}
        assert page.confidence is None
        assert page.source_session_id is None
        assert page.created_at is not None
        assert page.updated_at is not None


async def test_page_slug_unique_within_campaign(session_factory):
    campaign = uuid.uuid4()
    await _page(session_factory, campaign_id=campaign, slug="same-slug")
    async with session_factory() as db:
        db.add(WikiPage(campaign_id=campaign, kind="item", title="Other", slug="same-slug"))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_page_slug_allowed_across_campaigns(session_factory):
    await _page(session_factory, campaign_id=uuid.uuid4(), slug="same-slug")
    other = await _page(session_factory, campaign_id=uuid.uuid4(), slug="same-slug")
    assert other.slug == "same-slug"


async def test_page_version_snapshot(session_factory):
    page = await _page(session_factory)
    async with session_factory() as db:
        db.add(
            PageVersion(
                page_id=page.id,
                content_json={"body": "v1"},
                change_note="initial",
                created_by=uuid.uuid4(),
            )
        )
        await db.commit()
        versions = (await db.execute(select(PageVersion))).scalars().all()
        assert len(versions) == 1
        assert versions[0].content_json == {"body": "v1"}
        assert versions[0].change_note == "initial"
        assert versions[0].created_at is not None


async def test_page_relation_unique_triple(session_factory):
    page = await _page(session_factory)
    related = await _page(session_factory, campaign_id=page.campaign_id, slug="fellowship")
    async with session_factory() as db:
        db.add(PageRelation(page_id=page.id, related_page_id=related.id, relation_type="member_of"))
        await db.commit()

    async with session_factory() as db:
        db.add(PageRelation(page_id=page.id, related_page_id=related.id, relation_type="member_of"))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_timeline_event_defaults(session_factory):
    async with session_factory() as db:
        event = TimelineEvent(campaign_id=uuid.uuid4(), summary="The heroes set out")
        db.add(event)
        await db.commit()
        await db.refresh(event)
        assert event.approved is False
        assert event.page_id is None
        assert event.in_world_date is None
        assert event.created_at is not None
