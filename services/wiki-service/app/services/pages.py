"""Wiki page business logic: CRUD, draft review, visibility, versions, relations.

The service layer owns the invariants:

- every page belongs to exactly one campaign; slugs are unique per campaign
- a page is never hard-deleted: status moves draft -> pending_review ->
  published | archived (archived is terminal in v1)
- players may only ever read published + public pages; the DM reads everything
- every content change writes an immutable page_versions snapshot
- wiki.published / wiki.updated / wiki.archived are emitted via the publisher
  (search + notification services consume them)

Routers only translate HTTP <-> service calls and enforce who may call what.
"""

from __future__ import annotations

import logging
import re
from uuid import UUID

from dnd_common.events import Event
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.broker import EventPublisher
from app.models import PageRelation, PageVersion, WikiPage
from app.page_attributes import validate_attributes

logger = logging.getLogger(__name__)

DRAFT = "draft"
PENDING_REVIEW = "pending_review"
PUBLISHED = "published"
ARCHIVED = "archived"

PUBLIC = "public"
DM_ONLY = "dm_only"
HIDDEN = "hidden"

#: statuses the content-service (service token) may create drafts with
SERVICE_CREATE_STATUSES = (DRAFT, PENDING_REVIEW)

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(title: str) -> str:
    """Derive a URL-safe slug from a page title."""
    slug = _SLUG_STRIP.sub("-", title.lower()).strip("-")
    return slug or "page"


def page_event_payload(page: WikiPage) -> dict:
    """Payload shared by wiki.published / wiki.updated / wiki.archived."""
    return {
        "page_id": str(page.id),
        "campaign_id": str(page.campaign_id),
        "slug": page.slug,
        "kind": page.kind,
        "visibility": page.visibility,
    }


def visible_to(page: WikiPage, role: str) -> bool:
    """Whether a caller with role ('dm' | 'player') may read this page."""
    return role == "dm" or (page.status == PUBLISHED and page.visibility == PUBLIC)


# ------------------------------------------------------------- getters


async def get_page(db: AsyncSession, page_id: UUID) -> WikiPage | None:
    return await db.get(WikiPage, page_id)


async def get_page_or_404(db: AsyncSession, page_id: UUID) -> WikiPage:
    page = await db.get(WikiPage, page_id)
    if page is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found")
    return page


async def unique_slug(
    db: AsyncSession, campaign_id: UUID, base: str, exclude_id: UUID | None = None
) -> str:
    """Return a slug unique within the campaign (appends -2, -3, ... on collision)."""
    candidate = base
    n = 2
    while True:
        existing = await db.scalar(
            select(WikiPage.id).where(
                WikiPage.campaign_id == campaign_id, WikiPage.slug == candidate
            )
        )
        if existing is None or existing == exclude_id:
            return candidate
        candidate = f"{base}-{n}"
        n += 1


# ------------------------------------------------------------- pages


async def create_page(
    db: AsyncSession,
    *,
    campaign_id: UUID,
    kind: str,
    title: str,
    slug: str | None = None,
    content_json: dict | None = None,
    status: str = DRAFT,
    visibility: str = PUBLIC,
    confidence: float | None = None,
    source_session_id: UUID | None = None,
    created_by: UUID | None = None,
    change_note: str | None = None,
) -> WikiPage:
    """Persist a page and its initial version snapshot."""
    content_json = validate_attributes(kind, content_json)
    final_slug = await unique_slug(db, campaign_id, slug or slugify(title))
    page = WikiPage(
        campaign_id=campaign_id,
        kind=kind,
        title=title,
        slug=final_slug,
        content_json=content_json or {},
        status=status,
        visibility=visibility,
        confidence=confidence,
        source_session_id=source_session_id,
        created_by=created_by,
        updated_by=created_by,
    )
    db.add(page)
    await db.flush()  # get page.id for the initial version row
    db.add(
        PageVersion(
            page_id=page.id,
            version_no=1,
            content_json=page.content_json,
            change_note=change_note,
            created_by=created_by,
        )
    )
    await db.commit()
    await db.refresh(page)
    logger.info("page %s created (slug=%s) in campaign %s", page.id, final_slug, campaign_id)
    return page


async def list_pages(
    db: AsyncSession,
    campaign_id: UUID,
    role: str,
    *,
    kind: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[WikiPage]:
    """Pages the caller may see in a campaign, newest-updated first.

    Players always get status='published' AND visibility='public'; the DM gets
    everything (optionally filtered by status). kind/q filters apply to both.
    """
    stmt = select(WikiPage).where(WikiPage.campaign_id == campaign_id)
    if role != "dm":
        stmt = stmt.where(WikiPage.status == PUBLISHED, WikiPage.visibility == PUBLIC)
    elif status is not None:
        stmt = stmt.where(WikiPage.status == status)
    if kind is not None:
        stmt = stmt.where(WikiPage.kind == kind)
    if q:
        stmt = stmt.where(WikiPage.title.ilike(f"%{q}%"))
    stmt = stmt.order_by(WikiPage.updated_at.desc()).limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def update_page(
    db: AsyncSession,
    page_id: UUID,
    *,
    title: str | None = None,
    slug: str | None = None,
    content_json: dict | None = None,
    change_note: str | None = None,
    updated_by: UUID | None = None,
) -> WikiPage:
    """DM edits page metadata/content; a content change writes a version."""
    page = await get_page_or_404(db, page_id)
    if title is not None:
        page.title = title
    if slug is not None:
        page.slug = await unique_slug(db, page.campaign_id, slug, exclude_id=page.id)
    if content_json is not None:
        content_json = validate_attributes(page.kind, content_json)
        next_no = (
            await db.scalar(
                select(func.max(PageVersion.version_no)).where(PageVersion.page_id == page.id)
            )
        ) or 0
        db.add(
            PageVersion(
                page_id=page.id,
                version_no=next_no + 1,
                content_json=content_json,  # snapshot the state after this change
                change_note=change_note,
                created_by=updated_by,
            )
        )
        page.content_json = content_json
    page.updated_by = updated_by
    await db.commit()
    await db.refresh(page)
    logger.info("page %s updated by %s", page_id, updated_by)
    return page


async def approve_page(
    db: AsyncSession, page_id: UUID, *, approved_by: UUID, publisher: EventPublisher
) -> WikiPage:
    """DM approval: draft|pending_review -> published; emits wiki.published."""
    page = await get_page_or_404(db, page_id)
    if page.status == ARCHIVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Archived pages cannot be approved"
        )
    if page.status == PUBLISHED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Page is already published"
        )
    page.status = PUBLISHED
    page.updated_by = approved_by
    await db.commit()
    await db.refresh(page)
    logger.info("page %s approved -> published by %s", page_id, approved_by)
    await publisher.publish(Event(type="wiki.published", payload=page_event_payload(page)))
    return page


