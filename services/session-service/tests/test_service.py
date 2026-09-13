"""Service-layer tests: transitions, artifacts, upload, speaker assignments."""

import hashlib
import io
import uuid

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import select

from app import services
from app.core.config import ServiceSettings
from app.models import Session, SessionRecording
from app.schemas import SpeakerAssignmentIn
from app.status import SessionStatus


def make_upload(
    data: bytes, content_type: str = "audio/mp4", filename: str = "rec.m4a"
) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(data),
        filename=filename,
        headers={"content-type": content_type},
    )


async def _create_session(db, campaign_id=None, title="T"):
    session = Session(campaign_id=campaign_id or uuid.uuid4(), title=title)
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def test_create_session_persists(session_factory):
    async with session_factory() as db:
        session = await services.create_session(db, uuid.uuid4(), "Into the Abyss", 2)
        assert session.status == SessionStatus.UPLOADED.value
        assert session.session_no == 2


async def test_transition_valid(session_factory):
    async with session_factory() as db:
        session = await _create_session(db)
        updated = await services.transition_status(
            db, session.id, SessionStatus.RECORDED
        )
        assert updated.status == SessionStatus.RECORDED.value


async def test_transition_invalid_raises_409(session_factory):
    async with session_factory() as db:
        session = await _create_session(db)
        with pytest.raises(HTTPException) as exc:
            await services.transition_status(db, session.id, SessionStatus.TRANSCRIBING)
        assert exc.value.status_code == 409


async def test_transition_unknown_session_404(session_factory):
    async with session_factory() as db:
        with pytest.raises(HTTPException) as exc:
            await services.transition_status(db, uuid.uuid4(), SessionStatus.RECORDED)
        assert exc.value.status_code == 404


async def test_update_artifacts(session_factory):
    async with session_factory() as db:
        session = await _create_session(db)
        updated = await services.update_artifacts(
            db,
            session.id,
            transcript_uri="transcripts/x/transcript.json",
            diarization_uri="transcripts/x/diarization.json",
            duration_sec=3724.5,
        )
        assert updated.transcript_uri.endswith("transcript.json")
        assert updated.duration_sec == 3724.5


async def test_upload_success(session_factory, fake_publisher, fake_storage):
    data = b"fake-audio-bytes" * 1000
    duration = 3724.5
    async with session_factory() as db:
        session = await _create_session(db)
        uploader = uuid.uuid4()
        result = await services.upload_recording(
            db,
            session.id,
            uploader,
            make_upload(data),
            fake_storage,
            fake_publisher,
            ServiceSettings(max_upload_mb=512),
            duration_sec=duration,
        )

        assert result.status == SessionStatus.RECORDED.value
        assert result.raw_audio_uri == f"recordings/{session.id}/raw.m4a"
        assert float(result.duration_sec) == duration

        rec = (await db.execute(select(SessionRecording))).scalars().one()
        assert rec.size_bytes == len(data)
        assert rec.sha256 == hashlib.sha256(data).hexdigest()
        assert rec.mime == "audio/mp4"
        assert rec.uploaded_by == uploader

        # streamed to storage with the right key
        bucket, key, payload, ctype = fake_storage.uploads[0]
        assert (bucket, key, payload, ctype) == (
            "recordings",
            f"recordings/{session.id}/raw.m4a",
            data,
            "audio/mp4",
        )

    # session.recorded published with contract payload
    event = fake_publisher.events[0]
    assert event.type == "session.recorded"
    payload = event.payload
    assert payload["session_id"] == str(result.id)
    assert payload["campaign_id"] == str(result.campaign_id)
    assert payload["recorded_by"] == str(uploader)
    assert payload["audio_uri"] == f"recordings/{session.id}/raw.m4a"
    assert payload["duration_sec"] == duration


async def test_upload_wrong_mime_415(session_factory, fake_publisher, fake_storage):
    async with session_factory() as db:
        session = await _create_session(db)
        with pytest.raises(HTTPException) as exc:
            await services.upload_recording(
                db,
                session.id,
                uuid.uuid4(),
                make_upload(b"x", content_type="application/pdf"),
                fake_storage,
                fake_publisher,
                ServiceSettings(),
            )
        assert exc.value.status_code == 415
    assert fake_storage.uploads == []
    assert fake_publisher.events == []


async def test_upload_empty_400(session_factory, fake_publisher, fake_storage):
    async with session_factory() as db:
        session = await _create_session(db)
        with pytest.raises(HTTPException) as exc:
            await services.upload_recording(
                db,
                session.id,
                uuid.uuid4(),
                make_upload(b""),
                fake_storage,
                fake_publisher,
                ServiceSettings(),
            )
        assert exc.value.status_code == 400


