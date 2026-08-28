"""Model roundtrip tests against in-memory SQLite."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import User, VoiceProfile


async def _user(session_factory, keycloak_sub="sub-1"):
    async with session_factory() as db:
        user = User(keycloak_sub=keycloak_sub, email="a@test.local", display_name="Alice")
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


async def test_user_defaults(session_factory):
    async with session_factory() as db:
        user = User(keycloak_sub="sub-abc", email=None, display_name="Bob")
        db.add(user)
        await db.commit()
        await db.refresh(user)

        assert user.id is not None
        assert user.display_name == "Bob"
        assert user.email is None
        assert user.avatar_uri is None
        assert user.created_at is not None


async def test_user_keycloak_sub_unique(session_factory):
    await _user(session_factory, keycloak_sub="same-sub")
    async with session_factory() as db:
        db.add(User(keycloak_sub="same-sub", display_name="Other"))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_user_email_unique(session_factory):
    await _user(session_factory, keycloak_sub="sub-1")
    async with session_factory() as db:
        db.add(User(keycloak_sub="sub-2", email="a@test.local", display_name="Other"))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_voice_profile_defaults(session_factory):
    user = await _user(session_factory)
    async with session_factory() as db:
        profile = VoiceProfile(
            user_id=user.id,
            campaign_id=uuid.uuid4(),
            qdrant_point="point-1",
            sample_uri="voice-samples/u/c/sample.wav",
        )
        db.add(profile)
        await db.commit()
        await db.refresh(profile)

        assert profile.embedding_version == 1
        assert profile.id is not None
        assert profile.created_at is not None


async def test_voice_profile_unique_user_campaign(session_factory):
    user = await _user(session_factory)
    campaign_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(
            VoiceProfile(
                user_id=user.id,
                campaign_id=campaign_id,
                qdrant_point="p-1",
                sample_uri="voice-samples/a",
            )
        )
        await db.commit()

    async with session_factory() as db:
        db.add(
            VoiceProfile(
                user_id=user.id,
                campaign_id=campaign_id,
                qdrant_point="p-2",
                sample_uri="voice-samples/b",
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_voice_profile_same_user_other_campaign_ok(session_factory):
    user = await _user(session_factory)
    async with session_factory() as db:
        for n in range(2):
            db.add(
                VoiceProfile(
                    user_id=user.id,
                    campaign_id=uuid.uuid4(),
                    qdrant_point=f"p-{n}",
                    sample_uri=f"voice-samples/{n}",
                )
            )
        await db.commit()

    async with session_factory() as db:
        from sqlalchemy import select

        profiles = (await db.execute(select(VoiceProfile))).scalars().all()
        assert len(profiles) == 2  # voiceprints are campaign-scoped
