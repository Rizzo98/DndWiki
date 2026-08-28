"""Internal pipeline API (service-to-service, dnd-services client token).

content-service fetches the flat page listing of a campaign before drafting
so it can dedupe against what the wiki already documents (no more duplicate
pages for entities that exist under a different name). Nothing here is for
end users - the public API stays under /api/wiki behind membership checks.
"""

from __future__ import annotations

from uuid import UUID

from dnd_common.db import get_session
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.deps import require_service
from app.models import TimelineEvent, WikiPage
from app.schemas import ExistingPageOut, TimelineEventOut, TimelineUpsert, TimelineUpsertOut

router = APIRouter(
    prefix="/internal/wiki",
    tags=["internal"],
    dependencies=[Depends(require_service)],
)


def _timeline_out(event: TimelineEvent) -> TimelineEventOut:
    """TimelineEventOut for an internal response (no page title enrichment;
    the content pipeline works with page ids)."""
    return TimelineEventOut.model_validate(event)


def _aliases_of(content_json: dict | None) -> list[str]:
    """Stored alias strings of a page ('aliases' key of content_json)."""
    raw = (content_json or {}).get("aliases")
    if not isinstance(raw, list):
        return []
    return [a for a in raw if isinstance(a, str)]


@router.get("/pages", response_model=list[ExistingPageOut])
async def list_campaign_pages(
    campaign_id: UUID,
    limit: int = Query(default=200, ge=1, le=500),
    db: AsyncSession = Depends(get_session),
):
    """Flat listing of a campaign's pages for cross-run draft dedupe.

    Archived pages are excluded: they left the wiki on purpose and must not
    swallow new extractions.
    """
    pages: list[WikiPage] = await services.list_pages(db, campaign_id, "dm", limit=limit)
    return [
        ExistingPageOut(
            id=page.id,
            title=page.title,
            slug=page.slug,
            kind=page.kind,
            status=page.status,
            aliases=_aliases_of(page.content_json),
            content_json=page.content_json,
        )
        for page in pages
        if page.status != services.ARCHIVED
    ]


@router.get("/timeline", response_model=list[TimelineEventOut])
async def list_campaign_timeline(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_session),
):
    """All timeline entries of a campaign (approved or not) for the pipeline.

    The content-service uses this to tell which events the campaign timeline
    already documents before proposing new ones.
    """
    events = await services.list_timeline_events(db, campaign_id, "dm")
    return [_timeline_out(event) for event in events]


@router.post("/timeline/upsert", response_model=TimelineUpsertOut)
async def upsert_timeline(
    body: TimelineUpsert,
    db: AsyncSession = Depends(get_session),
):
    """Create or refresh the timeline entry for an event page.

    New pages get a pending entry (approved=False); existing entries are
    updated with the freshly extracted summary/in-world date. The DM's
    approval state is never overwritten.
    """
    event, created = await services.upsert_timeline_event(
        db,
        campaign_id=body.campaign_id,
        page_id=body.page_id,
        in_world_date=body.in_world_date,
        summary=body.summary,
        source_session_id=body.source_session_id,
    )
    return TimelineUpsertOut(event=_timeline_out(event), created=created)
