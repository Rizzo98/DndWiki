"""Model roundtrip tests against in-memory SQLite."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Session, SessionRecording, SpeakerAssignment


async def test_session_defaults(session_factory):
    async with session_factory() as db:
        session = Session(campaign_id=uuid.uuid4(), title="Session 1")
        db.add(session)
        await db.commit()
        await db.refresh(session)

        assert session.status == "uploaded"
        assert session.id is not None
        assert session.created_at is not None


async def test_session_roundtrip(session_factory):
    campaign = uuid.uuid4()
    async with session_factory() as db:
        session = Session(campaign_id=campaign, title="Dungeon of Doom", session_no=3)
        db.add(session)
        await db.commit()
        await db.refresh(session)
        sid = session.id

        rec = SessionRecording(
            session_id=sid,
            uploaded_by=uuid.uuid4(),
            file_uri=f"recordings/{sid}/raw.m4a",
            size_bytes=1234,
            mime="audio/mp4",
            sha256="a" * 64,
        )
        db.add(rec)
        await db.commit()

    async with session_factory() as db:
        loaded = await db.get(Session, sid)
        assert loaded.title == "Dungeon of Doom"
        assert loaded.session_no == 3
        recordings = (await db.execute(select(SessionRecording))).scalars().all()
        assert len(recordings) == 1
        assert recordings[0].sha256 == "a" * 64


async def test_speaker_assignment_unique_per_label(session_factory):
    async with session_factory() as db:
        session = Session(campaign_id=uuid.uuid4())
        db.add(session)
        await db.commit()
        await db.refresh(session)
        sid = session.id

    async with session_factory() as db:
        db.add(SpeakerAssignment(session_id=sid, speaker_label="SPEAKER_00"))
        await db.commit()

    async with session_factory() as db:
        db.add(SpeakerAssignment(session_id=sid, speaker_label="SPEAKER_00"))
        with pytest.raises(IntegrityError):
            await db.commit()