async def test_upload_too_large_413(session_factory, fake_publisher, fake_storage):
    big = b"x" * (1024 * 1024 + 1)  # 1 MiB + 1 byte
    async with session_factory() as db:
        session = await _create_session(db)
        with pytest.raises(HTTPException) as exc:
            await services.upload_recording(
                db,
                session.id,
                uuid.uuid4(),
                make_upload(big),
                fake_storage,
                fake_publisher,
                ServiceSettings(max_upload_mb=1),
            )
        assert exc.value.status_code == 413
    assert fake_storage.uploads == []
    assert fake_publisher.events == []


async def test_reupload_replaces_recording_and_clears_artifacts(
    session_factory, fake_publisher, fake_storage
):
    async with session_factory() as db:
        session = await _create_session(db)
        # Pretend the pipeline already ran and attached artifacts.
        session.status = SessionStatus.TRANSCRIBED.value
        session.raw_audio_uri = f"recordings/{session.id}/raw.mp3"
        session.transcript_uri = f"transcripts/{session.id}/transcript.json"
        session.diarization_uri = f"transcripts/{session.id}/diarization.json"
        session.error = "previous run failed"
        await db.commit()
        await db.refresh(session)

        await services.upload_recording(
            db,
            session.id,
            uuid.uuid4(),
            make_upload(b"audio2", content_type="audio/mp4"),
            fake_storage,
            fake_publisher,
            ServiceSettings(),
        )
        await db.refresh(session)

        assert session.status == SessionStatus.RECORDED.value
        assert session.raw_audio_uri == f"recordings/{session.id}/raw.m4a"
        assert session.transcript_uri is None
        assert session.diarization_uri is None
        assert session.error is None
        assert ("transcripts", f"transcripts/{session.id}/transcript.json") in fake_storage.deleted
        assert ("transcripts", f"transcripts/{session.id}/diarization.json") in fake_storage.deleted
        assert ("recordings", f"recordings/{session.id}/raw.mp3") in fake_storage.deleted
    assert len(fake_publisher.events) == 1


async def test_upsert_and_assign_speaker(session_factory, fake_publisher):
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    member_b = uuid.uuid4()
    async with session_factory() as db:
        session = await _create_session(db)

        assignments = await services.upsert_assignments(
            db,
            session.id,
            [
                SpeakerAssignmentIn(
                    speaker_label="SPEAKER_00", user_id=user_a, confidence=0.91, status="auto"
                ),
                SpeakerAssignmentIn(
                    speaker_label="SPEAKER_01", confidence=0.42, status="pending"
                ),
            ],
        )
        assert len(assignments) == 2

        # re-upsert updates in place (no duplicates)
        assignments = await services.upsert_assignments(
            db,
            session.id,
            [SpeakerAssignmentIn(speaker_label="SPEAKER_00", confidence=0.95, status="auto")],
        )
        assert len(assignments) == 2
        assert float(assignments[0].confidence) == 0.95

        # DM assigns the pending speaker -> confirmed + event (member
        # carries the user link, so user_id is mirrored for enrollment)
        dm = uuid.uuid4()
        assigned = await services.assign_speaker(
            db, session.id, "SPEAKER_01",
            member_id=member_b, user_id=user_b, display_name="Bob",
            character_name="Bobblin the Brave",
            assigned_by=dm, publisher=fake_publisher, enrolled_voiceprint=True,
        )
        assert assigned.status == "confirmed"
        assert assigned.member_id == member_b
        assert assigned.user_id == user_b
        assert assigned.assigned_by == dm

    event = fake_publisher.events[0]
    assert event.type == "speakers.assigned"
    assert event.payload["label"] == "SPEAKER_01"
    assert event.payload["member_id"] == str(member_b)
    assert event.payload["user_id"] == str(user_b)
    assert event.payload["display_name"] == "Bob"
    assert event.payload["character_name"] == "Bobblin the Brave"
    assert event.payload["assigned_by"] == str(dm)
    assert event.payload["enrolled_voiceprint"] is True
    # audio_uri lets speaker-service slice the voice out of the recording
    assert event.payload["audio_uri"] is None  # session has no recording yet


