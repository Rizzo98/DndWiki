"""Internal speaker-history endpoint: the DM's namings of earlier sessions.

speaker-service learns from these entries (it turns the labelled audio into
voice samples), so what the endpoint returns - and what it refuses to return -
is part of the speaker-identification contract.
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.models import Session, SpeakerAssignment


async def _insert_session(session_factory, campaign_id, title="T", status="content_ready", **kw):
    async with session_factory() as db:
        session = Session(campaign_id=campaign_id, title=title, status=status, **kw)
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session


async def _assign(session_factory, session_id, label, user_id=None, status="confirmed"):
    async with session_factory() as db:
        db.add(
            SpeakerAssignment(
                session_id=session_id,
                speaker_label=label,
                user_id=user_id,
                status=status,
            )
        )
        await db.commit()


async def test_history_returns_confirmed_user_linked_labels(client, session_factory):
    campaign = uuid.uuid4()
    session = await _insert_session(
        session_factory, campaign, raw_audio_uri="recordings/x/raw.mp3"
    )
    named = uuid.uuid4()
    await _assign(session_factory, session.id, "SPEAKER_00", named, "confirmed")
    # auto (a guess), pending (nobody said so) and a userless member are NOT
    # samples: only the DM's own namings of user-linked members count.
    await _assign(session_factory, session.id, "SPEAKER_01", uuid.uuid4(), "auto")
    await _assign(session_factory, session.id, "SPEAKER_02", uuid.uuid4(), "pending")
    await _assign(session_factory, session.id, "SPEAKER_03", None, "confirmed")

    resp = await client.get(
        f"/internal/campaigns/{campaign}/speaker-history"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [entry["speaker_label"] for entry in body] == ["SPEAKER_00"]
    assert body[0]["user_id"] == str(named)
    assert body[0]["session_id"] == str(session.id)
    assert body[0]["campaign_id"] == str(campaign)
    assert body[0]["session_status"] == "content_ready"
    assert body[0]["audio_uri"] == "recordings/x/raw.mp3"


async def test_history_skips_unfinished_and_audioless_sessions(client, session_factory):
    campaign = uuid.uuid4()
    user = uuid.uuid4()
    # still being transcribed / identified: the DM may be mid-naming
    transcribing = await _insert_session(
        session_factory, campaign, status="transcribing", raw_audio_uri="recordings/a/raw.mp3"
    )
    identifying = await _insert_session(
        session_factory,
        campaign,
        status="identifying_speakers",
        raw_audio_uri="recordings/b/raw.mp3",
    )
    failed = await _insert_session(
        session_factory, campaign, status="failed", raw_audio_uri="recordings/c/raw.mp3"
    )
    # settled, but there is no recording to slice the samples out of
    no_audio = await _insert_session(session_factory, campaign, status="content_ready")
    for session in (transcribing, identifying, failed, no_audio):
        await _assign(session_factory, session.id, "SPEAKER_00", user, "confirmed")

    resp = await client.get(f"/internal/campaigns/{campaign}/speaker-history")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_history_includes_speaker_pending_sessions(client, session_factory):
    """A partly-named session still teaches the campaign its named voices."""
    campaign = uuid.uuid4()
    user = uuid.uuid4()
    session = await _insert_session(
        session_factory, campaign, status="speaker_pending", raw_audio_uri="recordings/a/raw.mp3"
    )
    await _assign(session_factory, session.id, "SPEAKER_00", user, "confirmed")

    resp = await client.get(f"/internal/campaigns/{campaign}/speaker-history")
    assert [entry["speaker_label"] for entry in resp.json()] == ["SPEAKER_00"]


async def test_history_excludes_the_session_being_identified(client, session_factory):
    """A session must never become a voice reference for itself."""
    campaign = uuid.uuid4()
    user = uuid.uuid4()
    previous = await _insert_session(
        session_factory, campaign, title="Episode 1", raw_audio_uri="recordings/1/raw.mp3"
    )
    current = await _insert_session(
        session_factory, campaign, title="Episode 2", raw_audio_uri="recordings/2/raw.mp3"
    )
    await _assign(session_factory, previous.id, "SPEAKER_00", user, "confirmed")
    await _assign(session_factory, current.id, "SPEAKER_00", user, "confirmed")

    resp = await client.get(
        f"/internal/campaigns/{campaign}/speaker-history",
        params={"exclude_session_id": str(current.id)},
    )
    assert [entry["session_id"] for entry in resp.json()] == [str(previous.id)]


async def test_history_is_newest_first_and_limited(client, session_factory):
    campaign = uuid.uuid4()
    user = uuid.uuid4()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    sessions = []
    for index in range(3):
        session = await _insert_session(
            session_factory,
            campaign,
            title=f"Episode {index}",
            raw_audio_uri=f"recordings/{index}/raw.mp3",
            recorded_at=base + timedelta(days=index),
        )
        await _assign(session_factory, session.id, "SPEAKER_00", user, "confirmed")
        await _assign(session_factory, session.id, "SPEAKER_01", user, "confirmed")
        sessions.append(session)

    resp = await client.get(
        f"/internal/campaigns/{campaign}/speaker-history", params={"limit_sessions": 2}
    )
    body = resp.json()
    # newest two sessions only, labels alphabetical inside a session
    assert [entry["session_id"] for entry in body] == [
        str(sessions[2].id),
        str(sessions[2].id),
        str(sessions[1].id),
        str(sessions[1].id),
    ]
    assert [entry["speaker_label"] for entry in body] == [
        "SPEAKER_00",
        "SPEAKER_01",
        "SPEAKER_00",
        "SPEAKER_01",
    ]


async def test_history_ignores_other_campaigns(client, session_factory):
    campaign = uuid.uuid4()
    other = uuid.uuid4()
    user = uuid.uuid4()
    session = await _insert_session(
        session_factory, other, status="content_ready", raw_audio_uri="recordings/a/raw.mp3"
    )
    await _assign(session_factory, session.id, "SPEAKER_00", user, "confirmed")

    resp = await client.get(f"/internal/campaigns/{campaign}/speaker-history")
    assert resp.json() == []


async def test_history_rejects_an_absurd_limit(client, session_factory):
    resp = await client.get(
        f"/internal/campaigns/{uuid.uuid4()}/speaker-history", params={"limit_sessions": 0}
    )
    assert resp.status_code == 422
