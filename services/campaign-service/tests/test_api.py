"""HTTP API tests (public + internal) with the DB in memory and auth faked.

Identity is controlled through the mutable 'claims' fixture (defaults to
user_id); DM tests switch the caller to the campaign DM via claims["sub"].
"""

import uuid

from app import deps
from app.main import app
from app.models import CampaignMember


async def _member(session_factory, campaign_id, user_id, role="player", character_description=None):
    async with session_factory() as db:
        member = CampaignMember(
            campaign_id=campaign_id,
            user_id=user_id,
            role=role,
            player_name="Player",
            character_name="Character",
            character_description=character_description,
        )
        db.add(member)
        await db.commit()
        await db.refresh(member)
        return member


async def _invite(session_factory, campaign_id, email=None):
    from app import services as svc

    async with session_factory() as db:
        invite = await svc.create_invite(db, campaign_id, email=email)
        return invite.token, invite.id


# ---------------------------------------------------------------- campaigns


async def test_create_campaign_201(client, user_id):
    resp = await client.post(
        "/api/campaigns",
        json={
            "name": "The  Fellowship!!",
            "description": "Nine walkers",
            "members": [
                {
                    "player_name": "Alice",
                    "character_name": "Rowan",
                    "character_description": "Tall half-elf rogue with silver hair",
                }
            ],
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["slug"] == "the-fellowship"
    assert body["dm_user_id"] == str(user_id)
    assert body["status"] == "active"
    assert body["my_role"] == "dm"
    assert body["description"] == "Nine walkers"


async def test_create_campaign_explicit_slug_and_settings(client, user_id):
    resp = await client.post(
        "/api/campaigns",
        json={
            "name": "Tales",
            "slug": "my-slug",
            "settings": {"default_visibility": "public"},
            "members": [
                {
                    "player_name": "Bob",
                    "character_name": "Cedric",
                    "character_description": "Stocky dwarf cleric with a braided beard",
                }
            ],
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["slug"] == "my-slug"
    assert body["settings"] == {"default_visibility": "public"}

    # the players are on the roster, DM + one player
    roster = await client.get(f"/api/campaigns/{body['id']}/members")
    assert roster.status_code == 200
    players = [m for m in roster.json() if m["role"] == "player"]
    assert len(players) == 1
    assert players[0]["player_name"] == "Bob"
    assert players[0]["character_name"] == "Cedric"
    assert players[0]["character_description"] == "Stocky dwarf cleric with a braided beard"


async def test_create_campaign_invalid_slug_422(client, user_id):
    resp = await client.post(
        "/api/campaigns",
        json={
            "name": "Tales",
            "slug": "Bad Slug!",
            "members": [{"player_name": "A", "character_name": "B", "character_description": "C"}],
        },
    )
    assert resp.status_code == 422


async def test_create_campaign_requires_members_422(client, user_id):
    """The DM must add at least one player while creating the campaign."""
    resp = await client.post("/api/campaigns", json={"name": "Tales"})
    assert resp.status_code == 422
    resp = await client.post("/api/campaigns", json={"name": "Tales", "members": []})
    assert resp.status_code == 422


async def test_create_campaign_requires_member_description_422(client, user_id):
    """Each member must carry a physical description."""
    resp = await client.post(
        "/api/campaigns",
        json={
            "name": "Tales",
            "members": [{"player_name": "A", "character_name": "B"}],
        },
    )
    assert resp.status_code == 422


async def test_create_campaign_member_linked_user(client, user_id):
    """Members added at creation may link an existing platform user."""
    newbie = uuid.uuid4()
    resp = await client.post(
        "/api/campaigns",
        json={
            "name": "Linked",
            "members": [
                {
                    "player_name": "Alice",
                    "character_name": "Rowan",
                    "character_description": "Tall half-elf rogue",
                    "user_id": str(newbie),
                }
            ],
        },
    )
    assert resp.status_code == 201
    campaign_id = resp.json()["id"]
    roster = await client.get(f"/api/campaigns/{campaign_id}/members")
    member = next(m for m in roster.json() if m["role"] == "player")
    assert member["user_id"] == str(newbie)


async def test_create_campaign_duplicate_member_user_409(client, user_id):
    """Linking the DM (or twice) inside the creation payload is a 409."""
    resp = await client.post(
        "/api/campaigns",
        json={
            "name": "Dup",
            "members": [
                {
                    "player_name": "Alice",
                    "character_name": "Rowan",
                    "character_description": "Rogue",
                    "user_id": str(user_id),
                }
            ],
        },
    )
    assert resp.status_code == 409
    resp = await client.post(
        "/api/campaigns",
        json={
            "name": "Dup2",
            "members": [
                {
                    "player_name": "Alice",
                    "character_name": "Rowan",
                    "character_description": "Rogue",
                    "user_id": str(uuid.uuid4()),
                },
                {
                    "player_name": "Bob",
                    "character_name": "Cedric",
                    "character_description": "Cleric",
                    "user_id": str(uuid.uuid4()),
                },
            ],
        },
    )
    assert resp.status_code == 201
    # same user twice in one payload
    same = str(uuid.uuid4())
    resp = await client.post(
        "/api/campaigns",
        json={
            "name": "Dup3",
            "members": [
                {
                    "player_name": "Alice",
                    "character_name": "Rowan",
                    "character_description": "Rogue",
                    "user_id": same,
                },
                {
                    "player_name": "Bob",
                    "character_name": "Cedric",
                    "character_description": "Cleric",
                    "user_id": same,
                },
            ],
        },
    )
    assert resp.status_code == 409


async def test_list_campaigns_only_member_of(client, user_id, seed_campaign):
    await seed_campaign(dm_id=user_id, slug="mine")
    await seed_campaign(dm_id=uuid.uuid4(), slug="not-mine")

    resp = await client.get("/api/campaigns")
    assert resp.status_code == 200
    body = resp.json()
    assert [c["slug"] for c in body] == ["mine"]
    assert body[0]["my_role"] == "dm"


async def test_campaign_detail_member_only(client, user_id, dm_id, session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=dm_id)
    resp = await client.get(f"/api/campaigns/{campaign.id}")
    assert resp.status_code == 403

    await _member(session_factory, campaign.id, user_id)
    resp = await client.get(f"/api/campaigns/{campaign.id}")
    assert resp.status_code == 200
    assert resp.json()["my_role"] == "player"


async def test_campaign_detail_404(client, user_id):
    resp = await client.get(f"/api/campaigns/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_patch_campaign_dm_allowed(client, claims, dm_id, seed_campaign):
    claims["sub"] = str(dm_id)
    campaign = await seed_campaign(dm_id=dm_id)
    resp = await client.patch(
        f"/api/campaigns/{campaign.id}", json={"name": "Renamed", "description": "New"}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"
    assert resp.json()["description"] == "New"


async def test_patch_campaign_player_forbidden(
    client, user_id, dm_id, session_factory, seed_campaign
):
    campaign = await seed_campaign(dm_id=dm_id)
    await _member(session_factory, campaign.id, user_id)
    resp = await client.patch(f"/api/campaigns/{campaign.id}", json={"name": "Nope"})
    assert resp.status_code == 403


async def test_archive_and_restore_dm(client, claims, dm_id, seed_campaign):
    claims["sub"] = str(dm_id)
    campaign = await seed_campaign(dm_id=dm_id)
    resp = await client.post(f"/api/campaigns/{campaign.id}/archive")
    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"

    resp = await client.post(f"/api/campaigns/{campaign.id}/restore")
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


async def test_archive_player_forbidden(client, user_id, dm_id, session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=dm_id)
    await _member(session_factory, campaign.id, user_id)
    resp = await client.post(f"/api/campaigns/{campaign.id}/archive")
    assert resp.status_code == 403


# ---------------------------------------------------------------- members


async def test_list_members_member_only(client, user_id, dm_id, session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=dm_id)
    await _member(session_factory, campaign.id, user_id)
    resp = await client.get(f"/api/campaigns/{campaign.id}/members")
    assert resp.status_code == 200
    roles = {m["user_id"]: m["role"] for m in resp.json()}
    assert roles[str(dm_id)] == "dm"
    assert roles[str(user_id)] == "player"


async def test_add_member_dm(client, claims, dm_id, seed_campaign):
    claims["sub"] = str(dm_id)
    campaign = await seed_campaign(dm_id=dm_id)
    newbie = uuid.uuid4()
    resp = await client.post(
        f"/api/campaigns/{campaign.id}/members",
        json={
            "player_name": "Alice",
            "character_name": "Rowan",
            "character_description": "Tall half-elf rogue with a silver braid",
            "user_id": str(newbie),
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["user_id"] == str(newbie)
    assert body["role"] == "player"
    assert body["player_name"] == "Alice"
    assert body["character_name"] == "Rowan"
    assert body["character_description"] == "Tall half-elf rogue with a silver braid"
    assert body["id"]


async def test_add_member_unlinked_dm(client, claims, dm_id, seed_campaign):
    """A player without a platform account: names + description are required."""
    claims["sub"] = str(dm_id)
    campaign = await seed_campaign(dm_id=dm_id)
    resp = await client.post(
        f"/api/campaigns/{campaign.id}/members",
        json={
            "player_name": "Bob",
            "character_name": "Cedric",
            "character_description": "Stocky dwarf cleric with a braided beard",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["user_id"] is None
    assert body["player_name"] == "Bob"
    assert body["character_description"] == "Stocky dwarf cleric with a braided beard"


async def test_add_member_requires_names_422(client, claims, dm_id, seed_campaign):
    claims["sub"] = str(dm_id)
    campaign = await seed_campaign(dm_id=dm_id)
    resp = await client.post(f"/api/campaigns/{campaign.id}/members", json={"player_name": ""})
    assert resp.status_code == 422
    # character_description is required for every added member
    resp = await client.post(
        f"/api/campaigns/{campaign.id}/members",
        json={"player_name": "Bob", "character_name": "Cedric"},
    )
    assert resp.status_code == 422


async def test_add_member_player_forbidden(client, user_id, dm_id, session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=dm_id)
    await _member(session_factory, campaign.id, user_id)
    resp = await client.post(
        f"/api/campaigns/{campaign.id}/members",
        json={
            "player_name": "X",
            "character_name": "Y",
            "character_description": "Z",
        },
    )
    assert resp.status_code == 403


async def test_add_member_duplicate_409(client, claims, dm_id, seed_campaign):
    claims["sub"] = str(dm_id)
    campaign = await seed_campaign(dm_id=dm_id)
    resp = await client.post(
        f"/api/campaigns/{campaign.id}/members",
        json={
            "player_name": "X",
            "character_name": "Y",
            "character_description": "Z",
            "user_id": str(dm_id),
        },
    )
    assert resp.status_code == 409


async def test_patch_member_dm(client, claims, dm_id, session_factory, seed_campaign):
    claims["sub"] = str(dm_id)
    campaign = await seed_campaign(dm_id=dm_id)
    player = uuid.uuid4()
    member = await _member(session_factory, campaign.id, player)

    # rename + edit description + link a user
    resp = await client.patch(
        f"/api/campaigns/{campaign.id}/members/{member.id}",
        json={
            "player_name": "Alice the Bold",
            "character_name": "Rowan II",
            "character_description": "Now with a cybernetic arm",
            "user_id": str(player),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["player_name"] == "Alice the Bold"
    assert body["character_name"] == "Rowan II"
    assert body["character_description"] == "Now with a cybernetic arm"
    assert body["user_id"] == str(player)

    # unlink the user again
    resp = await client.patch(
        f"/api/campaigns/{campaign.id}/members/{member.id}",
        json={"unlink_user": True},
    )
    assert resp.status_code == 200
    assert resp.json()["user_id"] is None


async def test_patch_member_link_taken_409(client, claims, dm_id, session_factory, seed_campaign):
    claims["sub"] = str(dm_id)
    campaign = await seed_campaign(dm_id=dm_id)
    player = uuid.uuid4()
    await _member(session_factory, campaign.id, player)
    other = await _member(session_factory, campaign.id, uuid.uuid4())
    resp = await client.patch(
        f"/api/campaigns/{campaign.id}/members/{other.id}",
        json={"user_id": str(player)},
    )
    assert resp.status_code == 409


async def test_patch_member_player_forbidden(
    client, user_id, dm_id, session_factory, seed_campaign
):
    campaign = await seed_campaign(dm_id=dm_id)
    await _member(session_factory, campaign.id, user_id)
    resp = await client.patch(
        f"/api/campaigns/{campaign.id}/members/{user_id}",
        json={"player_name": "Nope"},
    )
    assert resp.status_code == 403


async def test_remove_member_dm(client, claims, dm_id, session_factory, seed_campaign):
    claims["sub"] = str(dm_id)
    campaign = await seed_campaign(dm_id=dm_id)
    player = uuid.uuid4()
    member = await _member(session_factory, campaign.id, player)
    resp = await client.delete(f"/api/campaigns/{campaign.id}/members/{member.id}")
    assert resp.status_code == 204


async def test_remove_dm_forbidden(client, claims, dm_id, session_factory, seed_campaign):
    claims["sub"] = str(dm_id)
    campaign = await seed_campaign(dm_id=dm_id)
    async with session_factory() as db:
        from app import services as svc

        dm_member = await svc.get_member(db, campaign.id, dm_id)
    resp = await client.delete(f"/api/campaigns/{campaign.id}/members/{dm_member.id}")
    assert resp.status_code == 400


# ---------------------------------------------------------------- invites


async def test_create_and_list_invites_dm(client, claims, dm_id, seed_campaign):
    claims["sub"] = str(dm_id)
    campaign = await seed_campaign(dm_id=dm_id)
    resp = await client.post(
        f"/api/campaigns/{campaign.id}/invites", json={"email": "bob@example.com"}
    )
    assert resp.status_code == 201
    token = resp.json()["token"]
    assert token

    resp = await client.get(f"/api/campaigns/{campaign.id}/invites")
    assert resp.status_code == 200
    assert [i["token"] for i in resp.json()] == [token]


async def test_create_invite_player_forbidden(
    client, user_id, dm_id, session_factory, seed_campaign
):
    campaign = await seed_campaign(dm_id=dm_id)
    await _member(session_factory, campaign.id, user_id)
    resp = await client.post(
        f"/api/campaigns/{campaign.id}/invites", json={"email": "bob@example.com"}
    )
    assert resp.status_code == 403


async def test_accept_invite_flow(client, claims, user_id, dm_id, session_factory, seed_campaign):
    """DM invites bob; bob (the API caller) accepts and becomes a member."""
    campaign = await seed_campaign(dm_id=dm_id)
    token, _ = await _invite(session_factory, campaign.id, email="bob@example.com")
    claims["email"] = "bob@example.com"

    resp = await client.post(f"/api/campaigns/invites/{token}/accept")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(campaign.id)
    assert body["my_role"] == "player"

    async with session_factory() as db:
        from app import services as svc

        member = await svc.get_member(db, campaign.id, user_id)
        assert member is not None and member.role == "player"


async def test_accept_invite_email_mismatch_403(
    client, claims, dm_id, session_factory, seed_campaign
):
    campaign = await seed_campaign(dm_id=dm_id)
    token, _ = await _invite(session_factory, campaign.id, email="bob@example.com")
    claims["email"] = "mallory@example.com"
    resp = await client.post(f"/api/campaigns/invites/{token}/accept")
    assert resp.status_code == 403


async def test_accept_invite_unknown_404(client, user_id):
    resp = await client.post("/api/campaigns/invites/no-such-token/accept")
    assert resp.status_code == 404


async def test_revoke_invite(client, claims, dm_id, session_factory, seed_campaign):
    claims["sub"] = str(dm_id)
    campaign = await seed_campaign(dm_id=dm_id)
    token, invite_id = await _invite(session_factory, campaign.id, email="bob@example.com")

    resp = await client.delete(f"/api/campaigns/{campaign.id}/invites/{invite_id}")
    assert resp.status_code == 204

    # the link no longer works
    resp = await client.post(f"/api/campaigns/invites/{token}/accept")
    assert resp.status_code == 404


# ---------------------------------------------------------------- internal


async def test_internal_membership_roles(client, dm_id, user_id, session_factory, seed_campaign):
    campaign = await seed_campaign(dm_id=dm_id)
    await _member(session_factory, campaign.id, user_id)

    resp = await client.get(
        "/internal/membership",
        params={"campaign_id": str(campaign.id), "user_id": str(dm_id)},
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "dm"

    resp = await client.get(
        "/internal/membership",
        params={"campaign_id": str(campaign.id), "user_id": str(user_id)},
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "player"


async def test_internal_membership_non_member_null(client, dm_id, seed_campaign):
    campaign = await seed_campaign(dm_id=dm_id)
    resp = await client.get(
        "/internal/membership",
        params={"campaign_id": str(campaign.id), "user_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 200
    assert resp.json()["role"] is None


async def test_internal_campaign_dm(client, dm_id, seed_campaign):
    """content-service resolves the DM user id to tell narrator from players."""
    campaign = await seed_campaign(dm_id=dm_id)
    resp = await client.get(f"/internal/campaigns/{campaign.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(campaign.id)
    assert body["name"] == "The Fellowship"
    assert body["dm_user_id"] == str(dm_id)


async def test_internal_campaign_unknown_404(client):
    resp = await client.get(f"/internal/campaigns/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_internal_membership_unknown_campaign_404(client, dm_id):
    resp = await client.get(
        "/internal/membership",
        params={"campaign_id": str(uuid.uuid4()), "user_id": str(dm_id)},
    )
    assert resp.status_code == 404


async def test_internal_member_lookup(client, dm_id, session_factory, seed_campaign):
    """session-service resolves a member by id for speaker assignment."""
    campaign = await seed_campaign(dm_id=dm_id)
    player = uuid.uuid4()
    member = await _member(session_factory, campaign.id, player)

    resp = await client.get(
        f"/internal/members/{member.id}",
        params={"campaign_id": str(campaign.id)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(member.id)
    assert body["user_id"] == str(player)
    assert body["player_name"] == "Player"
    assert body["character_name"] == "Character"
    assert body["character_description"] is None

    # a member of another campaign is not visible through this campaign
    other = await seed_campaign(dm_id=uuid.uuid4(), slug="other-campaign")
    resp = await client.get(
        f"/internal/members/{member.id}",
        params={"campaign_id": str(other.id)},
    )
    assert resp.status_code == 404
    # unknown member id
    resp = await client.get(
        f"/internal/members/{uuid.uuid4()}",
        params={"campaign_id": str(campaign.id)},
    )
    assert resp.status_code == 404


async def test_internal_requires_service_token(client, dm_id, seed_campaign):
    app.dependency_overrides.pop(deps.require_service, None)
    try:
        campaign = await seed_campaign(dm_id=dm_id)
        resp = await client.get(
            "/internal/membership",
            params={"campaign_id": str(campaign.id), "user_id": str(dm_id)},
        )
        assert resp.status_code == 401
    finally:
        app.dependency_overrides[deps.require_service] = lambda: None


async def test_internal_campaign_members(client, dm_id, user_id, session_factory, seed_campaign):
    """refiner-service fetches the campaign cast (names + descriptions)."""
    campaign = await seed_campaign(dm_id=dm_id)
    await _member(
        session_factory,
        campaign.id,
        user_id,
        character_description="Tall half-elf rogue",
    )

    resp = await client.get(f"/internal/campaigns/{campaign.id}/members")
    assert resp.status_code == 200
    by_role = {m["role"]: m for m in resp.json()}
    assert by_role["dm"]["user_id"] == str(dm_id)
    assert by_role["dm"]["character_description"] is None
    assert by_role["player"]["user_id"] == str(user_id)
    assert by_role["player"]["character_description"] == "Tall half-elf rogue"

    resp = await client.get(f"/internal/campaigns/{uuid.uuid4()}/members")
    assert resp.status_code == 404


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"