"""Session business logic: create, list, upload, transition, artifacts, speakers.

The service layer owns the state machine and the session.recorded /
speakers.assigned events; routers only translate HTTP <-> service calls.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from uuid import UUID

from dnd_common.events import Event
from fastapi import HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.broker import EventPublisher
from app.core.config import ServiceSettings
from app.models import Session, SessionRecording, SpeakerAssignment
from app.schemas import SpeakerAssignmentIn
from app.status import SessionStatus, can_transition
from app.storage import ObjectStorage

logger = logging.getLogger(__name__)

RECORDINGS_BUCKET = "recordings"

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

_CHUNK = 1024 * 1024  # 1 MiB read/upload chunks


async def get_session(db: AsyncSession, session_id: UUID) -> Session | None:
    return await db.get(Session, session_id)


async def get_session_or_404(db: AsyncSession, session_id: UUID) -> Session:
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return session


async def create_session(
    db: AsyncSession,
    campaign_id: UUID,
    title: str | None,
    session_no: int | None,
) -> Session:
    """Persist a new session in status "uploaded"."""
    session = Session(campaign_id=campaign_id, title=title, session_no=session_no)
    db.add(session)
    await db.commit()
    await db.refresh(session)
    logger.info("session %s created for campaign %s", session.id, campaign_id)
    return session


async def list_sessions(db: AsyncSession, campaign_id: UUID) -> list[Session]:
    """All sessions of a campaign, newest first."""
    result = await db.execute(
        select(Session)
        .where(Session.campaign_id == campaign_id)
        .order_by(Session.recorded_at.desc().nulls_last(), Session.created_at.desc())
    )
    return list(result.scalars().all())


async def update_session_meta(
    db: AsyncSession, session_id: UUID, title: str | None, session_no: int | None
) -> Session:
    """DM edits session metadata (title / session_no)."""
    session = await get_session_or_404(db, session_id)
    if title is not None:
        session.title = title
    if session_no is not None:
        session.session_no = session_no
    await db.commit()
    await db.refresh(session)
    return session


async def transition_status(
    db: AsyncSession, session_id: UUID, new_status: SessionStatus, error: str | None = None
) -> Session:
    """Move a session along the pipeline (validated against the state machine)."""
    session = await get_session_or_404(db, session_id)
    current = SessionStatus(session.status)
    if not can_transition(current, new_status):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Invalid transition: {current.value} -> {new_status.value}",
        )
    session.status = new_status.value
    session.error = error
    await db.commit()
    await db.refresh(session)
    logger.info("session %s: %s -> %s", session_id, current.value, new_status.value)
    return session


async def update_artifacts(
    db: AsyncSession,
    session_id: UUID,
    transcript_uri: str | None = None,
    diarization_uri: str | None = None,
    duration_sec: float | None = None,
) -> Session:
    """Attach worker-produced artifacts (transcripts/diarization) to a session."""
    session = await get_session_or_404(db, session_id)
    if transcript_uri is not None:
        session.transcript_uri = transcript_uri
    if diarization_uri is not None:
        session.diarization_uri = diarization_uri
    if duration_sec is not None:
        session.duration_sec = duration_sec
    await db.commit()
    await db.refresh(session)
    return session


async def upload_recording(
    db: AsyncSession,
    session_id: UUID,
    uploaded_by: UUID,
    upload: UploadFile,
    storage: ObjectStorage,
    publisher: EventPublisher,
    settings: ServiceSettings,
    duration_sec: float | None = None,
) -> Session:
    """Validate, hash, stream to MinIO, mark recorded and emit session.recorded.

    The multipart body is spooled to disk by FastAPI; we read it once to
    compute SHA-256 + size (enforcing MAX_UPLOAD_MB), then seek back and
    stream it to "recordings/<session_id>/raw.<ext>".
    """
    session = await get_session_or_404(db, session_id)
    # Re-upload is allowed: it replaces the recording, wipes the previous
    # transcript/diarization artifacts and restarts the pipeline.

    content_type = (upload.content_type or "").lower()
    if content_type not in settings.allowed_audio_mimes:
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

    max_bytes = settings.max_upload_mb * 1024 * 1024
    sha = hashlib.sha256()
    size = 0
    file = upload.file
    while chunk := file.read(_CHUNK):
        sha.update(chunk)
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"Recording exceeds {settings.max_upload_mb} MB limit",
            )
    if size == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty recording")

    key = f"recordings/{session_id}/raw.{ext}"
    file.seek(0)
    await storage.put_object_stream(RECORDINGS_BUCKET, key, file, content_type)
    logger.info("uploaded %s (%s bytes, sha256=%s)", key, size, sha.hexdigest())

    # A re-upload supersedes the previous recording: drop the transcript and
    # diarization (the pipeline restarts from scratch) and best-effort-delete
    # the now-orphaned objects from MinIO.
    old_audio_uri = session.raw_audio_uri
    old_transcript_uri = session.transcript_uri
    old_diarization_uri = session.diarization_uri
    if old_transcript_uri:
        await storage.delete_object(settings.minio_transcripts_bucket, old_transcript_uri)
    if old_diarization_uri:
        await storage.delete_object(settings.minio_transcripts_bucket, old_diarization_uri)
    if old_audio_uri and old_audio_uri != key:
        await storage.delete_object(RECORDINGS_BUCKET, old_audio_uri)

    db.add(
        SessionRecording(
            session_id=session_id,
            uploaded_by=uploaded_by,
            file_uri=key,
            size_bytes=size,
            mime=content_type,
            sha256=sha.hexdigest(),
        )
    )
    session.status = SessionStatus.RECORDED.value
    session.raw_audio_uri = key
    session.recorded_at = datetime.now(UTC)
    session.transcript_uri = None
    session.diarization_uri = None
    session.error = None
    if duration_sec is not None:
        session.duration_sec = duration_sec
    await db.commit()
    await db.refresh(session)

    await publisher.publish(
        Event(
            type="session.recorded",
            payload={
                "session_id": str(session.id),
                "campaign_id": str(session.campaign_id),
                "recorded_by": str(uploaded_by),
                "audio_uri": key,
                "duration_sec": float(session.duration_sec) if session.duration_sec is not None else None,
            },
        )
    )
    return session


async def list_assignments(db: AsyncSession, session_id: UUID) -> list[SpeakerAssignment]:
    await get_session_or_404(db, session_id)
    result = await db.execute(
        select(SpeakerAssignment)
        .where(SpeakerAssignment.session_id == session_id)
        .order_by(SpeakerAssignment.speaker_label)
    )
    return list(result.scalars().all())


async def upsert_assignments(
    db: AsyncSession, session_id: UUID, items: list[SpeakerAssignmentIn]
) -> list[SpeakerAssignment]:
    """Upsert diarized-label -> user mappings reported by speaker-service."""
    await get_session_or_404(db, session_id)
    for item in items:
        existing = await db.scalar(
            select(SpeakerAssignment).where(
                SpeakerAssignment.session_id == session_id,
                SpeakerAssignment.speaker_label == item.speaker_label,
            )
        )
        if existing is None:
            db.add(
                SpeakerAssignment(
                    session_id=session_id,
                    speaker_label=item.speaker_label,
                    user_id=item.user_id,
                    confidence=item.confidence,
                    status=item.status,
                )
            )
        else:
            if item.user_id is not None:
                existing.user_id = item.user_id
            if item.confidence is not None:
                existing.confidence = item.confidence
            existing.status = item.status
    await db.commit()
    return await list_assignments(db, session_id)


async def assign_speaker(
    db: AsyncSession,
    session_id: UUID,
    speaker_label: str,
    *,
    member_id: UUID,
    user_id: UUID | None,
    display_name: str | None,
    character_name: str | None,
    assigned_by: UUID,
    publisher: EventPublisher,
    enrolled_voiceprint: bool = False,
) -> SpeakerAssignment:
    """DM names a previously-unknown speaker; emits speakers.assigned.

    The speaker is identified by the campaign member (member_id), which
    may or may not have a linked user account; user_id is mirrored when
    the member is user-linked so voiceprint enrollment/matching still
    works. display_name (the member player name) lets content generation
    name userless speakers; character_name lets it label them by their
    CHARACTER in the wiki.
    """
    session = await get_session_or_404(db, session_id)
    assignment = await db.scalar(
        select(SpeakerAssignment).where(
            SpeakerAssignment.session_id == session_id,
            SpeakerAssignment.speaker_label == speaker_label,
        )
    )
    if assignment is None:
        assignment = SpeakerAssignment(
            session_id=session_id,
            speaker_label=speaker_label,
            member_id=member_id,
            user_id=user_id,
            status="confirmed",
            assigned_by=assigned_by,
        )
        db.add(assignment)
    else:
        assignment.member_id = member_id
        assignment.user_id = user_id
        assignment.status = "confirmed"
        assignment.assigned_by = assigned_by
    await db.commit()
    await db.refresh(assignment)

    # Naming the last pending speaker of a session closes the identification
    # stage (speaker_pending -> speakers_identified), so the speakers.assigned
    # event can drive content generation.
    if session.status == SessionStatus.SPEAKER_PENDING.value:
        remaining_pending = await db.scalar(
            select(func.count())
            .select_from(SpeakerAssignment)
            .where(
                SpeakerAssignment.session_id == session_id,
                SpeakerAssignment.status == "pending",
            )
        )
        if remaining_pending == 0:
            session.status = SessionStatus.SPEAKERS_IDENTIFIED.value
            await db.commit()

    await publisher.publish(
        Event(
            type="speakers.assigned",
            payload={
                "session_id": str(session.id),
                "campaign_id": str(session.campaign_id),
                "label": speaker_label,
                "member_id": str(member_id),
                "user_id": str(user_id) if user_id else None,
                "display_name": display_name,
                # the member's CHARACTER name - generation labels party
                # speakers by their character, never the player name
                "character_name": character_name,
                "assigned_by": str(assigned_by),
                "enrolled_voiceprint": enrolled_voiceprint,
                # lets speaker-service slice this speaker's voice out of the
                # session recording and enroll a session-derived voiceprint
                # (only when user_id is present — userless members have no
                # user-keyed voiceprint to enroll)
                "audio_uri": session.raw_audio_uri,
            },
        )
    )
    return assignment
