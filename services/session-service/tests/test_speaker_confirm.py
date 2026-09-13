"""Confirming an automatic speaker match (POST .../speakers/{label}/confirm).

Confirming is what makes a match usable: the assignment turns 'confirmed', which
is what speaker-service learns voices from, and the roster member behind the
match is filled in so generation has the player/character names.
"""

import uuid

from app.models import Session


async def _insert_session(session_factory, campaign_id, status="speaker_pending", **kw):
    async with session_factory() as db:
        session = Session(
            campaign_id=campaign_id,
            title="Episode 1",
            status=status,
            raw_audio_uri=f"recordings/{uuid.uuid4()}/raw.mp3",
            **kw,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session


async def _auto_speaker(client, session_id, label, user_id):
    """The pipeline's output: a label matched to a user, waiting for the DM."""
    resp = await client.post(
        f"/internal/sessions/{session_id}/speakers",
        json=[{"speaker_label": label, "user_id": str(user_id), "confidence": 0.91, "status": "auto"}],
    )
    assert resp.status_code == 200
    return resp.json()[0]


def _member(campaign_id, member_id, user_id, player_name="Bob", character_name="Borin"):
    return {
        "id": str(member_id),
        "campaign_id": str(campaign_id),
        "user_id": str(user_id) if user_id else None,
        "role": "player",
        "player_name": player_name,
        "character_name": character_name,
    }


async def test_confirm_auto_match(
    client, fake_campaign, fake_publisher, session_factory, user_id
):
    """The DM accepts the proposed name: it becomes a confirmed assignment."""
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "dm"
    player_user = uuid.uuid4()
    member = uuid.uuid4()
    fake_campaign.members[member] = _member(campaign, member, player_user)
    session = await _insert_session(session_factory, campaign)
    await _auto_speaker(client, session.id, "SPEAKER_00", player_user)

    resp = await client.post(
        f"/api/sessions/{session.id}/speakers/SPEAKER_00/confirm"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["user_id"] == str(player_user)
    # the roster row behind the match is filled in (character names for generation)
    assert body["member_id"] == str(member)
    assert body["assigned_by"] == str(user_id)

    assert [event.type for event in fake_publisher.events] == ["speakers.assigned"]
    payload = fake_publisher.events[0].payload
    assert payload["label"] == "SPEAKER_00"
    assert payload["user_id"] == str(player_user)
    assert payload["member_id"] == str(member)
    assert payload["display_name"] == "Bob"
    assert payload["character_name"] == "Borin"
    assert payload["enrolled_voiceprint"] is True
    assert payload["audio_uri"] == session.raw_audio_uri


async def test_confirm_last_speaker_closes_identification(
    client, fake_campaign, fake_publisher, session_factory, user_id
):
    """Confirming the last unconfirmed label ends the identification stage."""
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "dm"
    user = uuid.uuid4()
    member = uuid.uuid4()
    fake_campaign.members[member] = _member(campaign, member, user)
    session = await _insert_session(session_factory, campaign, status="speaker_pending")
    await _auto_speaker(client, session.id, "SPEAKER_00", user)

    resp = await client.post(f"/api/sessions/{session.id}/speakers/SPEAKER_00/confirm")
    assert resp.status_code == 200
    detail = await client.get(f"/api/sessions/{session.id}")
    assert detail.json()["status"] == "speakers_identified"


async def test_confirm_keeps_the_user_link_without_a_roster_member(
    client, fake_campaign, fake_publisher, session_factory, user_id
):
    """A match for an account that left the campaign still confirms (by user)."""
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "dm"
    ghost_user = uuid.uuid4()  # enrolled a voiceprint once, no member row today
    session = await _insert_session(session_factory, campaign)
    await _auto_speaker(client, session.id, "SPEAKER_00", ghost_user)

    resp = await client.post(f"/api/sessions/{session.id}/speakers/SPEAKER_00/confirm")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["user_id"] == str(ghost_user)
    assert body["member_id"] is None
    assert fake_publisher.events[0].payload["member_id"] is None


async def test_confirm_twice_publishes_once(
    client, fake_campaign, fake_publisher, session_factory, user_id
):
    """A double click must not re-trigger the pipeline (voiceprint, summary)."""
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "dm"
    user = uuid.uuid4()
    session = await _insert_session(session_factory, campaign)
    await _auto_speaker(client, session.id, "SPEAKER_00", user)

    first = await client.post(f"/api/sessions/{session.id}/speakers/SPEAKER_00/confirm")
    second = await client.post(f"/api/sessions/{session.id}/speakers/SPEAKER_00/confirm")
    assert first.status_code == second.status_code == 200
    assert second.json()["status"] == "confirmed"
    assert len(fake_publisher.events) == 1


async def test_confirm_pending_without_an_identity_409(
    client, fake_campaign, fake_publisher, session_factory, user_id
):
    """Nothing was proposed: the DM must name the speaker instead."""
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "dm"
    session = await _insert_session(session_factory, campaign)
    await client.post(
        f"/internal/sessions/{session.id}/speakers",
        json=[{"speaker_label": "SPEAKER_01", "confidence": 0.3, "status": "pending"}],
    )

    resp = await client.post(f"/api/sessions/{session.id}/speakers/SPEAKER_01/confirm")
    assert resp.status_code == 409
    assert fake_publisher.events == []


async def test_confirm_unknown_label_404(
    client, fake_campaign, fake_publisher, session_factory, user_id
):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "dm"
    session = await _insert_session(session_factory, campaign)

    resp = await client.post(f"/api/sessions/{session.id}/speakers/SPEAKER_07/confirm")
    assert resp.status_code == 404
    assert fake_publisher.events == []


async def test_confirm_requires_dm(
    client, fake_campaign, fake_publisher, session_factory, user_id
):
    """A player cannot confirm speakers."""
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "dm"
    fake_campaign.roles[(campaign, user_id)] = "player"
    user = uuid.uuid4()
    session = await _insert_session(session_factory, campaign)
    await _auto_speaker(client, session.id, "SPEAKER_00", user)

    resp = await client.post(f"/api/sessions/{session.id}/speakers/SPEAKER_00/confirm")
    assert resp.status_code == 403
    assert fake_publisher.events == []


async def test_confirm_survives_a_campaign_service_outage(
    client, fake_campaign, session_factory, user_id
):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "dm"
    user = uuid.uuid4()
    session = await _insert_session(session_factory, campaign)
    await _auto_speaker(client, session.id, "SPEAKER_00", user)
    fake_campaign.unavailable = True

    resp = await client.post(f"/api/sessions/{session.id}/speakers/SPEAKER_00/confirm")
    assert resp.status_code == 503