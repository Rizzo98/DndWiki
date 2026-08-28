"""User profile business logic.

The identity source of truth is Keycloak; this service keeps the local mirror
(users table) that other services reference by user_id. Rows are
lazy-provisioned from JWT claims on first access - there is no signup API
here. Profiles are never hard-deleted in v1 (Keycloak manages accounts).

If a Keycloak account is deleted and re-registered with the same email, the
new subject adopts the existing row (see get_or_create_user) so profiles,
voiceprints and cross-service user_id references survive the account switch.
"""

from __future__ import annotations

import logging
import uuid
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User

logger = logging.getLogger(__name__)

DEFAULT_DISPLAY_NAME = "Player"


def display_name_from_claims(claims: dict) -> str:
    """Pick a sensible default display name from the JWT claims."""
    return (
        claims.get("preferred_username")
        or claims.get("given_name")
        or claims.get("name")
        or DEFAULT_DISPLAY_NAME
    )


# ------------------------------------------------------------- getters


async def get_user_by_sub(db: AsyncSession, keycloak_sub: str) -> User | None:
    return await db.scalar(select(User).where(User.keycloak_sub == keycloak_sub))


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    """Case-insensitive lookup (Keycloak normalizes emails to lowercase)."""
    return await db.scalar(select(User).where(func.lower(User.email) == email.lower()))


async def get_user_by_id(db: AsyncSession, user_id: UUID) -> User | None:
    return await db.get(User, user_id)


async def get_user_or_404(db: AsyncSession, user_id: UUID) -> User:
    user = await db.get(User, user_id)
    if user is None:
        # Re-registration moves keycloak_sub while users.id stays; callers may
        # hold either value (campaign members / session speaker assignments
        # store the sub, voiceprint payloads the stable id), so resolve by the
        # current sub as well - otherwise one of them 404s and the UI falls
        # back to showing the raw uuid instead of the display name.
        user = await get_user_by_sub(db, str(user_id))
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


async def get_or_create_user(
    db: AsyncSession,
    *,
    keycloak_sub: str,
    email: str | None = None,
    display_name: str | None = None,
) -> User:
    """Return the user row for a Keycloak subject, creating it on first access.

    users.id doubles as the cross-service user_id, which is the Keycloak
    subject at creation time (a UUID by default): campaign-service stores the
    subject in campaign_members.user_id, so the same value must appear in
    voice_profiles.user_id for rosters to join. The email mirror is only
    written when the claim is present.

    If the subject is new but the email already exists, the account was
    re-registered after the old Keycloak user was deleted (Keycloak never
    reuses subjects): the existing row is adopted and re-linked to the new
    subject. The stable id - and therefore every voice profile and foreign key
    in other services - is kept; only keycloak_sub moves. This is what makes
    "delete the Keycloak account, register again with the same email" work
    instead of failing on the email unique constraint.
    """
    user = await get_user_by_sub(db, keycloak_sub)
    if user is not None:
        return user
    if email:
        existing = await get_user_by_email(db, email)
        if existing is not None:
            logger.info(
                "re-linking user %s from sub=%s to sub=%s (email %s reused)",
                existing.id,
                existing.keycloak_sub,
                keycloak_sub,
                email,
            )
            existing.keycloak_sub = keycloak_sub
            await db.commit()
            await db.refresh(existing)
            return existing
    try:
        user_id = UUID(keycloak_sub)
    except ValueError:  # non-UUID subjects get a generated id (rare)
        user_id = uuid.uuid4()
    user = User(
        id=user_id,
        keycloak_sub=keycloak_sub,
        email=email,
        display_name=display_name or DEFAULT_DISPLAY_NAME,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info("provisioned user %s (sub=%s)", user.id, keycloak_sub)
    return user


# ------------------------------------------------------------- updates


async def update_profile(db: AsyncSession, user_id: UUID, *, display_name: str | None) -> User:
    """Apply profile edits (None fields are left untouched)."""
    user = await get_user_or_404(db, user_id)
    if display_name is not None:
        user.display_name = display_name
    await db.commit()
    await db.refresh(user)
    return user


async def set_avatar(db: AsyncSession, user_id: UUID, avatar_uri: str) -> User:
    """Attach the MinIO avatar uri ("bucket/key") to a profile."""
    user = await get_user_or_404(db, user_id)
    user.avatar_uri = avatar_uri
    await db.commit()
    await db.refresh(user)
    return user


async def search_users(db: AsyncSession, query: str, limit: int = 10) -> list[User]:
    """Find users by display name or email substring (case-insensitive).

    Used by the DM to link a campaign member to an existing account. Email
    may be used to find the row but is never exposed in search results
    (UserOut hides it outside /api/users/me).
    """
    q = f"%{query.strip()}%"
    result = await db.execute(
        select(User)
        .where(or_(User.display_name.ilike(q), User.email.ilike(q)))
        .order_by(User.display_name)
        .limit(limit)
    )
    return list(result.scalars().all())


async def resolve_users(db: AsyncSession, user_ids: list[UUID]) -> dict[UUID, User]:
    """Fetch the existing users among ``user_ids`` (missing ids are skipped).

    Identifiers may be either the stable users.id (the historical
    cross-service user_id) or the current Keycloak subject
    (users.keycloak_sub) - a re-registered account keeps its id while its sub
    moves, so both values must resolve to the same row. The result is keyed by
    the *requested* identifier so callers can look up the id they stored.

    Used by the internal endpoint so other services can render display names
    for foreign keys (wiki.created_by, notifications.user_id, ...).
    """
    if not user_ids:
        return {}
    subs = {str(uid) for uid in user_ids}
    result = await db.execute(
        select(User).where(or_(User.id.in_(user_ids), User.keycloak_sub.in_(subs)))
    )
    rows = list(result.scalars().all())
    by_id = {user.id: user for user in rows}
    by_sub: dict[UUID, User] = {}
    for user in rows:
        try:
            by_sub[UUID(user.keycloak_sub)] = user
        except ValueError:  # non-UUID subjects can only match by id
            pass
    resolved: dict[UUID, User] = {}
    for uid in user_ids:
        user = by_id.get(uid) or by_sub.get(uid)
        if user is not None:
            resolved[uid] = user
    return resolved
