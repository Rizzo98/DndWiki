"""Internal service API (service-to-service, dnd-services client token).

Other services never read dnd_users tables directly; they ask user-service
for profile summaries (display names for foreign keys) and for voiceprint
enrollment status per campaign.
"""

from __future__ import annotations

from uuid import UUID

from dnd_common.db import get_session
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.deps import require_service
from app.schemas import InternalUserOut, InternalUsersOut, InternalVoiceProfileOut

router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(require_service)],
)


@router.get("/users", response_model=InternalUsersOut)
async def get_users(
    ids: list[UUID] = Query(...),
    db: AsyncSession = Depends(get_session),
):
    """Profile summaries for the given user ids (missing ids are skipped)."""
    users = await services.resolve_users(db, ids)
    return InternalUsersOut(
        users={
            str(uid): InternalUserOut(
                display_name=user.display_name,
                email=user.email,
                avatar_uri=user.avatar_uri,
            )
            for uid, user in users.items()
        }
    )


@router.get("/voice-profiles", response_model=list[InternalVoiceProfileOut])
async def get_voice_profiles(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_session),
):
    """Enrollment status for a campaign (which users have a voiceprint)."""
    return [
        InternalVoiceProfileOut(
            user_id=p.user_id,
            embedding_version=p.embedding_version,
            sample_uri=p.sample_uri,
            created_at=p.created_at,
        )
        for p in await services.list_profiles_for_campaign(db, campaign_id)
    ]
