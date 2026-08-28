"""Campaign business logic: campaigns, memberships, roles, invites.

The service layer owns the invariants:

- exactly one member row carries role "dm" — the user recorded in
  campaigns.dm_user_id (set when the campaign is created). Everyone else
  joins as "player".
- a campaign is never hard-deleted: status moves active <-> archived.
- invite tokens are one-time and expire; accepting an invite whose email is
  set requires the caller's JWT email to match.

Routers only translate HTTP <-> service calls and enforce who may call what.
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Campaign, CampaignMember, Invite

logger = logging.getLogger(__name__)

DM_ROLE = "dm"
PLAYER_ROLE = "player"
ACTIVE = "active"
ARCHIVED = "archived"
DEFAULT_PLAYER_NAME = "Player"
DEFAULT_DM_NAME = "Dungeon Master"

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def ensure_utc(dt: datetime) -> datetime:
    """Normalize a datetime to UTC-aware.

    SQLite returns naive datetimes even for DateTime(timezone=True); Postgres
    returns aware ones. Treat naive values as UTC so comparisons are safe on
    both backends.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def slugify(name: str) -> str:
    """Derive a URL-safe slug from a campaign name."""
    slug = _SLUG_STRIP.sub("-", name.lower()).strip("-")
    return slug or "campaign"


def display_name_from_claims(claims: dict) -> str:
    """Pick a sensible default player name from the JWT claims."""
    return (
        claims.get("preferred_username")
        or claims.get("given_name")
        or claims.get("name")
        or DEFAULT_PLAYER_NAME
    )


# ------------------------------------------------------------- getters


async def get_campaign(db: AsyncSession, campaign_id: UUID) -> Campaign | None:
    return await db.get(Campaign, campaign_id)


async def get_campaign_or_404(db: AsyncSession, campaign_id: UUID) -> Campaign:
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
    return campaign


async def get_member(db: AsyncSession, campaign_id: UUID, user_id: UUID) -> CampaignMember | None:
    """Look up the membership row linked to a user, or None."""
    return await db.scalar(
        select(CampaignMember).where(
            CampaignMember.campaign_id == campaign_id,
            CampaignMember.user_id == user_id,
        )
    )


async def get_member_by_id(db: AsyncSession, member_id: UUID) -> CampaignMember | None:
    """Look up a membership row by its surrogate id, or None."""
    return await db.get(CampaignMember, member_id)


async def unique_slug(db: AsyncSession, base: str, exclude_id: UUID | None = None) -> str:
    """Return a slug unique across campaigns (appends -2, -3, ... on collision)."""
    candidate = base
    n = 2
    while True:
        existing = await db.scalar(select(Campaign.id).where(Campaign.slug == candidate))
        if existing is None or existing == exclude_id:
            return candidate
        candidate = f"{base}-{n}"
        n += 1


# ------------------------------------------------------------- campaigns


async def create_campaign(
    db: AsyncSession,
    *,
    name: str,
    dm_user_id: UUID,
    slug: str | None = None,
    description: str | None = None,
    settings: dict | None = None,
    dm_player_name: str | None = None,
) -> Campaign:
    """Persist a campaign and make the creator its DM (role 'dm' member row).

    The DM member row carries player_name (claim-derived when available,
    "Dungeon Master" otherwise) and character_name "Dungeon Master"; the DM
    can edit both later via PATCH /members/{id}.
    """
    final_slug = await unique_slug(db, slug or slugify(name))
    campaign = Campaign(
        name=name,
        slug=final_slug,
        description=description,
        dm_user_id=dm_user_id,
        settings=settings or {},
    )
    db.add(campaign)
    await db.flush()  # get campaign.id for the member row
    db.add(
        CampaignMember(
            campaign_id=campaign.id,
            user_id=dm_user_id,
            role=DM_ROLE,
            player_name=dm_player_name or DEFAULT_DM_NAME,
            character_name=DEFAULT_DM_NAME,
        )
    )
    await db.commit()
    await db.refresh(campaign)
    logger.info("campaign %s created (slug=%s) by user %s", campaign.id, final_slug, dm_user_id)
    return campaign


