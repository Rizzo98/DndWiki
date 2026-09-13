"""DELETE /api/sessions/{id}: the DM's escape hatch for a run that went wrong.

A session may only be deleted while it has not generated its wiki updates:
from 'applying_wiki' on, its pages and timeline entries exist and would outlive
it. The happy path purges the recording, the rows content-service holds for the
session, and the session itself (uploads + speaker assignments included).
"""

import uuid

from sqlalchemy import func, select

from app.models import Session, SessionRecording, SpeakerAssignment


async def _insert_session(session_factory, campaign_id, title="T", status="recorded", **kw):
    async with session_factory() as db:
        session = Session(campaign_id=campaign_id, title=title, status=status, **kw)
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session


async def _seed_children(session_factory, session_id):
    """One upload row and one speaker assignment (both must go with the session)."""
    async with session_factory() as db:
        db.add(
            SessionRecording(
                session_id=session_id,
                uploaded_by=uuid.uuid4(),
                file_uri=f"recordings/{session_id}/raw.m4a",
                size_bytes=10,
                mime="audio/mp4",
                sha256="0" * 64,
            )
        )
        db.add(SpeakerAssignment(session_id=session_id, speaker_label="SPEAKER_00"))
        await db.commit()


async def test_delete_session_purges_everything(
    client, fake_campaign, fake_content, fake_storage, user_id, session_factory
):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "dm"
    session = await _insert_session(
        session_factory,
        campaign,
        status="summary_ready",
        raw_audio_uri="recordings/raw.m4a",
        transcript_uri="transcripts/t.json",
        diarization_uri="transcripts/d.json",
    )
    await _seed_children(session_factory, session.id)

    resp = await client.delete(f"/api/sessions/{session.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True
    assert body["session_id"] == str(session.id)
    assert body["title"] == "T"

    # content-service was asked to forget the session's generated rows
    assert fake_content.calls == [str(session.id)]
    # the recording + both artifacts left MinIO
    assert (("recordings", "recordings/raw.m4a")) in fake_storage.deleted
    assert (("transcripts", "transcripts/t.json")) in fake_storage.deleted
    assert (("transcripts", "transcripts/d.json")) in fake_storage.deleted

    # the session is gone
    assert (await client.get(f"/api/sessions/{session.id}")).status_code == 404
    async with session_factory() as db:
        assert await db.get(Session, session.id) is None


async def test_delete_session_removes_children_rows(
    client, fake_campaign, fake_storage, user_id, session_factory
):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "dm"
    session = await _insert_session(session_factory, campaign)
    await _seed_children(session_factory, session.id)

    resp = await client.delete(f"/api/sessions/{session.id}")
    assert resp.status_code == 200

    async with session_factory() as db:
        recordings = await db.scalar(
            select(func.count())
            .select_from(SessionRecording)
            .where(SessionRecording.session_id == session.id)
        )
        assignments = await db.scalar(
            select(func.count())
            .select_from(SpeakerAssignment)
            .where(SpeakerAssignment.session_id == session.id)
        )
    assert recordings == 0
    assert assignments == 0


async def test_delete_session_409_once_the_wiki_is_written(
    client, fake_campaign, fake_content, user_id, session_factory
):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "dm"
    for status in ("applying_wiki", "content_ready", "reviewed", "published"):
        session = await _insert_session(session_factory, campaign, status=status)
        resp = await client.delete(f"/api/sessions/{session.id}")
        assert resp.status_code == 409, status
        assert "already generated" in resp.json()["detail"]
        assert (await client.get(f"/api/sessions/{session.id}")).status_code == 200
    # nothing was purged
    assert fake_content.calls == []


async def test_delete_session_409_when_content_service_refuses(
    client, fake_campaign, fake_content, user_id, session_factory
):
    """A 'failed' session may still have written wiki content: content-service
    is the one that knows, and its refusal is passed through."""
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "dm"
    session = await _insert_session(session_factory, campaign, status="failed")
    fake_content.refuse = "this session already wrote 3 page(s) (Aragorn) into the wiki"

    resp = await client.delete(f"/api/sessions/{session.id}")
    assert resp.status_code == 409
    assert "already wrote" in resp.json()["detail"]
    # the session survives a refused deletion
    assert (await client.get(f"/api/sessions/{session.id}")).status_code == 200


async def test_delete_session_503_when_content_service_is_down(
    client, fake_campaign, fake_content, user_id, session_factory
):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "dm"
    session = await _insert_session(session_factory, campaign)
    fake_content.unavailable = True

    resp = await client.delete(f"/api/sessions/{session.id}")
    assert resp.status_code == 503
    assert (await client.get(f"/api/sessions/{session.id}")).status_code == 200


async def test_delete_session_403_for_players(
    client, fake_campaign, user_id, session_factory
):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    session = await _insert_session(session_factory, campaign)

    resp = await client.delete(f"/api/sessions/{session.id}")
    assert resp.status_code == 403
    assert (await client.get(f"/api/sessions/{session.id}")).status_code == 200


async def test_delete_session_404(client, fake_campaign, user_id):
    resp = await client.delete(f"/api/sessions/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_can_delete_flag_tracks_the_pipeline(
    client, fake_campaign, user_id, session_factory
):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "dm"
    await _insert_session(session_factory, campaign, title="early", status="summary_ready")
    await _insert_session(session_factory, campaign, title="late", status="content_ready")

    resp = await client.get("/api/sessions", params={"campaign_id": str(campaign)})
    assert resp.status_code == 200
    flags = {s["title"]: s["can_delete"] for s in resp.json()}
    assert flags == {"early": True, "late": False}
