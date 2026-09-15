"""Public session API (behind the gateway, JWT-authenticated users)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from dnd_common.auth import current_user
from dnd_common.db import get_session
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.broker import EventPublisher
from app.clients.campaigns import CampaignServiceClient, MembershipUnavailable
from app.clients.content import ContentServiceClient
from app.core.config import ServiceSettings, get_settings
from app.deps import get_campaign_client, get_content_client, get_publisher, get_storage
from app.models import SpeakerAssignment
from app.schemas import (
    SessionCreate,
    SessionDeleteOut,
    SessionDetail,
    SessionOut,
    SessionUpdate,
    SpeakerAssignmentOut,
    SpeakerAssignRequest,
)
from app.storage import ObjectStorage

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _user_id(user: dict[str, Any]) -> UUID:
    """Identity used across services is the Keycloak subject (UUID by default)."""
    return UUID(user["sub"])


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


async def _member_or_404(
    campaign_client: CampaignServiceClient, campaign_id: UUID, member_id: UUID
) -> dict[str, Any]:
    """Resolve a campaign member by id; 404 when it is not in the campaign."""
    try:
        return await campaign_client.get_member(campaign_id, member_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except MembershipUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="campaign-service unavailable"
        ) from exc


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: SessionCreate,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
):
    """Create a session for a campaign (the caller must be a member)."""
    user_id = _user_id(user)
    await _member_or_403(campaign_client, body.campaign_id, user_id)
    return await services.create_session(db, body.campaign_id, body.title, body.session_no)


@router.get("", response_model=list[SessionOut])
async def list_sessions(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
):
    """List sessions of a campaign (member-only)."""
    user_id = _user_id(user)
    await _member_or_403(campaign_client, campaign_id, user_id)
    return await services.list_sessions(db, campaign_id)


@router.get("/{session_id}", response_model=SessionDetail)
async def session_detail(
    session_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
    storage: ObjectStorage = Depends(get_storage),
    settings: ServiceSettings = Depends(get_settings),
):
    """Session detail plus short-lived presigned media URLs (when available)."""
    session = await services.get_session_or_404(db, session_id)
    user_id = _user_id(user)
    await _member_or_403(campaign_client, session.campaign_id, user_id)

    raw_audio_url = transcript_url = diarization_url = None
    if session.raw_audio_uri:
        raw_audio_url = await storage.presigned_get("recordings", session.raw_audio_uri)
    if session.transcript_uri:
        transcript_url = await storage.presigned_get(
            settings.minio_transcripts_bucket, session.transcript_uri
        )
    if session.diarization_uri:
        diarization_url = await storage.presigned_get(
            settings.minio_transcripts_bucket, session.diarization_uri
        )

    return SessionDetail(
        **SessionOut.model_validate(session).model_dump(),
        raw_audio_url=raw_audio_url,
        transcript_url=transcript_url,
        diarization_url=diarization_url,
    )


@router.put("/{session_id}/recording", response_model=SessionDetail)
async def upload_recording(
    session_id: UUID,
    file: UploadFile = File(...),
    duration_sec: float | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
    storage: ObjectStorage = Depends(get_storage),
    publisher: EventPublisher = Depends(get_publisher),
    settings: ServiceSettings = Depends(get_settings),
):
    """Upload the raw phone recording (multipart) and kick off the pipeline."""
    session = await services.get_session_or_404(db, session_id)
    user_id = _user_id(user)
    await _member_or_403(campaign_client, session.campaign_id, user_id)

    await services.upload_recording(
        db, session_id, user_id, file, storage, publisher, settings, duration_sec
    )
    updated = await services.get_session(db, session_id)
    raw_audio_url = await storage.presigned_get("recordings", updated.raw_audio_uri)
    return SessionDetail(
        **SessionOut.model_validate(updated).model_dump(),
        raw_audio_url=raw_audio_url,
    )


@router.patch("/{session_id}", response_model=SessionOut)
async def update_session(
    session_id: UUID,
    body: SessionUpdate,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
):
    """DM edits session metadata."""
    session = await services.get_session_or_404(db, session_id)
    user_id = _user_id(user)
    await _dm_or_403(campaign_client, session.campaign_id, user_id)
    return await services.update_session_meta(db, session_id, body.title, body.session_no)


@router.delete("/{session_id}", response_model=SessionDeleteOut)
async def delete_session(
    session_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
    content_client: ContentServiceClient = Depends(get_content_client),
    storage: ObjectStorage = Depends(get_storage),
    settings: ServiceSettings = Depends(get_settings),
):
    """Delete a session (DM only).

    Only possible while the session has not generated its wiki updates yet:
    once the pages and timeline entries exist, deleting the session would leave
    them behind pointing at a session that is gone (409). The recording, the
    generated rows (summary, jobs, proposed changes) and the session itself
    with its uploads and speaker assignments are removed.
    """
    session = await services.get_session_or_404(db, session_id)
    user_id = _user_id(user)
    await _dm_or_403(campaign_client, session.campaign_id, user_id)
    return await services.delete_session(
        db,
        session_id,
        content_client=content_client,
        storage=storage,
        settings=settings,
    )


@router.get("/{session_id}/speakers", response_model=list[SpeakerAssignmentOut])
async def list_speakers(
    session_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
):
    """Speaker assignments for a session (member-only)."""
    session = await services.get_session_or_404(db, session_id)
    user_id = _user_id(user)
    await _member_or_403(campaign_client, session.campaign_id, user_id)
    return await services.list_assignments(db, session_id)


@router.post(
    "/{session_id}/speakers/{speaker_label}/assign",
    response_model=SpeakerAssignmentOut,
)
async def assign_speaker(
    session_id: UUID,
    speaker_label: str,
    body: SpeakerAssignRequest,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
    publisher: EventPublisher = Depends(get_publisher),
):
    """DM names a previously-unknown speaker by member id (emits speakers.assigned)."""
    session = await services.get_session_or_404(db, session_id)
    user_id = _user_id(user)
    await _dm_or_403(campaign_client, session.campaign_id, user_id)
    member = await _member_or_404(campaign_client, session.campaign_id, body.member_id)
    return await services.assign_speaker(
        db,
        session_id,
        speaker_label,
        member_id=body.member_id,
        user_id=UUID(member["user_id"]) if member.get("user_id") else None,
        display_name=member.get("player_name"),
        character_name=member.get("character_name"),
        assigned_by=user_id,
        publisher=publisher,
    )



async def _member_for_assignment(
    campaign_client: CampaignServiceClient,
    campaign_id: UUID,
    assignment: SpeakerAssignment,
) -> dict[str, Any] | None:
    """The roster member an assignment belongs to, when it can be traced.

    A DM naming resolves the member directly; an automatic match only carries
    the user id, so the roster is searched for that account. Best effort: an
    unknown member (removed from the campaign) leaves the assignment as is.
    """
    if assignment.member_id is not None:
        try:
            return await campaign_client.get_member(campaign_id, assignment.member_id)
        except ValueError:
            return None
    if assignment.user_id is not None:
        return await campaign_client.find_member_for_user(campaign_id, assignment.user_id)
    return None


@router.post(
    "/{session_id}/speakers/{speaker_label}/confirm",
    response_model=SpeakerAssignmentOut,
)
async def confirm_speaker(
    session_id: UUID,
    speaker_label: str,
    db: AsyncSession = Depends(get_session),
    user: dict[str, Any] = Depends(current_user),
    campaign_client: CampaignServiceClient = Depends(get_campaign_client),
    publisher: EventPublisher = Depends(get_publisher),
):
    """DM confirms a proposed (auto) speaker match (emits speakers.assigned).

    Confirming is what turns a match into knowledge: the assignment becomes
    'confirmed', the speaker-service learns the voice from it (voice samples
    for later sessions) and generation gets the member + character names.
    The DM can still change the name instead, with the assign endpoint.
    """
    session = await services.get_session_or_404(db, session_id)
    user_id = _user_id(user)
    await _dm_or_403(campaign_client, session.campaign_id, user_id)
    assignment = await services.get_assignment(db, session_id, speaker_label)
    if assignment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No assignment for speaker {speaker_label}",
        )
    member = await _member_for_assignment(campaign_client, session.campaign_id, assignment)
    member_id = UUID(member["id"]) if member else assignment.member_id
    return await services.confirm_assignment(
        db,
        session_id,
        speaker_label,
        member_id=member_id,
        display_name=(member or {}).get("player_name"),
        character_name=(member or {}).get("character_name"),
        assigned_by=user_id,
        publisher=publisher,
    )
