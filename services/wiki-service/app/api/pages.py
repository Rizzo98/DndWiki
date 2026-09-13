"""Public wiki API (behind the gateway, JWT-authenticated users).

Authorization is enforced locally per query:

- membership + role come from campaign-service (service-token client)
- players read only published + public pages of their campaign
- the DM reads everything and alone may create/edit/approve/archive pages,
  change visibility, or manage relations
- POST /api/wiki/pages and POST /api/wiki/pages/{id}/relations also accept
  a dnd-services client token so a service can create DRAFT pages and tag
  possible duplicates (DM approval is still required to publish). The
  content pipeline itself writes its confirmed pages through the internal
  change-set apply endpoint, never as pending drafts.
"""

from __future__ import annotations

import io
from typing import Any
from uuid import UUID

from dnd_common.auth import current_user, is_developer
from dnd_common.db import get_session
from dnd_common.events import Event
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.broker import EventPublisher
from app.clients.campaigns import CampaignServiceClient, MembershipUnavailable
from app.core.config import ServiceSettings, get_settings
from app.deps import get_campaign_client, get_publisher, get_storage
from app.models import WikiPage
from app.schemas import (
    PAGE_KIND,
    PAGE_STATUS,
    PageCreate,
    PageOut,
    PageRelationCreate,
    PageRelationOut,
    PageSummaryOut,
    PageUpdate,
    PageVersionOut,
    VisibilityUpdate,
)
from app.storage import IMAGE_EXT_BY_MIME, ObjectStorage

router = APIRouter(prefix="/api/wiki", tags=["wiki"])


def _user_id(user: dict[str, Any]) -> UUID:
    """Identity used across services is the Keycloak subject (UUID by default)."""
    return UUID(user["sub"])


def _is_service(claims: dict[str, Any], settings: ServiceSettings) -> bool:
    """True when the token is a dnd-services client-credentials token."""
    return claims.get("azp") == settings.keycloak_service_client_id


async def _member_or_403(
    campaign_client: CampaignServiceClient, campaign_id: UUID, user_id: UUID
) -> str:
    """Return the caller's role in a campaign or raise 403/503."""
    try:
        return await campaign_client.assert_member(campaign_id, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except MembershipUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="campaign-service unavailable"
        ) from exc


async def _dm_or_403(
    campaign_client: CampaignServiceClient, campaign_id: UUID, user_id: UUID
) -> None:
    """Require the DM role for a campaign (403 otherwise)."""
    try:
        await campaign_client.assert_dm(campaign_id, user_id)
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except MembershipUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="campaign-service unavailable"
        ) from exc


def _page_visible_or_403(page: WikiPage, role: str) -> None:
    """Players may only read published + public pages."""
    if role != "dm" and not services.visible_to(page, role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Page is not visible to your role",
        )


async def _member_page(
    db: AsyncSession, campaign_client: CampaignServiceClient, page_id: UUID, user_id: UUID
) -> tuple[WikiPage, str]:
    """Load a page, verify membership in its campaign, enforce visibility."""
    page = await services.get_page_or_404(db, page_id)
    role = await _member_or_403(campaign_client, page.campaign_id, user_id)
    _page_visible_or_403(page, role)
    return page, role


async def _out_page(page: WikiPage, storage: ObjectStorage) -> PageOut:
    """PageOut plus a freshly presigned portrait URL (when one is set).

    content_json.image_uri is stored as "bucket/key"; the presigned URL is
    short-lived and never persisted. A storage outage degrades to no image.
    """
    out = PageOut.model_validate(page)
    image_uri = (page.content_json or {}).get("image_uri")
    if isinstance(image_uri, str) and "/" in image_uri:
        bucket, key = image_uri.split("/", 1)
        try:
            out.image_url = await storage.presigned_get(bucket, key)
        except Exception:  # noqa: BLE001 - object storage down: no portrait
            out.image_url = None
    return out


# ------------------------------------------------------------- pages


@router.delete("/campaigns/{campaign_id}/pages")
async def delete_all_pages(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
    publisher: EventPublisher = Depends(get_publisher),
):
    """DEBUG: hard-delete every page of a campaign (DM or 'dev' role).

    Developer reset for iterating on wiki generation: unlike the rest of the
    API this bypasses the "never hard-deleted" invariant. wiki.archived is
    emitted per previously-published page so search unindexes the documents.
    """
    if not is_developer(user):
        await _dm_or_403(campaign_client, campaign_id, _user_id(user))
    deleted, published_payloads = await services.delete_campaign_pages(db, campaign_id)
    for payload in published_payloads:
        await publisher.publish(Event(type="wiki.archived", payload=payload))
    return {"campaign_id": str(campaign_id), "deleted": deleted}


