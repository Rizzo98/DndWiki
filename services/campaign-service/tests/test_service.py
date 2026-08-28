"""Service-layer tests: campaigns, members, roles, invites."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import services
from app.models import CampaignMember, Invite
from app.services.campaigns import ensure_utc, slugify

# ------------------------------------------------------------- campaigns


async def test_create_campaign_makes_creator_dm(session_factory):
    dm = uuid.uuid4()
    async with session_factory() as db:
        campaign = await services.create_campaign(db, name="The  Fellowship!!", dm_user_id=dm)
        assert campaign.status == "active"
        assert campaign.dm_user_id == dm
        assert campaign.slug == "the-fellowship"  # slugified from name

        member = await services.get_member(db, campaign.id, dm)
        assert member is not None
        assert member.role == "dm"
        assert member.player_name == "Dungeon Master"  # default when no claim hint
        assert member.character_name == "Dungeon Master"


async def test_create_campaign_uses_dm_player_name_hint(session_factory):
    dm = uuid.uuid4()
    async with session_factory() as db:
        campaign = await services.create_campaign(
            db, name="Tales", dm_user_id=dm, dm_player_name="gandalf"
        )
        member = await services.get_member(db, campaign.id, dm)
        assert member.player_name == "gandalf"
        assert member.character_name == "Dungeon Master"


async def test_create_campaign_explicit_slug(session_factory):
    async with session_factory() as db:
        campaign = await services.create_campaign(
            db, name="Tales", slug="my-custom-slug", dm_user_id=uuid.uuid4()
        )
        assert campaign.slug == "my-custom-slug"


async def test_create_campaign_colliding_slug_gets_suffix(session_factory):
    dm = uuid.uuid4()
    async with session_factory() as db:
        first = await services.create_campaign(db, name="Same Name", dm_user_id=dm)
        second = await services.create_campaign(db, name="Same Name", dm_user_id=dm)
        assert first.slug == "same-name"
        assert second.slug == "same-name-2"


async def test_slugify():
    assert slugify("The Fellowship") == "the-fellowship"
    assert slugify("  A B C  ") == "a-b-c"
    assert slugify("!!!") == "campaign"


async def test_list_campaigns_for_user(session_factory, seed_campaign):
    me, other = uuid.uuid4(), uuid.uuid4()
    await seed_campaign(dm_id=me, slug="mine")
    await seed_campaign(dm_id=other, slug="theirs")
    shared = await seed_campaign(dm_id=other, slug="shared")
    async with session_factory() as db:
        await services.add_member(
            db, shared.id, player_name="Me", character_name="Hero", user_id=me
        )

    async with session_factory() as db:
        rows = await services.list_campaigns_for_user(db, me)
        roles = {c.slug: role for c, role in rows}
        assert roles["mine"] == "dm"
        assert roles["shared"] == "player"
        assert "theirs" not in roles


async def test_get_campaign_or_404(session_factory):
    async with session_factory() as db:
        with pytest.raises(HTTPException) as exc:
            await services.get_campaign_or_404(db, uuid.uuid4())
        assert exc.value.status_code == 404


async def test_update_campaign(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        updated = await services.update_campaign(
            db, campaign.id, name="Renamed", description="A tale"
        )
        assert updated.name == "Renamed"
        assert updated.description == "A tale"
        assert updated.slug == campaign.slug  # untouched fields stay


async def test_update_campaign_slug_unique_excludes_self(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        # re-setting the campaign's own slug must not collide
        updated = await services.update_campaign(db, campaign.id, slug=campaign.slug)
        assert updated.slug == campaign.slug


async def test_archive_and_restore(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        archived = await services.set_campaign_status(db, campaign.id, "archived")
        assert archived.status == "archived"
        restored = await services.set_campaign_status(db, campaign.id, "active")
        assert restored.status == "active"


async def test_archive_twice_409(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        await services.set_campaign_status(db, campaign.id, "archived")
        with pytest.raises(HTTPException) as exc:
            await services.set_campaign_status(db, campaign.id, "archived")
        assert exc.value.status_code == 409


# ------------------------------------------------------------- members


async def _add_player(
    db, campaign_id, *, player_name="Alice", character_name="Rowan", user_id=None
):
    return await services.add_member(
        db,
        campaign_id,
        player_name=player_name,
        character_name=character_name,
        user_id=user_id,
    )


async def test_add_member_unlinked(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        member = await _add_player(db, campaign.id)
        assert member.role == "player"
        assert member.user_id is None
        assert member.player_name == "Alice"
        assert member.character_name == "Rowan"
        assert member.joined_at is not None
        assert member.id is not None


async def test_add_member_linked_to_user(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    player = uuid.uuid4()
    async with session_factory() as db:
        member = await _add_player(db, campaign.id, user_id=player)
        assert member.user_id == player
        # the link is queryable through the user-based lookup
        assert await services.get_member(db, campaign.id, player) is not None


async def test_add_member_multiple_unlinked_ok(session_factory, seed_campaign):
    """Many members without accounts can coexist (NULLs exempt from the unique)."""
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        await _add_player(db, campaign.id, player_name="Bob")
        second = await _add_player(db, campaign.id, player_name="Carol")
        assert second.user_id is None


async def test_add_member_duplicate_user_409(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    player = uuid.uuid4()
    async with session_factory() as db:
        await _add_player(db, campaign.id, user_id=player)
        with pytest.raises(HTTPException) as exc:
            await _add_player(db, campaign.id, user_id=player)
        assert exc.value.status_code == 409
        # linking the DM (already a member) is also a 409
        with pytest.raises(HTTPException) as exc:
            await _add_player(db, campaign.id, user_id=campaign.dm_user_id)
        assert exc.value.status_code == 409


async def test_add_member_unknown_campaign_404(session_factory):
    async with session_factory() as db:
        with pytest.raises(HTTPException) as exc:
            await _add_player(db, uuid.uuid4())
        assert exc.value.status_code == 404


async def test_update_member_rename(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        member = await _add_player(db, campaign.id)
        updated = await services.update_member(
            db,
            campaign.id,
            member.id,
            player_name="Alice the Bold",
            character_name="Rowan II",
        )
        assert updated.player_name == "Alice the Bold"
        assert updated.character_name == "Rowan II"
        assert updated.user_id is None


async def test_update_member_link_user(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    player = uuid.uuid4()
    async with session_factory() as db:
        member = await _add_player(db, campaign.id)
        updated = await services.update_member(db, campaign.id, member.id, user_id=player)
        assert updated.user_id == player
        assert await services.get_member(db, campaign.id, player) is not None


async def test_update_member_unlink_user(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    player = uuid.uuid4()
    async with session_factory() as db:
        member = await _add_player(db, campaign.id, user_id=player)
        updated = await services.update_member(db, campaign.id, member.id, unlink=True)
        assert updated.user_id is None
        assert await services.get_member(db, campaign.id, player) is None


async def test_update_member_link_user_taken_409(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    player = uuid.uuid4()
    async with session_factory() as db:
        first = await _add_player(db, campaign.id, player_name="One")
        second = await _add_player(db, campaign.id, player_name="Two")
        await services.update_member(db, campaign.id, first.id, user_id=player)
        with pytest.raises(HTTPException) as exc:
            await services.update_member(db, campaign.id, second.id, user_id=player)
        assert exc.value.status_code == 409


async def test_update_member_dm_link_immutable(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        dm_member = await services.get_member(db, campaign.id, campaign.dm_user_id)
        with pytest.raises(HTTPException) as exc:
            await services.update_member(db, campaign.id, dm_member.id, user_id=uuid.uuid4())
        assert exc.value.status_code == 400
        with pytest.raises(HTTPException) as exc:
            await services.update_member(db, campaign.id, dm_member.id, unlink=True)
        assert exc.value.status_code == 400


async def test_update_member_link_and_unlink_400(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        member = await _add_player(db, campaign.id)
        with pytest.raises(HTTPException) as exc:
            await services.update_member(
                db, campaign.id, member.id, user_id=uuid.uuid4(), unlink=True
            )
        assert exc.value.status_code == 400


async def test_update_member_unknown_404(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        with pytest.raises(HTTPException) as exc:
            await services.update_member(db, campaign.id, uuid.uuid4(), player_name="X")
        assert exc.value.status_code == 404


async def test_update_member_from_other_campaign_404(session_factory, seed_campaign):
    first = await seed_campaign(dm_id=uuid.uuid4(), slug="first")
    second = await seed_campaign(dm_id=uuid.uuid4(), slug="second")
    async with session_factory() as db:
        member = await _add_player(db, first.id)
        with pytest.raises(HTTPException) as exc:
            await services.update_member(db, second.id, member.id, player_name="X")
        assert exc.value.status_code == 404


async def test_remove_member(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        member = await _add_player(db, campaign.id, user_id=uuid.uuid4())
        await services.remove_member(db, campaign.id, member.id)
        assert await services.get_member_by_id(db, member.id) is None


async def test_remove_member_unknown_404(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        with pytest.raises(HTTPException) as exc:
            await services.remove_member(db, campaign.id, uuid.uuid4())
        assert exc.value.status_code == 404


async def test_remove_dm_forbidden(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        dm_member = await services.get_member(db, campaign.id, campaign.dm_user_id)
        with pytest.raises(HTTPException) as exc:
            await services.remove_member(db, campaign.id, dm_member.id)
        assert exc.value.status_code == 400


async def test_membership_role(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    player = uuid.uuid4()
    async with session_factory() as db:
        await _add_player(db, campaign.id, user_id=player)
        assert await services.membership_role(db, campaign.id, campaign.dm_user_id) == "dm"
        assert await services.membership_role(db, campaign.id, player) == "player"
        # an unlinked member has no user to check
        assert await services.membership_role(db, campaign.id, uuid.uuid4()) is None


async def test_membership_role_unknown_campaign_404(session_factory):
    async with session_factory() as db:
        with pytest.raises(HTTPException) as exc:
            await services.membership_role(db, uuid.uuid4(), uuid.uuid4())
        assert exc.value.status_code == 404


# ------------------------------------------------------------- invites


async def test_create_invite(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        invite = await services.create_invite(db, campaign.id, email="bob@example.com", ttl_days=7)
        assert invite.token and len(invite.token) >= 32
        assert invite.role == "player"
        assert invite.used_at is None
        expected = datetime.now(UTC) + timedelta(days=7)
        assert invite.expires_at is not None
        assert ensure_utc(invite.expires_at) - expected < timedelta(seconds=5)


async def test_accept_invite_joins_campaign(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    bob = uuid.uuid4()
    async with session_factory() as db:
        invite = await services.create_invite(db, campaign.id, email="Bob@Example.com")
        joined = await services.accept_invite(
            db,
            invite.token,
            user_id=bob,
            email="bob@example.com",  # case-insensitive match
            player_name="bob",
        )
        assert joined.id == campaign.id
        member = await services.get_member(db, campaign.id, bob)
        assert member is not None and member.role == "player"
        assert member.player_name == "bob"
        assert member.character_name == ""  # the DM fills the character later
        assert (await db.get(Invite, invite.id)).used_at is not None


async def test_accept_invite_unknown_token_404(session_factory):
    async with session_factory() as db:
        with pytest.raises(HTTPException) as exc:
            await services.accept_invite(db, "no-such-token", user_id=uuid.uuid4(), email=None)
        assert exc.value.status_code == 404


async def test_accept_invite_twice_410(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        invite = await services.create_invite(db, campaign.id)
        await services.accept_invite(db, invite.token, user_id=uuid.uuid4(), email=None)
        with pytest.raises(HTTPException) as exc:
            await services.accept_invite(db, invite.token, user_id=uuid.uuid4(), email=None)
        assert exc.value.status_code == 410


async def test_accept_invite_expired_410(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        invite = Invite(
            campaign_id=campaign.id,
            token="expired-token",
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        db.add(invite)
        await db.commit()
        await db.refresh(invite)
        with pytest.raises(HTTPException) as exc:
            await services.accept_invite(db, invite.token, user_id=uuid.uuid4(), email=None)
        assert exc.value.status_code == 410


async def test_accept_invite_email_mismatch_403(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        invite = await services.create_invite(db, campaign.id, email="bob@example.com")
        with pytest.raises(HTTPException) as exc:
            await services.accept_invite(
                db, invite.token, user_id=uuid.uuid4(), email="mallory@example.com"
            )
        assert exc.value.status_code == 403


async def test_accept_invite_requires_email_claim_403(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        invite = await services.create_invite(db, campaign.id, email="bob@example.com")
        with pytest.raises(HTTPException) as exc:
            await services.accept_invite(db, invite.token, user_id=uuid.uuid4(), email=None)
        assert exc.value.status_code == 403


async def test_accept_invite_when_already_member_no_duplicate(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        invite = await services.create_invite(db, campaign.id)
        await services.accept_invite(db, invite.token, user_id=uuid.uuid4(), email=None)
    async with session_factory() as db:
        invites = (await db.execute(select(Invite))).scalars().all()
        assert len(invites) == 1
        members = (await db.execute(select(CampaignMember))).scalars().all()
        assert len(members) == 2  # dm + one player


async def test_list_invites(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        await services.create_invite(db, campaign.id, email="a@example.com")
        await services.create_invite(db, campaign.id, email="b@example.com")
        invites = await services.list_invites(db, campaign.id)
        assert len(invites) == 2


async def test_revoke_invite(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        invite = await services.create_invite(db, campaign.id)
        await services.revoke_invite(db, campaign.id, invite.id)
        assert await db.get(Invite, invite.id) is None


async def test_revoke_invite_unknown_404(session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=uuid.uuid4())
    async with session_factory() as db:
        with pytest.raises(HTTPException) as exc:
            await services.revoke_invite(db, campaign.id, uuid.uuid4())
        assert exc.value.status_code == 404
