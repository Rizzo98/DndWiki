"""HTTP API tests (public + internal) with all externals faked."""

import uuid

from app import deps
from app.core.config import ServiceSettings
from app.core.config import get_settings as get_service_settings
from app.main import app
from app.models import Session


async def _insert_session(session_factory, campaign_id, title="T", status="uploaded", **kw):
    async with session_factory() as db:
        session = Session(campaign_id=campaign_id, title=title, status=status, **kw)
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session


# ---------------------------------------------------------------- public


async def test_create_session_201(client, fake_campaign, user_id):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "dm"
    resp = await client.post(
        "/api/sessions",
        json={"campaign_id": str(campaign), "title": "Episode 1", "session_no": 1},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "uploaded"
    assert body["campaign_id"] == str(campaign)
    assert body["title"] == "Episode 1"
    assert body["session_no"] == 1


async def test_create_session_403_non_member(client, fake_campaign, user_id):
    resp = await client.post("/api/sessions", json={"campaign_id": str(uuid.uuid4())})
    assert resp.status_code == 403


async def test_create_session_503_when_campaign_down(client, fake_campaign, user_id):
    fake_campaign.unavailable = True
    resp = await client.post("/api/sessions", json={"campaign_id": str(uuid.uuid4())})
    assert resp.status_code == 503


async def test_list_sessions(client, fake_campaign, user_id, session_factory):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    await _insert_session(session_factory, campaign, title="A")
    await _insert_session(session_factory, campaign, title="B")
    await _insert_session(session_factory, uuid.uuid4(), title="Other campaign")

    resp = await client.get("/api/sessions", params={"campaign_id": str(campaign)})
    assert resp.status_code == 200
    titles = sorted(s["title"] for s in resp.json())
    assert titles == ["A", "B"]


async def test_get_session_404(client, fake_campaign, user_id):
    resp = await client.get(f"/api/sessions/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_get_session_detail_presigned_urls(
    client, fake_campaign, user_id, session_factory
):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    session = await _insert_session(session_factory, campaign)
    async with session_factory() as db:
        row = await db.get(Session, session.id)
        row.raw_audio_uri = f"recordings/{session.id}/raw.m4a"
        row.transcript_uri = f"transcripts/{session.id}/transcript.json"
        await db.commit()

    resp = await client.get(f"/api/sessions/{session.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "uploaded"
    assert body["raw_audio_url"].startswith("https://presigned/")
    assert body["transcript_url"].startswith("https://presigned/")


async def test_upload_recording_success(
    client, fake_campaign, user_id, fake_publisher, session_factory
):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    session = await _insert_session(session_factory, campaign)

    resp = await client.put(
        f"/api/sessions/{session.id}/recording",
        files={"file": ("rec.m4a", b"fake-audio-bytes", "audio/mp4")},
        data={"duration_sec": "3724.5"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "recorded"
    assert body["raw_audio_uri"] == f"recordings/{session.id}/raw.m4a"
    assert body["raw_audio_url"].startswith("https://presigned/")

    event = fake_publisher.events[0]
    assert event.type == "session.recorded"
    assert event.payload["session_id"] == str(session.id)


async def test_upload_recording_415(client, fake_campaign, user_id, session_factory):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    session = await _insert_session(session_factory, campaign)

    resp = await client.put(
        f"/api/sessions/{session.id}/recording",
        files={"file": ("notes.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert resp.status_code == 415


async def test_upload_recording_413(client, fake_campaign, user_id, session_factory):
    app.dependency_overrides[get_service_settings] = lambda: ServiceSettings(max_upload_mb=1)
    try:
        campaign = uuid.uuid4()
        fake_campaign.roles[(campaign, user_id)] = "player"
        session = await _insert_session(session_factory, campaign)
        resp = await client.put(
            f"/api/sessions/{session.id}/recording",
            files={"file": ("rec.m4a", b"x" * (1024 * 1024 + 1), "audio/mp4")},
        )
        assert resp.status_code == 413
    finally:
        app.dependency_overrides.pop(get_service_settings, None)


async def test_patch_session_dm_allowed(client, fake_campaign, user_id, session_factory):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "dm"
    session = await _insert_session(session_factory, campaign)
    resp = await client.patch(f"/api/sessions/{session.id}", json={"title": "Renamed"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Renamed"


async def test_patch_session_player_forbidden(client, fake_campaign, user_id, session_factory):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    session = await _insert_session(session_factory, campaign)
    resp = await client.patch(f"/api/sessions/{session.id}", json={"title": "Nope"})
    assert resp.status_code == 403


async def test_speaker_assign_flow(client, fake_campaign, user_id, fake_publisher, session_factory):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "dm"
    session = await _insert_session(session_factory, campaign)
    other_user = uuid.uuid4()
    member = uuid.uuid4()
    fake_campaign.members[member] = {
        "id": str(member),
        "campaign_id": str(campaign),
        "user_id": str(other_user),
        "role": "player",
        "player_name": "Bob",
        "character_name": "Cedric",
    }

    # speaker-service reports two labels
    resp = await client.post(
        f"/internal/sessions/{session.id}/speakers",
        json=[
            {"speaker_label": "SPEAKER_00", "confidence": 0.91, "status": "auto"},
            {"speaker_label": "SPEAKER_01", "confidence": 0.42, "status": "pending"},
        ],
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 2

    # members can list
    resp = await client.get(f"/api/sessions/{session.id}/speakers")
    assert resp.status_code == 200
    assert len(resp.json()) == 2

    # DM assigns the pending one by member id (member carries the user link)
    resp = await client.post(
        f"/api/sessions/{session.id}/speakers/SPEAKER_01/assign",
        json={"member_id": str(member), "enrolled_voiceprint": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["member_id"] == str(member)
    assert body["user_id"] == str(other_user)
    assert fake_publisher.events[0].type == "speakers.assigned"
    assert fake_publisher.events[0].payload["display_name"] == "Bob"


async def test_speaker_assign_userless_member(client, fake_campaign, user_id, session_factory):
    """A member without a linked user can be named as a speaker."""
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "dm"
    session = await _insert_session(session_factory, campaign)
    member = uuid.uuid4()
    fake_campaign.members[member] = {
        "id": str(member),
        "campaign_id": str(campaign),
        "user_id": None,
        "role": "player",
        "player_name": "Cedric the Bold",
        "character_name": "Cedric",
    }
    await client.post(
        f"/internal/sessions/{session.id}/speakers",
        json=[{"speaker_label": "SPEAKER_00", "confidence": 0.3, "status": "pending"}],
    )

    resp = await client.post(
        f"/api/sessions/{session.id}/speakers/SPEAKER_00/assign",
        json={"member_id": str(member)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["member_id"] == str(member)
    assert body["user_id"] is None


async def test_speaker_assign_unknown_member_404(client, fake_campaign, user_id, session_factory):
    """Assigning a member from another campaign (or a bogus id) is a 404."""
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "dm"
    session = await _insert_session(session_factory, campaign)
    await client.post(
        f"/internal/sessions/{session.id}/speakers",
        json=[{"speaker_label": "SPEAKER_00", "confidence": 0.3, "status": "pending"}],
    )

    resp = await client.post(
        f"/api/sessions/{session.id}/speakers/SPEAKER_00/assign",
        json={"member_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------- internal


async def test_internal_status_update(client, session_factory):
    session = await _insert_session(session_factory, uuid.uuid4())
    resp = await client.patch(
        f"/internal/sessions/{session.id}/status", json={"status": "recorded"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "recorded"


async def test_internal_status_invalid_transition(client, session_factory):
    session = await _insert_session(session_factory, uuid.uuid4())
    resp = await client.patch(
        f"/internal/sessions/{session.id}/status", json={"status": "transcribing"}
    )
    assert resp.status_code == 409


async def test_internal_status_failed_sets_error(client, session_factory):
    session = await _insert_session(session_factory, uuid.uuid4())
    resp = await client.patch(
        f"/internal/sessions/{session.id}/status",
        json={"status": "failed", "error": "whisper ran out of memory"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error"] == "whisper ran out of memory"


async def test_internal_artifacts_update(client, session_factory):
    session = await _insert_session(session_factory, uuid.uuid4())
    resp = await client.patch(
        f"/internal/sessions/{session.id}/artifacts",
        json={
            "transcript_uri": f"transcripts/{session.id}/transcript.json",
            "diarization_uri": f"transcripts/{session.id}/diarization.json",
            "duration_sec": 3724.5,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["transcript_uri"].endswith("transcript.json")
    assert body["duration_sec"] == 3724.5


async def test_internal_requires_service_token(client, session_factory):
    app.dependency_overrides.pop(deps.require_service, None)
    try:
        session = await _insert_session(session_factory, uuid.uuid4())
        resp = await client.patch(
            f"/internal/sessions/{session.id}/status", json={"status": "recorded"}
        )
        assert resp.status_code == 401
    finally:
        app.dependency_overrides[deps.require_service] = lambda: None