async def list_campaigns_for_user(db: AsyncSession, user_id: UUID) -> list[tuple[Campaign, str]]:
    """All campaigns the user is a member of (newest first) with their role."""
    result = await db.execute(
        select(Campaign, CampaignMember.role)
        .join(CampaignMember, CampaignMember.campaign_id == Campaign.id)
        .where(CampaignMember.user_id == user_id)
        .order_by(Campaign.created_at.desc())
    )
    return [(campaign, role) for campaign, role in result.all()]


async def update_campaign(
    db: AsyncSession,
    campaign_id: UUID,
    *,
    name: str | None = None,
    slug: str | None = None,
    description: str | None = None,
    settings: dict | None = None,
) -> Campaign:
    """DM edits campaign metadata (None fields are left untouched)."""
    campaign = await get_campaign_or_404(db, campaign_id)
    if name is not None:
        campaign.name = name
    if slug is not None:
        campaign.slug = await unique_slug(db, slug, exclude_id=campaign.id)
    if description is not None:
        campaign.description = description
    if settings is not None:
        campaign.settings = settings
    await db.commit()
    await db.refresh(campaign)
    return campaign


async def set_campaign_status(db: AsyncSession, campaign_id: UUID, new_status: str) -> Campaign:
    """Move a campaign between active and archived (idempotent-ish: same state 409s)."""
    campaign = await get_campaign_or_404(db, campaign_id)
    if campaign.status == new_status:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Campaign already {new_status}",
        )
    campaign.status = new_status
    await db.commit()
    await db.refresh(campaign)
    logger.info("campaign %s -> %s", campaign_id, new_status)
    return campaign


# ------------------------------------------------------------- members


async def list_members(db: AsyncSession, campaign_id: UUID) -> list[CampaignMember]:
    await get_campaign_or_404(db, campaign_id)
    result = await db.execute(
        select(CampaignMember)
        .where(CampaignMember.campaign_id == campaign_id)
        .order_by(CampaignMember.joined_at)
    )
    return list(result.scalars().all())


async def add_member(
    db: AsyncSession,
    campaign_id: UUID,
    *,
    player_name: str,
    character_name: str,
    user_id: UUID | None = None,
    role: str = PLAYER_ROLE,
) -> CampaignMember:
    """DM adds a player to a campaign (v1: always as 'player').

    player_name/character_name are the DM-curated display data; user_id is
    the optional link to an existing platform user. Linking a user that is
    already a member of the campaign is rejected with 409 (the DB unique
    constraint (campaign_id, user_id) backs this up).
    """
    await get_campaign_or_404(db, campaign_id)
    if user_id is not None and await get_member(db, campaign_id, user_id) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User is already a member")
    member = CampaignMember(
        campaign_id=campaign_id,
        user_id=user_id,
        role=role,
        player_name=player_name,
        character_name=character_name,
    )
    db.add(member)
    await db.commit()
    await db.refresh(member)
    return member


async def _member_in_campaign_or_404(
    db: AsyncSession, campaign_id: UUID, member_id: UUID
) -> CampaignMember:
    """The member row, scoped to the campaign (404 otherwise)."""
    member = await get_member_by_id(db, member_id)
    if member is None or member.campaign_id != campaign_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    return member


async def update_member(
    db: AsyncSession,
    campaign_id: UUID,
    member_id: UUID,
    *,
    player_name: str | None = None,
    character_name: str | None = None,
    user_id: UUID | None = None,
    unlink: bool = False,
) -> CampaignMember:
    """DM edits a member: rename display data and/or (un)link a user.

    The DM member row is always linked to campaigns.dm_user_id, so its
    user link cannot be changed. Linking a user that is already linked to
    another member of the campaign is rejected with 409.
    """
    await get_campaign_or_404(db, campaign_id)
    member = await _member_in_campaign_or_404(db, campaign_id, member_id)
    if unlink and user_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot link and unlink in the same request",
        )
    if member.role == DM_ROLE and (user_id is not None or unlink):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The DM user link cannot be changed",
        )
    if player_name is not None:
        member.player_name = player_name
    if character_name is not None:
        member.character_name = character_name
    if unlink:
        member.user_id = None
    elif user_id is not None:
        existing = await get_member(db, campaign_id, user_id)
        if existing is not None and existing.id != member.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="User is already a member of this campaign",
            )
        member.user_id = user_id
    await db.commit()
    await db.refresh(member)
    return member


