"""Public voiceprint API (behind the gateway, JWT-authenticated users).

Voiceprints are self-service: a member enrolls their own voice in a campaign
they belong to (membership is asserted against campaign-service). The DM
console triggers the flow, but the request carries the member's JWT.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from dnd_common.auth import current_user
from dnd_common.db import get_session
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.clients.campaigns import CampaignServiceClient, MembershipUnavailable
from app.core.config import ServiceSettings, get_settings
from app.deps import get_campaign_client, get_embedder, get_storage, get_voiceprint_store
from app.embedding import EmbeddingUnavailable, VoiceEmbedder
from app.models import VoiceProfile
from app.qdrant import VoiceprintStore
from app.schemas import VoiceProfileOut
from app.storage import ObjectStorage

router = APIRouter(prefix="/api/voice", tags=["voice"])


def _user_id(user: dict[str, Any]) -> UUID:
    """Identity used across services is the Keycloak subject (UUID by default)."""
    return UUID(user["sub"])


async def _member_or_403(
    campaign_client: CampaignServiceClient, campaign_id: UUID, user_id: UUID
) -> None:
    """Require campaign membership (403) or surface campaign-service outage (503)."""
    try:
        await campaign_client.assert_member(campaign_id, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except MembershipUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="campaign-service unavailable"
        ) from exc


async def _out(
    profile: VoiceProfile,
    storage: ObjectStorage,
    settings: ServiceSettings,
) -> VoiceProfileOut:
    """VoiceProfileOut with a short-lived presigned sample URL."""
    bucket, key = services.split_uri(profile.sample_uri)
    return VoiceProfileOut.model_validate(profile).model_copy(
        update={"sample_url": await storage.presigned_get(bucket, key)}
    )


@router.post("/enroll", response_model=VoiceProfileOut, status_code=status.HTTP_201_CREATED)
async def enroll(
    campaign_id: UUID = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
    storage: ObjectStorage = Depends(get_storage),
    embedder: VoiceEmbedder = Depends(get_embedder),
    voiceprints: VoiceprintStore = Depends(get_voiceprint_store),
    settings: ServiceSettings = Depends(get_settings),
):
    """Enroll the caller's voiceprint in a campaign (10-30 s clip recommended).

    The clip is stored in MinIO voice-samples, embedded (ECAPA-TDNN) and
    upserted into Qdrant 'voiceprints'; the voice_profiles row is upserted
    per (user, campaign).
    """
    user_id = _user_id(user)
    await _member_or_403(campaign_client, campaign_id, user_id)
    profile_row = await services.get_or_create_user(
        db,
        keycloak_sub=str(user["sub"]),
        email=user.get("email"),
        display_name=services.display_name_from_claims(user),
    )
    try:
        profile = await services.enroll(
            db,
            user=profile_row,
            campaign_id=campaign_id,
            upload=file,
            storage=storage,
            embedder=embedder,
            voiceprints=voiceprints,
            settings=settings,
        )
    except EmbeddingUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return await _out(profile, storage, settings)


@router.get("/profiles", response_model=list[VoiceProfileOut])
async def list_voice_profiles(
    campaign_id: UUID | None = None,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    storage: ObjectStorage = Depends(get_storage),
    settings: ServiceSettings = Depends(get_settings),
):
    """The caller's own voiceprints (optionally filtered by campaign).

    Profiles are keyed by the stable users.id (the Keycloak subject at
    registration). After a re-registration the JWT sub moved while the row id
    stayed, so resolve the current sub to the row id before listing -
    otherwise "my voiceprints" comes back empty for re-registered accounts.
    """
    user_row = await services.get_user_by_sub(db, str(user["sub"]))
    if user_row is None:
        return []
    profiles = await services.list_profiles(db, user_row.id, campaign_id)
    return [await _out(p, storage, settings) for p in profiles]


@router.delete("/profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_voice_profile(
    profile_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    voiceprints: VoiceprintStore = Depends(get_voiceprint_store),
):
    """Remove one of the caller's voiceprints (also deletes the Qdrant point).

    Scoped to the caller via the resolved users row id (the JWT sub may have
    moved after a re-registration); someone else's profile is 404.
    """
    user_row = await services.get_user_by_sub(db, str(user["sub"]))
    if user_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Voice profile not found")
    profile = await services.get_profile(db, profile_id)
    if profile is None or profile.user_id != user_row.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Voice profile not found")
    await services.delete_profile(db, profile, voiceprints)
