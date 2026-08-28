"""Timeline business logic: in-world events with DM approval.

Timeline events carry a campaign-specific calendar string (in_world_date) and
may point at a wiki page. Players only see approved events; the DM sees all
(LLM-proposed events land with approved=False and the DM approves them).
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TimelineEvent
from app.services.pages import get_page_or_404

logger = logging.getLogger(__name__)


async def create_timeline_event(
    db: AsyncSession,
    *,
    campaign_id: UUID,
    page_id: UUID | None = None,
    in_world_date: str | None = None,
    summary: str,
    source_session_id: UUID | None = None,
    approved: bool = True,
) -> TimelineEvent:
    """Persist a timeline event; the page (when given) must be in the campaign."""
    if page_id is not None:
        page = await get_page_or_404(db, page_id)
        if page.campaign_id != campaign_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="page_id does not belong to this campaign",
            )
    event = TimelineEvent(
        campaign_id=campaign_id,
        page_id=page_id,
        in_world_date=in_world_date,
        summary=summary,
        source_session_id=source_session_id,
        approved=approved,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    logger.info("timeline event %s created in campaign %s", event.id, campaign_id)
    return event


async def get_timeline_event_or_404(db: AsyncSession, event_id: UUID) -> TimelineEvent:
    event = await db.get(TimelineEvent, event_id)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Timeline event not found"
        )
    return event


async def list_timeline_events(
    db: AsyncSession, campaign_id: UUID, role: str
) -> list[TimelineEvent]:
    """Timeline entries the caller may see, newest first.

    Players see approved events only; the DM sees everything.
    """
    stmt = select(TimelineEvent).where(TimelineEvent.campaign_id == campaign_id)
    if role != "dm":
        stmt = stmt.where(TimelineEvent.approved.is_(True))
    stmt = stmt.order_by(TimelineEvent.created_at.desc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def update_timeline_event(db: AsyncSession, event_id: UUID, *, fields: dict) -> TimelineEvent:
    """DM edits a timeline entry; applies only the fields that were sent."""
    event = await get_timeline_event_or_404(db, event_id)
    if "page_id" in fields:
        new_page_id = fields["page_id"]
        if new_page_id is not None:
            page = await get_page_or_404(db, new_page_id)
            if page.campaign_id != event.campaign_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="page_id does not belong to this campaign",
                )
        event.page_id = new_page_id
    if "in_world_date" in fields:
        event.in_world_date = fields["in_world_date"]
    if "summary" in fields:
        event.summary = fields["summary"]
    if "approved" in fields:
        event.approved = fields["approved"]
    await db.commit()
    await db.refresh(event)
    logger.info("timeline event %s updated", event_id)
    return event


async def get_timeline_event_by_page(
    db: AsyncSession, campaign_id: UUID, page_id: UUID
) -> TimelineEvent | None:
    """The timeline entry backing an event page (one per page, or None)."""
    return await db.scalar(
        select(TimelineEvent).where(
            TimelineEvent.campaign_id == campaign_id,
            TimelineEvent.page_id == page_id,
        )
    )


async def delete_campaign_timeline_events(
    db: AsyncSession, campaign_id: UUID
) -> int:
    """DEBUG: hard-delete every timeline event of a campaign.

    The event PAGES are left untouched (they live in the wiki); only the
    timeline entries are wiped, so a fresh generation re-creates them as
    pending. Mirrors the pages reset: the one sanctioned exception to
    'timeline entries are never hard-deleted'.
    """
    result = await db.execute(
        delete(TimelineEvent).where(TimelineEvent.campaign_id == campaign_id)
    )
    await db.commit()
    deleted = result.rowcount or 0
    logger.info("deleted %d timeline events of campaign %s", deleted, campaign_id)
    return deleted


async def upsert_timeline_event(
    db: AsyncSession,
    *,
    campaign_id: UUID,
    page_id: UUID,
    in_world_date: str | None = None,
    summary: str,
    source_session_id: UUID | None = None,
) -> tuple[TimelineEvent, bool]:
    """Create or refresh the timeline entry for an event page.

    The content pipeline calls this after drafting an event page: new pages
    get a pending entry (approved=False, the DM approves it), pages that
    already have an entry get their summary/in-world date refreshed with the
    newly extracted information. The approval state is never touched here.
    """
    page = await get_page_or_404(db, page_id)
    if page.campaign_id != campaign_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="page_id does not belong to this campaign",
        )
    existing = await get_timeline_event_by_page(db, campaign_id, page_id)
    if existing is not None:
        existing.summary = summary
        if in_world_date is not None:
            existing.in_world_date = in_world_date
        if source_session_id is not None:
            existing.source_session_id = source_session_id
        await db.commit()
        await db.refresh(existing)
        logger.info("timeline event %s refreshed for page %s", existing.id, page_id)
        return existing, False
    event = TimelineEvent(
        campaign_id=campaign_id,
        page_id=page_id,
        in_world_date=in_world_date,
        summary=summary,
        source_session_id=source_session_id,
        approved=False,  # LLM-proposed: the DM approves it on the timeline
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    logger.info("timeline event %s created for page %s", event.id, page_id)
    return event, True
