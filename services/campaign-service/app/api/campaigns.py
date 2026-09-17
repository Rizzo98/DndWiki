"""Public campaign API (behind the gateway, JWT-authenticated users).

Authorization is enforced locally against the dnd_campaigns tables:

- any authenticated user may create a campaign (they become the DM)
- members may view the campaign and its roster
- the DM (campaigns.dm_user_id) alone may edit/archive it and manage
  members and invites
"""

from __future__ import annotations

import io
from typing import Any
from uuid import UUID

from dnd_common.auth import current_user
from dnd_common.db import get_session
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.core.config import ServiceSettings, get_settings
from app.deps import get_storage
from app.models import Campaign
from app.schemas import (
    CampaignCreate,
    CampaignMemberOut,
    CampaignOut,
    CampaignUpdate,
    InviteCreate,
    InviteOut,
    MemberAdd,
    MemberUpdate,
)
from app.storage import IMAGE_EXT_BY_MIME, ObjectStorage

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


def _user_id(user: dict[str, Any]) -> UUID:
    """Identity used across services is the Keycloak subject (UUID by default)."""
    return UUID(user["sub"])


def _out(campaign: Campaign, role: str | None, cover_url: str | None = None) -> CampaignOut:
    """CampaignOut with the calling user's role attached (my_role).

    cover_url is passed in by callers that have a storage handle (see
    _cover_url); it stays null elsewhere.
    """
    return CampaignOut.model_validate(campaign).model_copy(
        update={"my_role": role, "cover_url": cover_url}
    )


async def _cover_url(campaign: Campaign, storage: ObjectStorage) -> str | None:
    """Freshly presigned cover URL from settings' cover_uri ("bucket/key").

    The URL is short-lived and never persisted; a storage outage degrades to
    no cover.
    """
    uri = services.cover_uri(campaign)
    if uri is None:
        return None
    bucket, key = uri.split("/", 1)
    try:
        return await storage.presigned_get(bucket, key)
    except Exception:  # noqa: BLE001 - object storage down: no cover
        return None


async def _member_or_403(db: AsyncSession, campaign_id: UUID, user_id: UUID) -> str:
    """Return the caller's role in a campaign.

    404 when the campaign does not exist (no existence leak for strangers),
    403 when the caller is not a member.
    """
    await services.get_campaign_or_404(db, campaign_id)
    member = await services.get_member(db, campaign_id, user_id)
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not a member of this campaign"
        )
    return member.role


async def _dm_or_403(db: AsyncSession, campaign_id: UUID, user_id: UUID) -> Campaign:
    """Require the DM of a campaign (403 otherwise); returns the campaign."""
    campaign = await services.get_campaign_or_404(db, campaign_id)
    if campaign.dm_user_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="DM role required")
    return campaign


# ------------------------------------------------------------- campaigns


@router.post("", response_model=CampaignOut, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    body: CampaignCreate,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    storage: ObjectStorage = Depends(get_storage),
):
    """Create a campaign; the caller becomes its DM (creator -> 'dm' member)."""
    campaign = await services.create_campaign(
        db,
        name=body.name,
        slug=body.slug,
        description=body.description,
        language=body.language,
        settings=body.settings,
        dm_user_id=_user_id(user),
        dm_player_name=services.display_name_from_claims(user),
        members=[m.model_dump() for m in body.members],
    )
    return _out(campaign, services.DM_ROLE, await _cover_url(campaign, storage))


@router.get("", response_model=list[CampaignOut])
async def list_campaigns(
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    storage: ObjectStorage = Depends(get_storage),
):
    """Campaigns the caller is a member of, newest first (with their role)."""
    return [
        _out(campaign, role, await _cover_url(campaign, storage))
        for campaign, role in await services.list_campaigns_for_user(db, _user_id(user))
    ]


@router.get("/{campaign_id}", response_model=CampaignOut)
async def campaign_detail(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    storage: ObjectStorage = Depends(get_storage),
):
    """Campaign detail (member-only)."""
    role = await _member_or_403(db, campaign_id, _user_id(user))
    campaign = await services.get_campaign_or_404(db, campaign_id)
    return _out(campaign, role, await _cover_url(campaign, storage))


@router.patch("/{campaign_id}", response_model=CampaignOut)
async def update_campaign(
    campaign_id: UUID,
    body: CampaignUpdate,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    storage: ObjectStorage = Depends(get_storage),
):
    """DM edits campaign metadata."""
    await _dm_or_403(db, campaign_id, _user_id(user))
    campaign = await services.update_campaign(
        db, campaign_id, **body.model_dump(exclude_unset=True)
    )
    return _out(campaign, services.DM_ROLE, await _cover_url(campaign, storage))


@router.post("/{campaign_id}/archive", response_model=CampaignOut)
async def archive_campaign(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    storage: ObjectStorage = Depends(get_storage),
):
    """DM archives a campaign (status 'archived'; nothing is deleted)."""
    await _dm_or_403(db, campaign_id, _user_id(user))
    campaign = await services.set_campaign_status(db, campaign_id, services.ARCHIVED)
    return _out(campaign, services.DM_ROLE, await _cover_url(campaign, storage))


@router.post("/{campaign_id}/restore", response_model=CampaignOut)
async def restore_campaign(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    storage: ObjectStorage = Depends(get_storage),
):
    """DM reactivates an archived campaign (status 'active')."""
    await _dm_or_403(db, campaign_id, _user_id(user))
    campaign = await services.set_campaign_status(db, campaign_id, services.ACTIVE)
    return _out(campaign, services.DM_ROLE, await _cover_url(campaign, storage))


