"""Session business logic: create, list, upload, transition, artifacts, speakers.

The service layer owns the state machine and the session.recorded /
speakers.assigned events; routers only translate HTTP <-> service calls.

Deleting a session is the DM's escape hatch for a run that went wrong: it is
allowed only while the session has not produced any wiki content yet, and it
purges the recording, the generated rows in content-service and the session
itself (with its uploads and speaker assignments).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from dnd_common.events import Event
from fastapi import HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.broker import EventPublisher
from app.clients.content import ContentServiceClient, ContentServiceError, SessionNotDeletable
from app.core.config import ServiceSettings
from app.models import Session, SessionRecording, SpeakerAssignment
from app.schemas import SpeakerAssignmentIn
from app.status import SessionStatus, can_delete, can_transition
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


async def delete_session(
    db: AsyncSession,
    session_id: UUID,
    *,
    content_client: ContentServiceClient,
    storage: ObjectStorage,
    settings: ServiceSettings,
) -> dict[str, Any]:
    """Delete a session the pipeline never finished writing into the wiki.

    Allowed only while the session has not generated its wiki updates yet
    (see status.can_delete): once the pages and timeline entries exist, they
    would outlive the session that produced them. The order below keeps a
    failure recoverable — the guards run before anything is destroyed, and the
    session row itself goes last, so a half-done deletion is simply retried.
    """
    session = await get_session_or_404(db, session_id)
    current = SessionStatus(session.status)
    if not can_delete(current):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"the wiki updates of this session were already generated (status "
                f"'{current.value}'); archive them in the wiki tab first"
            ),
        )

    # The generated rows live in content-service; it also re-checks that the
    # wiki does not already hold this session's content.
    try:
        await content_client.delete_session_data(str(session_id))
    except SessionNotDeletable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ContentServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    # The recording and the transcript artifacts (best effort: a storage hiccup
    # must not keep a deleted session alive in the database).
    for bucket, key in (
        (RECORDINGS_BUCKET, session.raw_audio_uri),
        (settings.minio_transcripts_bucket, session.transcript_uri),
        (settings.minio_transcripts_bucket, session.diarization_uri),
    ):
        if not key:
            continue
        try:
            await storage.delete_object(bucket, key)
        except Exception:  # noqa: BLE001 - orphaned object, never a failed delete
            logger.warning("could not delete %s/%s of session %s", bucket, key, session_id)

    title = session.title
    for recording in await _recordings(db, session_id):
        await db.delete(recording)
    for assignment in await list_assignments(db, session_id):
        await db.delete(assignment)
    await db.delete(session)
    await db.commit()
    logger.info("session %s deleted (status was %s)", session_id, current.value)
    return {"session_id": str(session_id), "title": title, "deleted": True}


async def _recordings(db: AsyncSession, session_id: UUID) -> list[SessionRecording]:
    """Every upload row of a session (one per upload attempt)."""
    result = await db.execute(
        select(SessionRecording).where(SessionRecording.session_id == session_id)
    )
    return list(result.scalars().all())


#: Speaker assignment status meaning "the DM said so" (see app/status.py consumers
#: and the session page): only these labels are a trustworthy voice sample.
CONFIRMED_STATUS = "confirmed"

#: Session statuses whose speaker map is settled enough to learn voices from:
#: identification is over (or the DM is naming the remaining labels), so a
#: 'confirmed' label is a durable manual naming. Sessions still being
#: transcribed/identified are excluded, and so are failed runs.
HISTORY_SESSION_STATUSES: tuple[str, ...] = (
    SessionStatus.SPEAKERS_IDENTIFIED.value,
    SessionStatus.SPEAKER_PENDING.value,
    SessionStatus.SUMMARIZING.value,
    SessionStatus.SUMMARY_READY.value,
    SessionStatus.GENERATING_WIKI.value,
    SessionStatus.WIKI_PLAN_READY.value,
    SessionStatus.APPLYING_WIKI.value,
    SessionStatus.CONTENT_READY.value,
    SessionStatus.REVIEWED.value,
    SessionStatus.PUBLISHED.value,
)


async def campaign_speaker_history(
    db: AsyncSession,
    campaign_id: UUID,
    *,
    exclude_session_id: UUID | None = None,
    limit_sessions: int = 5,
) -> list[dict[str, Any]]:
    """DM-confirmed, user-linked labels of a campaign's settled sessions.

    speaker-service turns these into voice samples (see its app.history): the
    DM already said who spoke, so that labelled audio is a labelled sample for
    the campaign's voiceprints — the next session is identified against it.
    Newest sessions first, so a caller that keeps only the head of the list
    gets the most recent history.

    Only 'confirmed' assignments are returned (an 'auto' or 'pending' label is
    a guess), and only user-linked ones: voiceprints are keyed by user id, so a
    userless campaign member has nothing to enroll against.
    """
    stmt = (
        select(Session)
        .where(
            Session.campaign_id == campaign_id,
            Session.status.in_(HISTORY_SESSION_STATUSES),
            Session.raw_audio_uri.is_not(None),
        )
        .order_by(Session.recorded_at.desc().nulls_last(), Session.created_at.desc())
        .limit(limit_sessions)
    )
    if exclude_session_id is not None:
        stmt = stmt.where(Session.id != exclude_session_id)
    sessions = list((await db.execute(stmt)).scalars().all())
    if not sessions:
        return []

    by_id = {session.id: session for session in sessions}
    rows = await db.execute(
        select(SpeakerAssignment)
        .where(
            SpeakerAssignment.session_id.in_(list(by_id)),
            SpeakerAssignment.status == CONFIRMED_STATUS,
            SpeakerAssignment.user_id.is_not(None),
        )
        .order_by(SpeakerAssignment.speaker_label)
    )
    order = {session.id: index for index, session in enumerate(sessions)}
    history = [
        {
            "session_id": assignment.session_id,
            "campaign_id": by_id[assignment.session_id].campaign_id,
            "audio_uri": by_id[assignment.session_id].raw_audio_uri,
            "session_status": by_id[assignment.session_id].status,
            "speaker_label": assignment.speaker_label,
            "user_id": assignment.user_id,
            "updated_at": assignment.updated_at,
        }
        for assignment in rows.scalars().all()
    ]
    # The join does not preserve the "newest session first" order of the SQL
    # above, so restore it (labels stay alphabetical inside one session).
    history.sort(key=lambda entry: (order[entry["session_id"]], entry["speaker_label"]))
    return history


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

    Together with confirm_assignment this is how a session's speakers get
    settled: the identification stage closes only when no label is left in
    'pending' or 'auto' (see _publish_assignment).
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

    return await _publish_assignment(
        db,
        session,
        assignment,
        display_name=display_name,
        character_name=character_name,
        assigned_by=assigned_by,
        publisher=publisher,
        enrolled_voiceprint=enrolled_voiceprint,
    )


async def get_assignment(
    db: AsyncSession, session_id: UUID, speaker_label: str
) -> SpeakerAssignment | None:
    """One diarized label's assignment (None when the label is unknown)."""
    return await db.scalar(
        select(SpeakerAssignment).where(
            SpeakerAssignment.session_id == session_id,
            SpeakerAssignment.speaker_label == speaker_label,
        )
    )