@router.get("/pages", response_model=list[PageSummaryOut])
async def list_pages(
    campaign_id: UUID,
    kind: PAGE_KIND | None = None,
    status: PAGE_STATUS | None = None,
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
):
    """Pages the caller may see in a campaign (players: published+public only)."""
    role = await _member_or_403(campaign_client, campaign_id, _user_id(user))
    return await services.list_pages(
        db, campaign_id, role, kind=kind, status=status, q=q, limit=limit, offset=offset
    )


@router.get("/pages/{page_id}", response_model=PageOut)
async def page_detail(
    page_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
    storage: ObjectStorage = Depends(get_storage),
):
    """Full page body (member-only, visibility-filtered)."""
    page, _ = await _member_page(db, campaign_client, page_id, _user_id(user))
    return await _out_page(page, storage)


@router.post("/pages", response_model=PageOut, status_code=status.HTTP_201_CREATED)
async def create_page(
    body: PageCreate,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
    publisher: EventPublisher = Depends(get_publisher),
    settings: ServiceSettings = Depends(get_settings),
    storage: ObjectStorage = Depends(get_storage),
):
    """Create a page.

    - DM (user token): any status; the caller becomes created_by.
    - service token: drafts only — publishing requires DM approval. Emits
      wiki.draft_ready so the draft can be picked up for review.
    """
    is_service = _is_service(user, settings)
    if is_service:
        if body.status not in services.SERVICE_CREATE_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Service tokens may only create drafts (status 'draft')",
            )
        page = await services.create_page(
            db,
            campaign_id=body.campaign_id,
            kind=body.kind,
            title=body.title,
            slug=body.slug,
            content_json=body.content_json,
            status=body.status,
            visibility=body.visibility,
            confidence=body.confidence,
            source_session_id=body.source_session_id,
            change_note=body.change_note,
        )
    else:
        user_id = _user_id(user)
        await _dm_or_403(campaign_client, body.campaign_id, user_id)
        page = await services.create_page(
            db,
            campaign_id=body.campaign_id,
            kind=body.kind,
            title=body.title,
            slug=body.slug,
            content_json=body.content_json,
            status=body.status,
            visibility=body.visibility,
            confidence=body.confidence,
            source_session_id=body.source_session_id,
            created_by=user_id,
            change_note=body.change_note,
        )

    if is_service and page.status == services.DRAFT:
        await publisher.publish(
            Event(
                type="wiki.draft_ready",
                payload={
                    "campaign_id": str(page.campaign_id),
                    "session_id": str(page.source_session_id) if page.source_session_id else None,
                    "draft_count": 1,
                },
            )
        )
    return await _out_page(page, storage)


@router.patch("/pages/{page_id}", response_model=PageOut)
async def update_page(
    page_id: UUID,
    body: PageUpdate,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
    publisher: EventPublisher = Depends(get_publisher),
    storage: ObjectStorage = Depends(get_storage),
    settings: ServiceSettings = Depends(get_settings),
):
    """Edit page content/metadata; published pages emit wiki.updated.

    The DM edits anything; the content-service (service token) may only
    refresh content (title/slug/content_json/change_note) of event pages it
    drafted — status and visibility are out of reach, so LLM updates can
    never publish or hide a page.
    """
    page = await services.get_page_or_404(db, page_id)
    if _is_service(user, settings):
        updated_by = None
    else:
        user_id = _user_id(user)
        await _dm_or_403(campaign_client, page.campaign_id, user_id)
        updated_by = user_id
    updated = await services.update_page(
        db, page_id, **body.model_dump(exclude_unset=True), updated_by=updated_by
    )
    if updated.status == services.PUBLISHED:
        await publisher.publish(
            Event(type="wiki.updated", payload=services.page_event_payload(updated))
        )
    return await _out_page(updated, storage)


@router.put("/pages/{page_id}/image", response_model=PageOut)
async def upload_page_image(
    page_id: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
    storage: ObjectStorage = Depends(get_storage),
    publisher: EventPublisher = Depends(get_publisher),
    settings: ServiceSettings = Depends(get_settings),
):
    """DM uploads/replaces the character portrait (multipart image).

    The image is streamed to MinIO under wiki-assets/characters/{page_id}.{ext}
    (one portrait per page; re-uploading overwrites it) and its uri is saved on
    content_json.image_uri — which, like every content change, writes an
    immutable version snapshot.
    """
    page = await services.get_page_or_404(db, page_id)
    user_id = _user_id(user)
    await _dm_or_403(campaign_client, page.campaign_id, user_id)

    content_type = (file.content_type or "").lower()
    ext = IMAGE_EXT_BY_MIME.get(content_type)
    if ext is None or content_type not in settings.allowed_image_mimes:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported image type: {content_type or 'unknown'}",
        )
    data = await file.read()
    max_bytes = settings.max_image_mb * 1024 * 1024
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty image file")
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Image exceeds {settings.max_image_mb} MB limit",
        )

    key = f"characters/{page.id}.{ext}"
    uri = f"{settings.minio_wiki_assets_bucket}/{key}"
    await storage.put_object_stream(
        settings.minio_wiki_assets_bucket, key, io.BytesIO(data), content_type
    )
    updated = await services.update_page(
        db,
        page_id,
        content_json={**page.content_json, "image_uri": uri},
        change_note="Character portrait updated",
        updated_by=user_id,
    )
    if updated.status == services.PUBLISHED:
        await publisher.publish(
            Event(type="wiki.updated", payload=services.page_event_payload(updated))
        )
    return await _out_page(updated, storage)


