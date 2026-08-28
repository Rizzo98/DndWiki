"""Model roundtrip tests against in-memory SQLite."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Campaign, CampaignMember, Invite


async def _campaign(session_factory, slug="the-fellowship"):
    async with session_factory() as db:
        campaign = Campaign(name="The Fellowship", slug=slug, dm_user_id=uuid.uuid4())
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)
        return campaign


async def test_campaign_defaults(session_factory):
    async with session_factory() as db:
        campaign = Campaign(name="The Fellowship", slug="the-fellowship", dm_user_id=uuid.uuid4())
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)

        assert campaign.status == "active"
        assert campaign.settings == {}
        assert campaign.id is not None
        assert campaign.created_at is not None


async def test_campaign_slug_unique(session_factory):
    await _campaign(session_factory, slug="same-slug")
    async with session_factory() as db:
        db.add(Campaign(name="Other", slug="same-slug", dm_user_id=uuid.uuid4()))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_member_gets_surrogate_id(session_factory):
    campaign = await _campaign(session_factory)
    async with session_factory() as db:
        member = CampaignMember(campaign_id=campaign.id, user_id=None, role="player")
        db.add(member)
        await db.commit()
        await db.refresh(member)
        assert member.id is not None
        assert member.player_name == ""  # Python-side defaults apply
        assert member.character_name == ""


async def test_member_user_link_unique_per_campaign(session_factory):
    """A user can be linked at most once per campaign; unlinked rows (NULL
    user_id) are exempt from the unique constraint."""
    campaign = await _campaign(session_factory)
    uid = uuid.uuid4()
    async with session_factory() as db:
        db.add(CampaignMember(campaign_id=campaign.id, user_id=uid, role="player"))
        await db.commit()

    async with session_factory() as db:
        db.add(CampaignMember(campaign_id=campaign.id, user_id=uid, role="player"))
        with pytest.raises(IntegrityError):
            await db.commit()

    # many members without a user link coexist (NULLs are distinct)
    async with session_factory() as db:
        db.add(CampaignMember(campaign_id=campaign.id, user_id=None, role="player"))
        db.add(CampaignMember(campaign_id=campaign.id, user_id=None, role="player"))
        await db.commit()

    # the same user may link in a different campaign
    other = await _campaign(session_factory, slug="other-campaign")
    async with session_factory() as db:
        db.add(CampaignMember(campaign_id=other.id, user_id=uid, role="player"))
        await db.commit()


async def test_invite_token_unique(session_factory):
    campaign = await _campaign(session_factory)
    async with session_factory() as db:
        db.add(Invite(campaign_id=campaign.id, token="tok-1"))
        await db.commit()

    async with session_factory() as db:
        db.add(Invite(campaign_id=campaign.id, token="tok-1"))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_invite_defaults(session_factory):
    campaign = await _campaign(session_factory)
    async with session_factory() as db:
        invite = Invite(campaign_id=campaign.id, token="tok-abc", email="a@b.c")
        db.add(invite)
        await db.commit()
        await db.refresh(invite)
        assert invite.role == "player"
        assert invite.used_at is None
        assert invite.created_at is not None