async def test_assign_last_pending_moves_to_identified(session_factory, fake_publisher):
    """Naming the last unnamed speaker closes identification (speaker_pending
    -> speakers_identified) so content generation can proceed - once nothing
    is left to decide, i.e. no other label is still an unaccepted proposal."""
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    async with session_factory() as db:
        session = await _create_session(db)
        session.status = SessionStatus.SPEAKER_PENDING.value
        await db.commit()

        await services.upsert_assignments(
            db,
            session.id,
            [
                SpeakerAssignmentIn(
                    speaker_label="SPEAKER_00", user_id=user_a, confidence=0.91, status="auto"
                ),
                SpeakerAssignmentIn(
                    speaker_label="SPEAKER_01", confidence=0.42, status="pending"
                ),
            ],
        )
        await services.assign_speaker(
            db, session.id, "SPEAKER_01",
            member_id=uuid.uuid4(), user_id=user_b, display_name="Bob",
            character_name=None,
            assigned_by=uuid.uuid4(), publisher=fake_publisher,
        )
        # SPEAKER_00 is still an unaccepted auto match: the panel is not done.
        await db.refresh(session)
        assert session.status == SessionStatus.SPEAKER_PENDING.value

        await services.confirm_assignment(
            db, session.id, "SPEAKER_00",
            member_id=uuid.uuid4(), display_name="Ann", character_name=None,
            assigned_by=uuid.uuid4(), publisher=fake_publisher,
        )
        await db.refresh(session)
        assert session.status == SessionStatus.SPEAKERS_IDENTIFIED.value


async def test_auto_match_keeps_the_stage_open_until_confirmed(
    session_factory, fake_publisher
):
    """An auto match is a proposal: the session waits for the DM to accept it
    (or pick someone else) before the next pipeline step runs."""
    user_a = uuid.uuid4()
    async with session_factory() as db:
        session = await _create_session(db)
        session.status = SessionStatus.SPEAKER_PENDING.value
        await db.commit()

        await services.upsert_assignments(
            db,
            session.id,
            [
                SpeakerAssignmentIn(
                    speaker_label="SPEAKER_00", user_id=user_a, confidence=0.93, status="auto"
                ),
                SpeakerAssignmentIn(
                    speaker_label="SPEAKER_01", user_id=user_a, confidence=0.88, status="auto"
                ),
            ],
        )
        # Naming is not needed for these labels, but confirming is.
        await services.assign_speaker(
            db, session.id, "SPEAKER_00",
            member_id=uuid.uuid4(), user_id=user_a, display_name="Ann",
            character_name=None,
            assigned_by=uuid.uuid4(), publisher=fake_publisher,
        )
        await db.refresh(session)
        assert session.status == SessionStatus.SPEAKER_PENDING.value

        await services.confirm_assignment(
            db, session.id, "SPEAKER_01",
            member_id=uuid.uuid4(), display_name="Ann", character_name=None,
            assigned_by=uuid.uuid4(), publisher=fake_publisher,
        )
        await db.refresh(session)
        assert session.status == SessionStatus.SPEAKERS_IDENTIFIED.value


async def test_assign_keeps_pending_when_others_remain(session_factory, fake_publisher):
    """Naming one of several pending speakers keeps the session pending."""
    user_a = uuid.uuid4()
    async with session_factory() as db:
        session = await _create_session(db)
        session.status = SessionStatus.SPEAKER_PENDING.value
        await db.commit()

        await services.upsert_assignments(
            db,
            session.id,
            [
                SpeakerAssignmentIn(
                    speaker_label="SPEAKER_00", confidence=0.2, status="pending"
                ),
                SpeakerAssignmentIn(
                    speaker_label="SPEAKER_01", confidence=0.3, status="pending"
                ),
            ],
        )
        await services.assign_speaker(
            db, session.id, "SPEAKER_00",
            member_id=uuid.uuid4(), user_id=user_a, display_name="Alice",
            character_name=None,
            assigned_by=uuid.uuid4(), publisher=fake_publisher,
        )
        await db.refresh(session)
        assert session.status == SessionStatus.SPEAKER_PENDING.value


async def test_assign_speaker_userless_member(session_factory, fake_publisher):
    """A member without a user account can still be named as a speaker.

    member_id is stored; user_id stays None and the event carries the
    member display name so content generation can label the transcript.
    """
    member = uuid.uuid4()
    async with session_factory() as db:
        session = await _create_session(db)
        assigned = await services.assign_speaker(
            db, session.id, "SPEAKER_02",
            member_id=member, user_id=None, display_name="Cedric the Bold",
            character_name="Cedric of the Vale",
            assigned_by=uuid.uuid4(), publisher=fake_publisher,
        )
        assert assigned.member_id == member
        assert assigned.user_id is None
        assert assigned.status == "confirmed"

    event = fake_publisher.events[0]
    assert event.type == "speakers.assigned"
    assert event.payload["member_id"] == str(member)
    assert event.payload["user_id"] is None
    assert event.payload["display_name"] == "Cedric the Bold"
