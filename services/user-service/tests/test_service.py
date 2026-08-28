"""Service-layer tests: profile provisioning/edits and voiceprint enrollment."""

import io
import uuid
from uuid import UUID

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import select

from app import services
from app.core.config import ServiceSettings
from app.models import User, VoiceProfile


def _upload(data: bytes, mime: str = "audio/m4a", name: str = "sample.m4a") -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(data), headers={"content-type": mime})


async def _enroll(
    session_factory,
    fakes,
    *,
    user: User,
    campaign_id: UUID,
    data: bytes = b"audio-bytes",
    mime: str = "audio/m4a",
    settings: ServiceSettings | None = None,
) -> VoiceProfile:
    settings = settings or ServiceSettings()
    async with session_factory() as db:
        return await services.enroll(
            db,
            user=user,
            campaign_id=campaign_id,
            upload=_upload(data, mime),
            storage=fakes.storage,
            embedder=fakes.embedder,
            voiceprints=fakes.voiceprints,
            settings=settings,
        )


# ------------------------------------------------------------- users


async def test_get_or_create_user_provisions_once(session_factory):
    sub = str(uuid.uuid4())
    async with session_factory() as db:
        first = await services.get_or_create_user(
            db, keycloak_sub=sub, email="a@test.local", display_name="Alice"
        )
        second = await services.get_or_create_user(db, keycloak_sub=sub, email="a@test.local")
        assert first.id == second.id
        assert first.display_name == "Alice"

    async with session_factory() as db:
        rows = (await db.execute(select(User))).scalars().all()
        assert len(rows) == 1


async def test_get_or_create_user_keeps_existing_profile(session_factory):
    sub = str(uuid.uuid4())
    async with session_factory() as db:
        first = await services.get_or_create_user(db, keycloak_sub=sub, display_name="Alice")
    async with session_factory() as db:
        # a later call with different claims must not overwrite the profile
        again = await services.get_or_create_user(db, keycloak_sub=sub, display_name="Mallory")
        assert again.id == first.id
        assert again.display_name == "Alice"


async def test_get_or_create_user_relinks_same_email(session_factory):
    """Deleting a Keycloak account and re-registering with the same email
    adopts the existing row (stable id) and re-links it to the new subject
    instead of violating the email unique constraint."""
    old_sub, new_sub = str(uuid.uuid4()), str(uuid.uuid4())
    async with session_factory() as db:
        original = await services.get_or_create_user(
            db, keycloak_sub=old_sub, email="same@test.local", display_name="Andre"
        )
    async with session_factory() as db:
        relinked = await services.get_or_create_user(
            db, keycloak_sub=new_sub, email="same@test.local", display_name="Andre 2"
        )
        assert relinked.id == original.id  # identity survives the account switch
        assert relinked.keycloak_sub == new_sub
        assert relinked.display_name == "Andre"  # profile not clobbered

    async with session_factory() as db:
        rows = (await db.execute(select(User))).scalars().all()
        assert len(rows) == 1


async def test_update_profile(session_factory, seed_user):
    user = await seed_user(display_name="Old Name")
    async with session_factory() as db:
        updated = await services.update_profile(db, user.id, display_name="New Name")
        assert updated.display_name == "New Name"


async def test_set_avatar(session_factory, seed_user):
    user = await seed_user()
    async with session_factory() as db:
        updated = await services.set_avatar(db, user.id, "wiki-assets/avatars/x.png")
        assert updated.avatar_uri == "wiki-assets/avatars/x.png"


async def test_resolve_users_skips_missing(session_factory, seed_user):
    existing = await seed_user()
    async with session_factory() as db:
        found = await services.resolve_users(db, [existing.id, uuid.uuid4()])
        assert list(found.keys()) == [existing.id]


async def test_resolve_users_matches_id_or_current_sub(session_factory, seed_user):
    """After a re-registration the row keeps its id while the current sub
    moves; the resolver must accept both identifiers for the same row."""
    user = await seed_user()
    new_sub = str(uuid.uuid4())
    async with session_factory() as db:
        row = await db.get(User, user.id)
        row.keycloak_sub = new_sub
        await db.commit()

    async with session_factory() as db:
        found = await services.resolve_users(db, [user.id, uuid.UUID(new_sub)])
        assert set(found.keys()) == {user.id, uuid.UUID(new_sub)}
        assert found[user.id] is found[uuid.UUID(new_sub)]  # the same row


async def test_get_user_or_404(session_factory):
    async with session_factory() as db:
        with pytest.raises(HTTPException) as exc:
            await services.get_user_or_404(db, uuid.uuid4())
        assert exc.value.status_code == 404


async def test_get_user_or_404_resolves_current_sub(session_factory, seed_user):
    """Re-registered accounts keep their id while the sub moves; the public
    lookup must accept either identifier so display names render for both
    profile-enrolled (stable id) and session-assigned (current sub) prints."""
    user = await seed_user()
    new_sub = str(uuid.uuid4())
    async with session_factory() as db:
        row = await db.get(User, user.id)
        row.keycloak_sub = new_sub
        await db.commit()

    async with session_factory() as db:
        found = await services.get_user_or_404(db, uuid.UUID(new_sub))
        assert found.id == user.id


