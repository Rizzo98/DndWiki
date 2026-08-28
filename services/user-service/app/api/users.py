"""Public user API (behind the gateway, JWT-authenticated users).

- GET/PATCH /api/users/me  - own profile (lazy-provisioned from JWT claims)
- PUT /api/users/me/avatar - avatar upload (multipart -> MinIO)
- GET /api/users/{id}      - public profile for roster display (no email)

Identity across the platform is the Keycloak subject (users.keycloak_sub);
email is mirrored read-only (Keycloak is the source of truth).
"""

from __future__ import annotations

import io
from typing import Any
from uuid import UUID

from dnd_common.auth import current_user
from dnd_common.db import get_session
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.core.config import ServiceSettings, get_settings
from app.deps import get_storage
from app.models import User
from app.schemas import ProfileUpdate, UserOut
from app.services.voice import AVATAR_EXT_BY_MIME
from app.storage import ObjectStorage

router = APIRouter(prefix="/api/users", tags=["users"])


def _user_id(user: dict[str, Any]) -> UUID:
    """Identity used across services is the Keycloak subject (UUID by default)."""
    return UUID(user["sub"])


async def _me(db: AsyncSession, claims: dict[str, Any]) -> User:
    """Resolve (provisioning on first access) the caller's user row."""
    return await services.get_or_create_user(
        db,
        keycloak_sub=str(claims["sub"]),
        email=claims.get("email"),
        display_name=services.display_name_from_claims(claims),
    )


def _out(user: User, *, with_email: bool = False) -> UserOut:
    """UserOut; email is private (only on /me), avatar_url is filled by callers."""
    out = UserOut.model_validate(user)
    if not with_email:
        out.email = None
    return out


@router.get("/me", response_model=UserOut)
async def me(
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    storage: ObjectStorage = Depends(get_storage),
):
    """Own profile (creates the user row on first access)."""
    profile = await _me(db, user)
    out = _out(profile, with_email=True)
    if profile.avatar_uri:
        bucket, key = services.split_uri(profile.avatar_uri)
        out.avatar_url = await storage.presigned_get(bucket, key)
    return out


@router.patch("/me", response_model=UserOut)
async def update_me(
    body: ProfileUpdate,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    storage: ObjectStorage = Depends(get_storage),
):
    """Edit own profile (v1: display_name only; email is Keycloak-managed)."""
    profile = await services.update_profile(
        db, (await _me(db, user)).id, display_name=body.display_name
    )
    out = _out(profile, with_email=True)
    if profile.avatar_uri:
        bucket, key = services.split_uri(profile.avatar_uri)
        out.avatar_url = await storage.presigned_get(bucket, key)
    return out


@router.put("/me/avatar", response_model=UserOut)
async def upload_avatar(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    storage: ObjectStorage = Depends(get_storage),
    settings: ServiceSettings = Depends(get_settings),
):
    """Set the profile avatar (multipart image; stored under wiki-assets/avatars/)."""
    profile = await _me(db, user)

    content_type = (file.content_type or "").lower()
    ext = AVATAR_EXT_BY_MIME.get(content_type)
    if ext is None or content_type not in settings.allowed_avatar_mimes:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported avatar type: {content_type or 'unknown'}",
        )
    data = await file.read()
    max_bytes = settings.max_avatar_mb * 1024 * 1024
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty avatar file")
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Avatar exceeds {settings.max_avatar_mb} MB limit",
        )

    key = f"avatars/{profile.id}.{ext}"
    uri = f"{settings.minio_avatars_bucket}/{key}"
    await storage.put_object_stream(
        settings.minio_avatars_bucket, key, io.BytesIO(data), content_type
    )
    profile = await services.set_avatar(db, profile.id, uri)

    out = _out(profile, with_email=True)
    out.avatar_url = await storage.presigned_get(settings.minio_avatars_bucket, key)
    return out


# NOTE: /search must stay defined before /{user_id} — a bare "search" path
# would otherwise be captured by the {user_id: UUID} converter.
@router.get("/search", response_model=list[UserOut])
async def search_users(
    q: str = Query(min_length=1, max_length=64),
    limit: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    storage: ObjectStorage = Depends(get_storage),
):
    """Find users by display name or email (email stays private in results).

    Used by the DM to link a campaign member to an existing platform user.
    """
    profiles = await services.search_users(db, q, limit=limit)
    out = []
    for profile in profiles:
        item = _out(profile, with_email=False)
        if profile.avatar_uri:
            bucket, key = services.split_uri(profile.avatar_uri)
            item.avatar_url = await storage.presigned_get(bucket, key)
        out.append(item)
    return out


@router.get("/{user_id}", response_model=UserOut)
async def get_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    storage: ObjectStorage = Depends(get_storage),
):
    """Public profile by id (no email); used by rosters and page attributions."""
    profile = await services.get_user_or_404(db, user_id)
    out = _out(profile, with_email=False)
    if profile.avatar_uri:
        bucket, key = services.split_uri(profile.avatar_uri)
        out.avatar_url = await storage.presigned_get(bucket, key)
    return out
