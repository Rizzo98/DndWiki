"""Internal worker API (service-to-service, dnd-services client token).

transcription/speaker/content workers never touch session-service tables
directly; they update pipeline state through these endpoints. The read
endpoints expose the session record and the speaker map to the other services.
"""

from __future__ import annotations

import logging
from uuid import UUID

from dnd_common.db import get_session
from fastapi import APIRouter, Depends, Query
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
    SpeakerHistoryEntryOut,
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


@router.get("/{session_id}/speaker-assignments")
async def get_speaker_assignments(
    session_id: UUID, db: AsyncSession = Depends(get_session)
):
    """The derived compatibility view (a projection of the engine's belief).

    During the transition the legacy speaker panel and the old content path read
    this instead of the table directly, so they cannot disagree with the engine.
    """
    return [
        {
            "speaker_label": row.speaker_label,
            "member_id": str(row.member_id) if row.member_id else None,
            "user_id": str(row.user_id) if row.user_id else None,
            "confidence": float(row.confidence) if row.confidence is not None else None,
            "status": row.status,
        }
        for row in await services.list_assignments(db, session_id)
    ]


@router.put("/{session_id}/speaker-assignments")
async def put_speaker_assignments(
    session_id: UUID, body: list[dict], db: AsyncSession = Depends(get_session)
):
    """Write the engine's projection of the belief into the compatibility view."""
    rows = await services.project_assignments(db, session_id, body)
    return {"session_id": str(session_id), "rows": len(rows)}


campaigns_router = APIRouter(
    prefix="/internal/campaigns",
    tags=["internal"],
    dependencies=[Depends(require_service)],
)


@campaigns_router.get("/{campaign_id}/speaker-history", response_model=list[SpeakerHistoryEntryOut])
async def campaign_speaker_history(
    campaign_id: UUID,
    exclude_session_id: UUID | None = None,
    limit_sessions: int = Query(default=5, ge=1, le=50),
    # Defaults preserve what the ENROLLMENT path has always seen: confirmed,
    # user-linked labels only. Calibration opts out of both explicitly.
    confirmed_only: bool = Query(default=True),
    include_member_keyed: bool = Query(default=False),
    db: AsyncSession = Depends(get_session),
):
    """DM-confirmed speaker labels of a campaign's previous sessions.

    speaker-service reads this while identifying a session: the DM already
    named those speakers by hand, so the labelled audio of their turns becomes
    a voice sample for the campaign (only high-confidence, long-enough turns —
    the filtering itself lives in speaker-service). Newest sessions first;
    'exclude_session_id' keeps the session being identified out of its own
    reference set, and 'limit_sessions' bounds how far back a run looks.

    `confirmed_only` and `include_member_keyed` exist for CALIBRATION, which
    wants a different slice than enrollment does: the engine fits its
    score-to-LLR curve from every labelled turn of the campaign, including
    labels attached to a member who has no linked user account, and it does not
    want to be capped at the five newest sessions. Widening the defaults would
    have changed what the enrollment path sees, so the new behaviour is opt-in
    (docs/attribution-plan.md S3).
    """
    return await services.campaign_speaker_history(
        db,
        campaign_id,
        exclude_session_id=exclude_session_id,
        limit_sessions=limit_sessions,
        confirmed_only=confirmed_only,
        include_member_keyed=include_member_keyed,
    )
