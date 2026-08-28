"""Voiceprint enrollment business logic.

Enrollment pipeline (DM triggers the flow, the member records in the app):

1. validate the clip (mime + size), stream it to MinIO ``voice-samples``;
2. extract a 192-d ECAPA-TDNN embedding (SpeechBrain, lazy-loaded);
3. upsert the vector into Qdrant ``voiceprints`` with payload
   ``{user_id, campaign_id, sample_uri, version}``;
4. upsert the ``voice_profiles`` row (one per (user, campaign)).

Membership/role authorization happens in the router via campaign-service;
this layer only owns the enrollment invariants.
"""

from __future__ import annotations

import io
import logging
import uuid
from uuid import UUID

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import ServiceSettings
from app.embedding import VoiceEmbedder
from app.models import User, VoiceProfile
from app.qdrant import VoiceprintStore
from app.storage import ObjectStorage

logger = logging.getLogger(__name__)

# Same mapping as session-service (phone recordings).
EXT_BY_MIME: dict[str, str] = {
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/m4a": "m4a",
    "audio/aac": "aac",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
}

AVATAR_EXT_BY_MIME: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def split_uri(uri: str) -> tuple[str, str]:
    """Split a stored "bucket/key" MinIO uri into (bucket, key)."""
    bucket, _, key = uri.partition("/")
    return bucket, key


# ------------------------------------------------------------- getters


async def get_profile(db: AsyncSession, profile_id: UUID) -> VoiceProfile | None:
    return await db.get(VoiceProfile, profile_id)


async def list_profiles(
    db: AsyncSession, user_id: UUID, campaign_id: UUID | None = None
) -> list[VoiceProfile]:
    """The caller's own voiceprints (optionally scoped to a campaign)."""
    stmt = select(VoiceProfile).where(VoiceProfile.user_id == user_id)
    if campaign_id is not None:
        stmt = stmt.where(VoiceProfile.campaign_id == campaign_id)
    stmt = stmt.order_by(VoiceProfile.created_at.desc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def list_profiles_for_campaign(
    db: AsyncSession, campaign_id: UUID
) -> list[VoiceProfile]:
    """Every enrolled voiceprint in a campaign (internal, for the DM console)."""
    result = await db.execute(
        select(VoiceProfile)
        .where(VoiceProfile.campaign_id == campaign_id)
        .order_by(VoiceProfile.created_at)
    )
    return list(result.scalars().all())


# ------------------------------------------------------------- enroll


async def enroll(
    db: AsyncSession,
    *,
    user: User,
    campaign_id: UUID,
    upload: UploadFile,
    storage: ObjectStorage,
    embedder: VoiceEmbedder,
    voiceprints: VoiceprintStore,
    settings: ServiceSettings,
) -> VoiceProfile:
    """Validate, store, embed and upsert a voiceprint for (user, campaign).

    Re-enrollment replaces the existing profile for the pair (unique
    constraint): the new embedding is upserted first, then the old Qdrant
    point is removed and the row updated in place.
    """
    content_type = (upload.content_type or "").lower()
    if content_type not in settings.allowed_voice_mimes:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported media type: {content_type or 'unknown'}",
        )
    ext = EXT_BY_MIME.get(content_type)
    if ext is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"No extension mapping for {content_type}",
        )

    max_bytes = settings.max_voice_sample_mb * 1024 * 1024
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty audio clip")
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Voice sample exceeds {settings.max_voice_sample_mb} MB limit",
        )

    # Embed first (duration validation) so a rejected clip never lands in MinIO.
    # EmbeddingUnavailable (missing speechbrain) propagates to the router -> 503.
    embedding, duration = await embedder.embed_bytes(data, suffix=f".{ext}")
    if duration < settings.voice_sample_min_sec or duration > settings.voice_sample_max_sec:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Voice sample must be between {settings.voice_sample_min_sec:g} and "
                f"{settings.voice_sample_max_sec:g} seconds (got {duration:.1f}s)"
            ),
        )

    key = f"{user.id}/{campaign_id}/{uuid.uuid4()}.{ext}"
    sample_uri = f"{settings.minio_voice_samples_bucket}/{key}"
    await storage.put_object_stream(
        settings.minio_voice_samples_bucket, key, io.BytesIO(data), content_type
    )
    logger.info("stored voice sample %s (%s bytes)", sample_uri, len(data))

    point_id = str(uuid.uuid4())
    await voiceprints.upsert(
        point_id,
        embedding,
        payload={
            "user_id": str(user.id),
            "campaign_id": str(campaign_id),
            "sample_uri": sample_uri,
            "version": settings.embedding_version,
        },
    )

    existing = await db.scalar(
        select(VoiceProfile).where(
            VoiceProfile.user_id == user.id, VoiceProfile.campaign_id == campaign_id
        )
    )
    if existing is not None:
        if existing.qdrant_point != point_id:
            await voiceprints.delete(existing.qdrant_point)
        existing.qdrant_point = point_id
        existing.sample_uri = sample_uri
        existing.embedding_version = settings.embedding_version
        profile = existing
    else:
        profile = VoiceProfile(
            user_id=user.id,
            campaign_id=campaign_id,
            qdrant_point=point_id,
            sample_uri=sample_uri,
            embedding_version=settings.embedding_version,
        )
        db.add(profile)

    await db.commit()
    await db.refresh(profile)
    logger.info(
        "voiceprint enrolled for user %s in campaign %s (point %s)",
        user.id,
        campaign_id,
        point_id,
    )
    return profile


# ------------------------------------------------------------- delete


async def delete_profile(
    db: AsyncSession,
    profile: VoiceProfile,
    voiceprints: VoiceprintStore,
) -> None:
    """Remove a voiceprint: the Qdrant point (best effort) and the row."""
    try:
        await voiceprints.delete(profile.qdrant_point)
    except Exception:  # cleanup must not block profile removal
        logger.warning(
            "could not delete Qdrant point %s (continuing)", profile.qdrant_point, exc_info=True
        )
    await db.delete(profile)
    await db.commit()
    logger.info("voiceprint profile %s deleted", profile.id)