# ------------------------------------------------------------- enroll


async def test_enroll_happy_path(session_factory, fakes, seed_user):
    user = await seed_user()
    campaign_id = uuid.uuid4()

    profile = await _enroll(session_factory, fakes, user=user, campaign_id=campaign_id)

    assert profile.user_id == user.id
    assert profile.campaign_id == campaign_id
    assert profile.embedding_version == 1
    assert profile.sample_uri.startswith("voice-samples/")

    # one clip streamed to MinIO
    assert len(fakes.storage.uploads) == 1
    assert fakes.storage.uploads[0]["bucket"] == "voice-samples"

    # one vector upserted into Qdrant with the documented payload
    assert len(fakes.voiceprints.points) == 1
    point = fakes.voiceprints.points[profile.qdrant_point]
    vector, payload = point
    assert len(vector) == 192
    assert payload["user_id"] == str(user.id)
    assert payload["campaign_id"] == str(campaign_id)
    assert payload["sample_uri"] == profile.sample_uri
    assert payload["version"] == 1


async def test_enroll_replaces_existing_profile(session_factory, fakes, seed_user):
    user = await seed_user()
    campaign_id = uuid.uuid4()

    first = await _enroll(session_factory, fakes, user=user, campaign_id=campaign_id)
    old_point = first.qdrant_point
    fakes.embedder.calls.clear()
    fakes.storage.uploads.clear()

    second = await _enroll(session_factory, fakes, user=user, campaign_id=campaign_id)

    assert second.id == first.id  # same row, replaced in place
    assert second.qdrant_point != old_point
    # the old Qdrant point is gone, only the new one remains
    assert old_point not in fakes.voiceprints.points
    assert list(fakes.voiceprints.points.keys()) == [second.qdrant_point]

    async with session_factory() as db:
        rows = (await db.execute(select(VoiceProfile))).scalars().all()
        assert len(rows) == 1


async def test_enroll_rejects_unsupported_mime(session_factory, fakes, seed_user):
    user = await seed_user()
    with pytest.raises(HTTPException) as exc:
        await _enroll(session_factory, fakes, user=user, campaign_id=uuid.uuid4(), mime="text/plain")
    assert exc.value.status_code == 415


async def test_enroll_rejects_empty_clip(session_factory, fakes, seed_user):
    user = await seed_user()
    with pytest.raises(HTTPException) as exc:
        await _enroll(session_factory, fakes, user=user, campaign_id=uuid.uuid4(), data=b"")
    assert exc.value.status_code == 400


async def test_enroll_rejects_oversized_clip(session_factory, fakes, seed_user):
    user = await seed_user()
    settings = ServiceSettings(max_voice_sample_mb=1)
    with pytest.raises(HTTPException) as exc:
        await _enroll(
            session_factory,
            fakes,
            user=user,
            campaign_id=uuid.uuid4(),
            data=b"x" * (1024 * 1024 + 1),
            settings=settings,
        )
    assert exc.value.status_code == 413


async def test_enroll_rejects_short_sample(session_factory, fakes, seed_user):
    user = await seed_user()
    fakes.embedder.duration = 1.0  # below voice_sample_min_sec
    with pytest.raises(HTTPException) as exc:
        await _enroll(session_factory, fakes, user=user, campaign_id=uuid.uuid4())
    assert exc.value.status_code == 400


async def test_enroll_rejected_clip_not_stored(session_factory, fakes, seed_user):
    """Duration validation happens before MinIO/Qdrant writes."""
    user = await seed_user()
    fakes.embedder.duration = 1.0
    with pytest.raises(HTTPException):
        await _enroll(session_factory, fakes, user=user, campaign_id=uuid.uuid4())
    assert fakes.storage.uploads == []
    assert fakes.voiceprints.points == {}


# ------------------------------------------------------------- list/delete


async def test_list_profiles_filters_by_campaign(session_factory, fakes, seed_user):
    user = await seed_user()
    c1, c2 = uuid.uuid4(), uuid.uuid4()
    await _enroll(session_factory, fakes, user=user, campaign_id=c1)
    await _enroll(session_factory, fakes, user=user, campaign_id=c2)

    async with session_factory() as db:
        all_profiles = await services.list_profiles(db, user.id)
        only_c1 = await services.list_profiles(db, user.id, campaign_id=c1)
        assert len(all_profiles) == 2
        assert [p.campaign_id for p in only_c1] == [c1]


async def test_delete_profile_removes_point_and_row(session_factory, fakes, seed_user):
    user = await seed_user()
    profile = await _enroll(session_factory, fakes, user=user, campaign_id=uuid.uuid4())
    assert profile.qdrant_point in fakes.voiceprints.points

    async with session_factory() as db:
        await services.delete_profile(db, profile, fakes.voiceprints)

    assert fakes.voiceprints.points == {}
    async with session_factory() as db:
        assert await db.get(VoiceProfile, profile.id) is None