@router.post("/pages/{page_id}/approve", response_model=PageOut)
async def approve_page(
    page_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
    publisher: EventPublisher = Depends(get_publisher),
    storage: ObjectStorage = Depends(get_storage),
):
    """DM approval: draft -> published (emits wiki.published)."""
    page = await services.get_page_or_404(db, page_id)
    user_id = _user_id(user)
    await _dm_or_403(campaign_client, page.campaign_id, user_id)
    approved = await services.approve_page(db, page_id, approved_by=user_id, publisher=publisher)
    return await _out_page(approved, storage)


@router.post("/pages/{page_id}/archive", response_model=PageOut)
async def archive_page(
    page_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
    publisher: EventPublisher = Depends(get_publisher),
    storage: ObjectStorage = Depends(get_storage),
):
    """DM archives a page (nothing is deleted; emits wiki.archived)."""
    page = await services.get_page_or_404(db, page_id)
    user_id = _user_id(user)
    await _dm_or_403(campaign_client, page.campaign_id, user_id)
    archived = await services.archive_page(db, page_id, updated_by=user_id, publisher=publisher)
    return await _out_page(archived, storage)


@router.patch("/pages/{page_id}/visibility", response_model=PageOut)
async def set_visibility(
    page_id: UUID,
    body: VisibilityUpdate,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
    publisher: EventPublisher = Depends(get_publisher),
    storage: ObjectStorage = Depends(get_storage),
):
    """DM sets page visibility (public | dm_only | hidden)."""
    page = await services.get_page_or_404(db, page_id)
    user_id = _user_id(user)
    await _dm_or_403(campaign_client, page.campaign_id, user_id)
    updated = await services.set_visibility(
        db, page_id, body.visibility, updated_by=user_id, publisher=publisher
    )
    return await _out_page(updated, storage)


# ------------------------------------------------------------- versions


@router.get("/pages/{page_id}/versions", response_model=list[PageVersionOut])
async def page_versions(
    page_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
):
    """Version history of a visible page, newest first."""
    await _member_page(db, campaign_client, page_id, _user_id(user))
    return await services.list_versions(db, page_id)


# ------------------------------------------------------------- relations


@router.get("/pages/{page_id}/relations", response_model=list[PageRelationOut])
async def page_relations(
    page_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
):
    """Cross-references of a visible page."""
    await _member_page(db, campaign_client, page_id, _user_id(user))
    return [
        PageRelationOut(
            id=rel.id,
            page_id=rel.page_id,
            related_page_id=rel.related_page_id,
            relation_type=rel.relation_type,
            created_at=rel.created_at,
            related_title=title,
            related_slug=slug,
        )
        for rel, title, slug in await services.list_relations(db, page_id)
    ]


@router.post(
    "/pages/{page_id}/relations",
    response_model=PageRelationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_page_relation(
    page_id: UUID,
    body: PageRelationCreate,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
    settings: ServiceSettings = Depends(get_settings),
):
    """DM proposes a cross-reference between two pages.

    content-service (service token) may also propose relations: it tags fresh
    drafts with 'possible_duplicate' when they look like existing pages. The
    service layer still enforces same-campaign + no self-relations.
    """
    page = await services.get_page_or_404(db, page_id)
    if not _is_service(user, settings):
        user_id = _user_id(user)
        await _dm_or_403(campaign_client, page.campaign_id, user_id)
    rel = await services.create_relation(db, page_id, body.related_page_id, body.relation_type)
    related = await services.get_page_or_404(db, rel.related_page_id)
    return PageRelationOut(
        id=rel.id,
        page_id=rel.page_id,
        related_page_id=rel.related_page_id,
        relation_type=rel.relation_type,
        created_at=rel.created_at,
        related_title=related.title,
        related_slug=related.slug,
    )


@router.delete("/pages/{page_id}/relations/{relation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_page_relation(
    page_id: UUID,
    relation_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
):
    """DM removes a proposed relation."""
    page = await services.get_page_or_404(db, page_id)
    user_id = _user_id(user)
    await _dm_or_403(campaign_client, page.campaign_id, user_id)
    await services.delete_relation(db, page_id, relation_id)