# ------------------------------------------------------------- cover


@router.put("/{campaign_id}/cover", response_model=CampaignOut)
async def upload_campaign_cover(
    campaign_id: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    storage: ObjectStorage = Depends(get_storage),
    settings: ServiceSettings = Depends(get_settings),
):
    """DM uploads/replaces the campaign cover (multipart image).

    The image is streamed to MinIO under campaign-assets/covers/{campaign_id}.{ext}
    (one cover per campaign) and its uri is stored in campaigns.settings
    (key "cover_uri"), so no migration is involved. Re-uploading with a
    different extension drops the superseded object.
    """
    campaign = await _dm_or_403(db, campaign_id, _user_id(user))

    content_type = (file.content_type or "").lower()
    ext = IMAGE_EXT_BY_MIME.get(content_type)
    if ext is None or content_type not in settings.allowed_image_mimes:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported cover type: {content_type or 'unknown'}",
        )
    data = await file.read()
    max_bytes = settings.max_image_mb * 1024 * 1024
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty cover file")
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Cover exceeds {settings.max_image_mb} MB limit",
        )

    bucket = settings.minio_campaign_assets_bucket
    key = f"covers/{campaign.id}.{ext}"
    uri = f"{bucket}/{key}"
    previous = services.cover_uri(campaign)
    await storage.put_object_stream(bucket, key, io.BytesIO(data), content_type)
    campaign = await services.set_cover_uri(db, campaign_id, uri)
    if previous is not None and previous != uri:
        old_bucket, old_key = previous.split("/", 1)
        await storage.delete_object(old_bucket, old_key)
    return _out(campaign, services.DM_ROLE, await _cover_url(campaign, storage))


@router.delete("/{campaign_id}/cover", status_code=status.HTTP_204_NO_CONTENT)
async def delete_campaign_cover(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    storage: ObjectStorage = Depends(get_storage),
):
    """DM removes the campaign cover (the object is best-effort deleted)."""
    campaign = await _dm_or_403(db, campaign_id, _user_id(user))
    uri = services.cover_uri(campaign)
    await services.set_cover_uri(db, campaign_id, None)
    if uri is not None:
        bucket, key = uri.split("/", 1)
        await storage.delete_object(bucket, key)


# ------------------------------------------------------------- members


@router.get("/{campaign_id}/members", response_model=list[CampaignMemberOut])
async def list_members(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
):
    """Campaign roster (member-only)."""
    await _member_or_403(db, campaign_id, _user_id(user))
    return await services.list_members(db, campaign_id)


@router.post(
    "/{campaign_id}/members",
    response_model=CampaignMemberOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    campaign_id: UUID,
    body: MemberAdd,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
):
    """DM adds a player to the campaign (player + character names, optional user link)."""
    await _dm_or_403(db, campaign_id, _user_id(user))
    return await services.add_member(
        db,
        campaign_id,
        player_name=body.player_name,
        character_name=body.character_name,
        character_description=body.character_description,
        user_id=body.user_id,
    )


@router.patch("/{campaign_id}/members/{member_id}", response_model=CampaignMemberOut)
async def update_member(
    campaign_id: UUID,
    member_id: UUID,
    body: MemberUpdate,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
):
    """DM edits a member: rename display data and/or (un)link a user."""
    await _dm_or_403(db, campaign_id, _user_id(user))
    return await services.update_member(
        db,
        campaign_id,
        member_id,
        player_name=body.player_name,
        character_name=body.character_name,
        character_description=body.character_description,
        user_id=body.user_id,
        unlink=body.unlink_user,
    )


@router.delete("/{campaign_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    campaign_id: UUID,
    member_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
):
    """DM removes a player by member id (the DM itself cannot be removed)."""
    await _dm_or_403(db, campaign_id, _user_id(user))
    await services.remove_member(db, campaign_id, member_id)


# ------------------------------------------------------------- invites


@router.post("/invites/{token}/accept", response_model=CampaignOut)
async def accept_invite(
    token: str,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    storage: ObjectStorage = Depends(get_storage),
):
    """Redeem a join token: become a member of the invite's campaign."""
    campaign = await services.accept_invite(
        db,
        token,
        user_id=_user_id(user),
        email=user.get("email"),
        player_name=services.display_name_from_claims(user),
    )
    role = await services.membership_role(db, campaign.id, _user_id(user))
    return _out(campaign, role, await _cover_url(campaign, storage))


@router.get("/{campaign_id}/invites", response_model=list[InviteOut])
async def list_invites(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
):
    """Pending invites (DM-only; includes the shareable token)."""
    await _dm_or_403(db, campaign_id, _user_id(user))
    return await services.list_invites(db, campaign_id)


@router.post(
    "/{campaign_id}/invites",
    response_model=InviteOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_invite(
    campaign_id: UUID,
    body: InviteCreate,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    settings: ServiceSettings = Depends(get_settings),
):
    """DM invites a player by email (one-time token, expires after ttl days)."""
    await _dm_or_403(db, campaign_id, _user_id(user))
    return await services.create_invite(
        db,
        campaign_id,
        email=body.email,
        role=body.role,
        ttl_days=settings.invite_ttl_days,
    )


@router.delete("/{campaign_id}/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(
    campaign_id: UUID,
    invite_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
):
    """DM revokes an invite (the link stops working)."""
    await _dm_or_403(db, campaign_id, _user_id(user))
    await services.revoke_invite(db, campaign_id, invite_id)