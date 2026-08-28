"""Timeline API: in-world events with DM approval (member-only read).

Events back their own wiki page: the DM creates an entry (optionally linked
to an event page), and the content-service drafts event pages + pending
timeline entries through the internal endpoints (service token).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from dnd_common.auth import current_user, is_developer
from dnd_common.db import get_session
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.clients.campaigns import CampaignServiceClient
from app.core.config import ServiceSettings, get_settings
from app.deps import get_campaign_client
from app.models import TimelineEvent, WikiPage
from app.schemas import TimelineEventCreate, TimelineEventOut, TimelineEventUpdate

from .pages import _dm_or_403, _is_service, _member_or_403, _user_id

router = APIRouter(prefix="/api/wiki", tags=["wiki"])


async def _with_page_titles(
    db: AsyncSession, events: list[TimelineEvent]
) -> list[TimelineEventOut]:
    """TimelineEventOut rows with the linked event page's title/slug filled."""
    if not events:
        return []
    page_ids = [e.page_id for e in events if e.page_id is not None]
    titles: dict[UUID, tuple[str, str]] = {}
    if page_ids:
        rows = (
            await db.execute(select(WikiPage.id, WikiPage.title, WikiPage.slug).where(
                WikiPage.id.in_(page_ids)
            ))
        ).all()
        titles = {row.id: (row.title, row.slug) for row in rows}
    out: list[TimelineEventOut] = []
    for event in events:
        item = TimelineEventOut.model_validate(event)
        if event.page_id is not None and event.page_id in titles:
            item.page_title, item.page_slug = titles[event.page_id]
        out.append(item)
    return out


@router.get("/timeline", response_model=list[TimelineEventOut])
async def timeline(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
):
    """Timeline entries the caller may see (players: approved only)."""
    role = await _member_or_403(campaign_client, campaign_id, _user_id(user))
    events = await services.list_timeline_events(db, campaign_id, role)
    return await _with_page_titles(db, events)


@router.post("/timeline", response_model=TimelineEventOut, status_code=201)
async def create_timeline_event(
    body: TimelineEventCreate,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
    settings: ServiceSettings = Depends(get_settings),
):
    """Add a timeline entry.

    The DM creates approved-by-default entries and may link an event page.
    The content-service (service token) proposes entries for freshly drafted
    event pages: those always land as approved=False for DM review.
    """
    if _is_service(user, settings):
        approved = False
    else:
        user_id = _user_id(user)
        await _dm_or_403(campaign_client, body.campaign_id, user_id)
        approved = body.approved
    return await services.create_timeline_event(
        db,
        campaign_id=body.campaign_id,
        page_id=body.page_id,
        in_world_date=body.in_world_date,
        summary=body.summary,
        source_session_id=body.source_session_id,
        approved=approved,
    )


@router.patch("/timeline/{event_id}", response_model=TimelineEventOut)
async def update_timeline_event(
    event_id: UUID,
    body: TimelineEventUpdate,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
):
    """DM edits/approves a timeline entry."""
    event = await services.get_timeline_event_or_404(db, event_id)
    user_id = _user_id(user)
    await _dm_or_403(campaign_client, event.campaign_id, user_id)
    updated = await services.update_timeline_event(
        db, event_id, fields=body.model_dump(exclude_unset=True)
    )
    return (await _with_page_titles(db, [updated]))[0]


@router.delete("/campaigns/{campaign_id}/timeline")
async def delete_campaign_timeline(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
):
    """DEBUG: hard-delete every timeline event of a campaign.

    Developer reset for iterating on timeline generation (same contract as
    DELETE /api/wiki/campaigns/{id}/pages): the 'dev' realm role unlocks it
    outside any membership; otherwise the campaign DM may call it. The linked
    event pages are left in the wiki — only the timeline entries are wiped,
    so a fresh generation re-creates them as pending.
    """
    if not is_developer(user):
        await _dm_or_403(campaign_client, campaign_id, _user_id(user))
    deleted = await services.delete_campaign_timeline_events(db, campaign_id)
    return {"campaign_id": str(campaign_id), "deleted": deleted}