async def archive_page(
    db: AsyncSession, page_id: UUID, *, updated_by: UUID, publisher: EventPublisher
) -> WikiPage:
    """DM archives a page (nothing is deleted); emits wiki.archived."""
    page = await get_page_or_404(db, page_id)
    if page.status == ARCHIVED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Page is already archived")
    page.status = ARCHIVED
    page.updated_by = updated_by
    await db.commit()
    await db.refresh(page)
    logger.info("page %s archived by %s", page_id, updated_by)
    await publisher.publish(Event(type="wiki.archived", payload=page_event_payload(page)))
    return page


async def set_visibility(
    db: AsyncSession,
    page_id: UUID,
    visibility: str,
    *,
    updated_by: UUID,
    publisher: EventPublisher,
) -> WikiPage:
    """DM sets page visibility; published pages emit wiki.updated (search re-index)."""
    page = await get_page_or_404(db, page_id)
    page.visibility = visibility
    page.updated_by = updated_by
    await db.commit()
    await db.refresh(page)
    if page.status == PUBLISHED:
        # search must re-index (hidden pages leave the public index)
        await publisher.publish(Event(type="wiki.updated", payload=page_event_payload(page)))
    return page


# ------------------------------------------------------------- versions


async def list_versions(db: AsyncSession, page_id: UUID) -> list[PageVersion]:
    """Version history of a page, newest first (per-page version counter)."""
    await get_page_or_404(db, page_id)
    result = await db.execute(
        select(PageVersion)
        .where(PageVersion.page_id == page_id)
        .order_by(PageVersion.version_no.desc())
    )
    return list(result.scalars().all())


# ------------------------------------------------------------- relations


async def create_relation(
    db: AsyncSession, page_id: UUID, related_page_id: UUID, relation_type: str
) -> PageRelation:
    """Propose a cross-reference between two pages of the same campaign."""
    page = await get_page_or_404(db, page_id)
    related = await get_page_or_404(db, related_page_id)
    if related.id == page.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="A page cannot relate to itself"
        )
    if related.campaign_id != page.campaign_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Relations are scoped to a single campaign",
        )
    existing = await db.scalar(
        select(PageRelation.id).where(
            PageRelation.page_id == page_id,
            PageRelation.related_page_id == related_page_id,
            PageRelation.relation_type == relation_type,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Relation already exists")
    relation = PageRelation(
        page_id=page_id, related_page_id=related_page_id, relation_type=relation_type
    )
    db.add(relation)
    await db.commit()
    await db.refresh(relation)
    logger.info("relation %s->%s (%s)", page_id, related_page_id, relation_type)
    return relation


async def list_relations(
    db: AsyncSession, page_id: UUID
) -> list[tuple[PageRelation, str | None, str | None]]:
    """Relations of a page plus the related page's title/slug for display."""
    await get_page_or_404(db, page_id)
    result = await db.execute(
        select(PageRelation, WikiPage.title, WikiPage.slug)
        .join(WikiPage, WikiPage.id == PageRelation.related_page_id)
        .where(PageRelation.page_id == page_id)
        .order_by(PageRelation.created_at)
    )
    return [(rel, title, slug) for rel, title, slug in result.all()]


async def delete_relation(db: AsyncSession, page_id: UUID, relation_id: UUID) -> None:
    """DM removes a proposed relation (hard delete — links are not pages)."""
    await get_page_or_404(db, page_id)
    relation = await db.get(PageRelation, relation_id)
    if relation is None or relation.page_id != page_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Relation not found")
    await db.delete(relation)
    await db.commit()


# ------------------------------------------------------------- debug reset


async def delete_campaign_pages(db: AsyncSession, campaign_id: UUID) -> tuple[int, list[dict]]:
    """DEBUG: hard-delete EVERY page of a campaign (versions/relations cascade).

    This is the one sanctioned exception to the "pages are never hard-deleted"
    invariant — a developer-only reset used while iterating on wiki generation.
    Returns (deleted_count, event_payloads): the caller must emit wiki.archived
    for every previously published page so search unindexes them.
    """
    pages = list(
        (
            await db.execute(select(WikiPage).where(WikiPage.campaign_id == campaign_id))
        ).scalars().all()
    )
    published_payloads = [page_event_payload(p) for p in pages if p.status == PUBLISHED]
    for page in pages:
        await db.delete(page)
    await db.commit()
    logger.warning(
        "DEBUG reset: deleted %d pages from campaign %s", len(pages), campaign_id
    )
    return len(pages), published_payloads