async def remove_member(db: AsyncSession, campaign_id: UUID, member_id: UUID) -> None:
    """DM removes a player from a campaign (the DM itself cannot be removed)."""
    campaign = await get_campaign_or_404(db, campaign_id)
    member = await _member_in_campaign_or_404(db, campaign_id, member_id)
    if member.role == DM_ROLE or member.user_id == campaign.dm_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The DM cannot be removed; transfer the DM role first",
        )
    await db.delete(member)
    await db.commit()
    logger.info("member %s removed from campaign %s", member_id, campaign_id)


async def membership_role(db: AsyncSession, campaign_id: UUID, user_id: UUID) -> str | None:
    """The user's role ("dm" | "player") in a campaign, or None if not a member.

    Raises 404 when the campaign itself does not exist (the caller — e.g.
    session-service's client — treats 404 as "not a member").
    """
    await get_campaign_or_404(db, campaign_id)
    member = await get_member(db, campaign_id, user_id)
    return member.role if member is not None else None


# ------------------------------------------------------------- invites


async def create_invite(
    db: AsyncSession,
    campaign_id: UUID,
    *,
    email: str | None = None,
    role: str = PLAYER_ROLE,
    ttl_days: int = 7,
) -> Invite:
    """DM creates a one-time join token for a campaign (expires after ttl_days)."""
    await get_campaign_or_404(db, campaign_id)
    token = secrets.token_urlsafe(32)
    invite = Invite(
        campaign_id=campaign_id,
        email=email,
        role=role,
        token=token,
        expires_at=datetime.now(UTC) + timedelta(days=ttl_days),
    )
    db.add(invite)
    await db.commit()
    await db.refresh(invite)
    logger.info("invite %s created for campaign %s", invite.id, campaign_id)
    return invite


async def list_invites(db: AsyncSession, campaign_id: UUID) -> list[Invite]:
    await get_campaign_or_404(db, campaign_id)
    result = await db.execute(
        select(Invite).where(Invite.campaign_id == campaign_id).order_by(Invite.created_at.desc())
    )
    return list(result.scalars().all())


async def revoke_invite(db: AsyncSession, campaign_id: UUID, invite_id: UUID) -> None:
    """DM revokes an invite (hard delete — a revoked link stops working)."""
    await get_campaign_or_404(db, campaign_id)
    invite = await db.get(Invite, invite_id)
    if invite is None or invite.campaign_id != campaign_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found")
    await db.delete(invite)
    await db.commit()


async def accept_invite(
    db: AsyncSession,
    token: str,
    *,
    user_id: UUID,
    email: str | None,
    player_name: str | None = None,
) -> Campaign:
    """Redeem a join token: validate, add the member, mark the invite used.

    - unknown token        -> 404
    - already used         -> 410
    - expired              -> 410
    - email pinned and the caller's JWT email does not match -> 403
    """
    invite = await db.scalar(select(Invite).where(Invite.token == token))
    if invite is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found")
    if invite.used_at is not None:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Invite already used")
    if invite.expires_at is not None and ensure_utc(invite.expires_at) < datetime.now(UTC):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Invite expired")
    if invite.email:
        if not email:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="An email claim is required to accept this invite",
            )
        if email.lower() != invite.email.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invite email does not match your account",
            )

    campaign = await get_campaign_or_404(db, invite.campaign_id)
    if await get_member(db, campaign.id, user_id) is None:
        db.add(
            CampaignMember(
                campaign_id=campaign.id,
                user_id=user_id,
                role=invite.role,
                player_name=player_name or DEFAULT_PLAYER_NAME,
                character_name="",  # the DM fills the character in later
            )
        )
    invite.used_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(campaign)
    logger.info("user %s joined campaign %s via invite %s", user_id, campaign.id, invite.id)
    return campaign