async def confirm_assignment(
    db: AsyncSession,
    session_id: UUID,
    speaker_label: str,
    *,
    member_id: UUID | None = None,
    display_name: str | None = None,
    character_name: str | None = None,
    assigned_by: UUID,
    publisher: EventPublisher,
    enrolled_voiceprint: bool = True,
) -> SpeakerAssignment:
    """DM confirms a speaker the pipeline proposed (auto) — emits speakers.assigned.

    Confirming is how an automatic match becomes usable knowledge: the
    assignment turns 'confirmed', which is what speaker-service learns voices
    from (see app.history there) and what makes the roster/character names
    available to content generation. member_id/display_name/character_name
    come from the campaign roster when the proposal can be traced back to a
    member; an unmatched proposal (or a campaign member who left) still
    confirms with the user link alone.

    Idempotent: confirming an already-confirmed label changes nothing and
    publishes nothing (a double click must not re-trigger the pipeline).
    """
    session = await get_session_or_404(db, session_id)
    assignment = await get_assignment(db, session_id, speaker_label)
    if assignment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No assignment for speaker {speaker_label}",
        )
    if assignment.status == CONFIRMED_STATUS:
        return assignment
    if assignment.user_id is None and member_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Speaker {speaker_label} has no proposed identity to confirm - "
                "name the speaker instead"
            ),
        )

    if member_id is not None:
        assignment.member_id = member_id
    assignment.status = CONFIRMED_STATUS
    assignment.assigned_by = assigned_by
    await db.commit()
    await db.refresh(assignment)

    return await _publish_assignment(
        db,
        session,
        assignment,
        display_name=display_name,
        character_name=character_name,
        assigned_by=assigned_by,
        publisher=publisher,
        enrolled_voiceprint=enrolled_voiceprint,
    )


async def _publish_assignment(
    db: AsyncSession,
    session: Session,
    assignment: SpeakerAssignment,
    *,
    display_name: str | None,
    character_name: str | None,
    assigned_by: UUID,
    publisher: EventPublisher,
    enrolled_voiceprint: bool,
) -> SpeakerAssignment:
    """Close the identification stage when it is done, then announce the name.

    The stage closes only once EVERY diarized label is 'confirmed' - named by
    the DM or accepted by them (an 'auto' match is a proposal, not a decision).
    That transition (speaker_pending -> speakers_identified) happens BEFORE the
    event is published, so the content worker always sees a session whose
    speakers are settled, and only then starts distilling it.
    """
    if session.status == SessionStatus.SPEAKER_PENDING.value:
        remaining = await db.scalar(
            select(func.count())
            .select_from(SpeakerAssignment)
            .where(
                SpeakerAssignment.session_id == session.id,
                SpeakerAssignment.status != CONFIRMED_STATUS,
            )
        )
        if remaining == 0:
            session.status = SessionStatus.SPEAKERS_IDENTIFIED.value
            await db.commit()

    await publisher.publish(
        Event(
            type="speakers.assigned",
            payload={
                "session_id": str(session.id),
                "campaign_id": str(session.campaign_id),
                "label": assignment.speaker_label,
                "member_id": str(assignment.member_id) if assignment.member_id else None,
                "user_id": str(assignment.user_id) if assignment.user_id else None,
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
