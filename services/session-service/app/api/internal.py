"""Internal worker API (service-to-service, dnd-services client token).

transcription/speaker/content workers never touch session-service tables
directly; they update pipeline state through these endpoints. Read endpoints
back the debug regenerate flow (content-service needs the session plus its
speaker map to re-publish content.generate).
"""

from __future__ import annotations

import logging
from uuid import UUID

from dnd_common.db import get_session
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.clients.campaigns import CampaignServiceClient
from app.deps import get_campaign_client, require_service
from app.schemas import (
    ArtifactsUpdate,
    InternalSpeakerOut,
    SessionOut,
    SpeakerAssignmentIn,
    SpeakerAssignmentOut,
    StatusUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal/sessions",
    tags=["internal"],
    dependencies=[Depends(require_service)],
)


@router.get("/{session_id}", response_model=SessionOut)
async def get_session_record(
    session_id: UUID,
    db: AsyncSession = Depends(get_session),
):
    """Session record (no presigned URLs) for service-to-service consumers."""
    return await services.get_session_or_404(db, session_id)


@router.patch("/{session_id}/status", response_model=SessionOut)
async def update_status(session_id: UUID, body: StatusUpdate, db: AsyncSession = Depends(get_session)):
    """Move a session along the pipeline (validated state machine)."""
    return await services.transition_status(db, session_id, body.status, body.error)


@router.patch("/{session_id}/artifacts", response_model=SessionOut)
async def update_artifacts(
    session_id: UUID, body: ArtifactsUpdate, db: AsyncSession = Depends(get_session)
):
    """Attach transcript/diarization artifacts produced by transcription-service."""
    return await services.update_artifacts(
        db, session_id, body.transcript_uri, body.diarization_uri, body.duration_sec
    )


@router.post("/{session_id}/speakers", response_model=list[SpeakerAssignmentOut])
async def upsert_speakers(
    session_id: UUID, body: list[SpeakerAssignmentIn], db: AsyncSession = Depends(get_session)
):
    """Write diarized-label -> user mappings reported by speaker-service."""
    return await services.upsert_assignments(db, session_id, body)


@router.get("/{session_id}/speakers", response_model=list[InternalSpeakerOut])
async def list_speakers(
    session_id: UUID,
    db: AsyncSession = Depends(get_session),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
):
    """Speaker map for a session (label -> user / display / character names).

    display_name comes from the campaign member's player_name when the
    assignment has no linked user account — the same name the
    speakers.assigned event carried the first time, so a regenerate re-run
    keeps speaker names stable. character_name is resolved for every
    assigned member so generation can use CHARACTER names for players.
    """
    session = await services.get_session_or_404(db, session_id)
    out: list[InternalSpeakerOut] = []
    for row in await services.list_assignments(db, session_id):
        display_name: str | None = None
        character_name: str | None = None
        if row.member_id:
            try:
                member = await campaign_client.get_member(session.campaign_id, row.member_id)
                character_name = member.get("character_name") or None
                if not row.user_id:
                    display_name = member.get("player_name")
            except Exception:  # noqa: BLE001 - naming is best-effort for a re-run
                logger.debug("could not resolve member %s names", row.member_id)
        out.append(
            InternalSpeakerOut(
                label=row.speaker_label,
                user_id=row.user_id,
                display_name=display_name,
                character_name=character_name,
                status=row.status,
            )
        )
    return out
