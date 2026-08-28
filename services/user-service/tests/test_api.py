"""HTTP API tests (public + internal) with the DB in memory and auth faked.

Identity is controlled through the mutable 'claims' fixture; integrations
(MinIO/Qdrant/SpeechBrain/campaign-service) are the conftest fakes.
"""

import uuid

from sqlalchemy import select

from app import deps
from app.main import app
from app.models import User, VoiceProfile


def _audio_file(data: bytes = b"audio-bytes", mime: str = "audio/m4a"):
    return {"file": ("sample.m4a", data, mime)}


# ---------------------------------------------------------------- users


async def test_me_creates_profile_on_first_access(client, claims, session_factory):
    resp = await client.get("/api/users/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(claims["sub"])
    assert body["display_name"] == "player"  # from preferred_username
    assert body["email"] == "player@test.local"
    assert body["avatar_url"] is None

    async with session_factory() as db:
        user = await db.get(User, uuid.UUID(body["id"]))
        assert user is not None
        assert user.keycloak_sub == str(claims["sub"])


async def test_me_is_stable(client):
    first = await client.get("/api/users/me")
    second = await client.get("/api/users/me")
    assert first.json()["id"] == second.json()["id"]


async def test_me_relinks_existing_email_after_re_registration(
    client, claims, session_factory, seed_user
):
    """Reported bug: delete the Keycloak account, register again with the same
    email (new subject), then GET /api/users/me. The old row is adopted and
    re-linked instead of failing on the email unique constraint."""
    old_sub = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert claims["sub"] != old_sub
    await seed_user(
        id=uuid.UUID(old_sub),
        keycloak_sub=old_sub,
        email="player@test.local",
        display_name="Andre",
    )

    resp = await client.get("/api/users/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == old_sub  # stable id preserved
    assert body["display_name"] == "Andre"
    assert body["email"] == "player@test.local"

    async with session_factory() as db:
        rows = (await db.execute(select(User))).scalars().all()
        assert len(rows) == 1
        assert rows[0].keycloak_sub == str(claims["sub"])


async def test_patch_me_updates_display_name(client):
    resp = await client.patch("/api/users/me", json={"display_name": "Gandalf"})
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Gandalf"


async def test_patch_me_empty_display_name_422(client):
    resp = await client.patch("/api/users/me", json={"display_name": ""})
    assert resp.status_code == 422


async def test_get_user_public_profile_hides_email(client, seed_user):
    other = await seed_user()
    resp = await client.get(f"/api/users/{other.id}")
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Seeded"
    assert resp.json()["email"] is None  # email is private to /me


async def test_search_users_by_display_name(client, seed_user):
    await seed_user(
        keycloak_sub="33333333-3333-3333-3333-333333333331",
        display_name="Gandalf the Grey",
        email="gandalf@test.local",
    )
    await seed_user(
        keycloak_sub="33333333-3333-3333-3333-333333333332",
        display_name="Frodo Baggins",
        email="frodo@test.local",
    )

    resp = await client.get("/api/users/search", params={"q": "gandalf"})
    assert resp.status_code == 200
    body = resp.json()
    assert [u["display_name"] for u in body] == ["Gandalf the Grey"]
    assert body[0]["email"] is None  # email stays private in search results


async def test_search_users_by_email_and_limit(client, seed_user):
    await seed_user(
        keycloak_sub="33333333-3333-3333-3333-333333333333",
        display_name="Alice",
        email="alice@test.local",
    )
    await seed_user(
        keycloak_sub="33333333-3333-3333-3333-333333333334",
        display_name="Bob",
        email="bob@test.local",
    )

    resp = await client.get("/api/users/search", params={"q": "bob@test"})
    assert resp.status_code == 200
    assert [u["display_name"] for u in resp.json()] == ["Bob"]

    resp = await client.get("/api/users/search", params={"q": "a", "limit": 1})
    assert resp.status_code == 200
    assert len(resp.json()) == 1


async def test_search_users_empty_query_422(client):
    resp = await client.get("/api/users/search", params={"q": ""})
    assert resp.status_code == 422


async def test_search_users_route_not_captured_by_user_id(client):
    """The literal path /api/users/search must not hit /{user_id}."""
    resp = await client.get("/api/users/search", params={"q": "zzz"})
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_user_404(client):
    resp = await client.get(f"/api/users/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_avatar_upload(client, fakes, session_factory):
    resp = await client.put(
        "/api/users/me/avatar", files={"file": ("me.png", b"png-bytes", "image/png")}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["avatar_uri"] == f"wiki-assets/avatars/{body['id']}.png"
    assert body["avatar_url"] == f"http://presigned/wiki-assets/avatars/{body['id']}.png"

    assert len(fakes.storage.uploads) == 1
    assert fakes.storage.uploads[0]["bucket"] == "wiki-assets"

    async with session_factory() as db:
        user = await db.get(User, uuid.UUID(body["id"]))
        assert user.avatar_uri == f"wiki-assets/avatars/{body['id']}.png"


async def test_avatar_unsupported_mime_415(client):
    resp = await client.put("/api/users/me/avatar", files={"file": ("a.gif", b"gif", "image/gif")})
    assert resp.status_code == 415


async def test_avatar_empty_400(client):
    resp = await client.put("/api/users/me/avatar", files={"file": ("a.png", b"", "image/png")})
    assert resp.status_code == 400


# ---------------------------------------------------------------- voice


async def test_enroll_201(client, claims, fakes, session_factory):
    campaign_id = uuid.uuid4()
    fakes.membership[(campaign_id, uuid.UUID(claims["sub"]))] = "player"

    resp = await client.post(
        "/api/voice/enroll",
        data={"campaign_id": str(campaign_id)},
        files=_audio_file(),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert uuid.UUID(body["id"])
    assert uuid.UUID(body["user_id"]) == uuid.UUID(claims["sub"])
    assert uuid.UUID(body["campaign_id"]) == campaign_id
    assert body["sample_uri"].startswith("voice-samples/")
    assert body["sample_url"].startswith("http://presigned/voice-samples/")

    # vector in (fake) Qdrant with the documented payload
    assert len(fakes.voiceprints.points) == 1
    point_id = next(iter(fakes.voiceprints.points))
    _vector, payload = fakes.voiceprints.points[point_id]
    assert payload["campaign_id"] == str(campaign_id)

    # voice_profiles row + lazy-provisioned user row
    async with session_factory() as db:
        profiles = (await db.execute(select(VoiceProfile))).scalars().all()
        assert len(profiles) == 1
        users = (await db.execute(select(User))).scalars().all()
        assert len(users) == 1


async def test_enroll_not_member_403(client, fakes):
    resp = await client.post(
        "/api/voice/enroll",
        data={"campaign_id": str(uuid.uuid4())},
        files=_audio_file(),
    )
    assert resp.status_code == 403


async def test_enroll_unsupported_mime_415(client, claims, fakes):
    campaign_id = uuid.uuid4()
    fakes.membership[(campaign_id, uuid.UUID(claims["sub"]))] = "player"
    resp = await client.post(
        "/api/voice/enroll",
        data={"campaign_id": str(campaign_id)},
        files=_audio_file(mime="text/plain"),
    )
    assert resp.status_code == 415


async def test_list_voice_profiles_own_only(client, claims, fakes, session_factory):
    campaign_id = uuid.uuid4()
    fakes.membership[(campaign_id, uuid.UUID(claims["sub"]))] = "player"
    resp = await client.post(
        "/api/voice/enroll",
        data={"campaign_id": str(campaign_id)},
        files=_audio_file(),
    )
    assert resp.status_code == 201

    # seed someone else's voiceprint in the same campaign
    async with session_factory() as db:
        other = User(keycloak_sub="other-sub", display_name="Other")
        db.add(other)
        await db.flush()
        db.add(
            VoiceProfile(
                user_id=other.id,
                campaign_id=campaign_id,
                qdrant_point="other-point",
                sample_uri="voice-samples/other",
            )
        )
        await db.commit()

    resp = await client.get(f"/api/voice/profiles?campaign_id={campaign_id}")
    assert resp.status_code == 200
    profiles = resp.json()
    assert len(profiles) == 1
    assert uuid.UUID(profiles[0]["user_id"]) == uuid.UUID(claims["sub"])


async def test_delete_voice_profile(client, claims, fakes, session_factory):
    campaign_id = uuid.uuid4()
    fakes.membership[(campaign_id, uuid.UUID(claims["sub"]))] = "player"
    resp = await client.post(
        "/api/voice/enroll",
        data={"campaign_id": str(campaign_id)},
        files=_audio_file(),
    )
    profile_id = resp.json()["id"]
    assert len(fakes.voiceprints.points) == 1

    resp = await client.delete(f"/api/voice/profiles/{profile_id}")
    assert resp.status_code == 204
    assert fakes.voiceprints.points == {}

    async with session_factory() as db:
        assert await db.get(VoiceProfile, uuid.UUID(profile_id)) is None


async def test_delete_voice_profile_other_user_404(client, fakes, session_factory):
    async with session_factory() as db:
        other = User(keycloak_sub="other-sub", display_name="Other")
        db.add(other)
        await db.flush()
        db.add(
            VoiceProfile(
                user_id=other.id,
                campaign_id=uuid.uuid4(),
                qdrant_point="other-point",
                sample_uri="voice-samples/other",
            )
        )
        await db.commit()
        profile_id = (await db.execute(select(VoiceProfile))).scalars().one().id

    resp = await client.delete(f"/api/voice/profiles/{profile_id}")
    assert resp.status_code == 404


async def test_list_voice_profiles_resolves_current_sub_after_relink(
    client, claims, fakes, seed_user
):
    """Reported bug: enroll after re-registration stores the profile under the
    stable users row id, but the profile page lists by the JWT sub - after a
    re-link the two differ and "my voiceprints" came back empty."""
    old_sub = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    await seed_user(
        id=uuid.UUID(old_sub),
        keycloak_sub=old_sub,
        email="player@test.local",
        display_name="Andre",
    )
    await client.get("/api/users/me")  # re-links the row to claims["sub"]

    campaign_id = uuid.uuid4()
    fakes.membership[(campaign_id, uuid.UUID(claims["sub"]))] = "player"
    enrolled = await client.post(
        "/api/voice/enroll",
        data={"campaign_id": str(campaign_id)},
        files=_audio_file(),
    )
    assert enrolled.status_code == 201
    enrolled_body = enrolled.json()
    # the profile is keyed by the stable row id, not the current JWT sub
    assert uuid.UUID(enrolled_body["user_id"]) == uuid.UUID(old_sub)

    resp = await client.get(f"/api/voice/profiles?campaign_id={campaign_id}")
    assert resp.status_code == 200
    profiles = resp.json()
    assert [p["id"] for p in profiles] == [enrolled_body["id"]]
    assert uuid.UUID(profiles[0]["user_id"]) == uuid.UUID(old_sub)


async def test_delete_voice_profile_resolves_current_sub_after_relink(
    client, claims, fakes, seed_user
):
    """Same re-link scenario: deleting an own voiceprint must not 404."""
    old_sub = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    await seed_user(
        id=uuid.UUID(old_sub),
        keycloak_sub=old_sub,
        email="player@test.local",
        display_name="Andre",
    )
    await client.get("/api/users/me")  # re-links the row to claims["sub"]

    campaign_id = uuid.uuid4()
    fakes.membership[(campaign_id, uuid.UUID(claims["sub"]))] = "player"
    enrolled = await client.post(
        "/api/voice/enroll",
        data={"campaign_id": str(campaign_id)},
        files=_audio_file(),
    )
    assert enrolled.status_code == 201
    profile_id = enrolled.json()["id"]
    assert len(fakes.voiceprints.points) == 1

    resp = await client.delete(f"/api/voice/profiles/{profile_id}")
    assert resp.status_code == 204
    assert fakes.voiceprints.points == {}


# ---------------------------------------------------------------- internal


async def test_internal_users(client, seed_user):
    user = await seed_user()
    resp = await client.get("/internal/users", params=[("ids", str(user.id))])
    assert resp.status_code == 200
    body = resp.json()["users"]
    assert body[str(user.id)]["display_name"] == "Seeded"
    assert body[str(user.id)]["email"] == "seeded@test.local"


async def test_internal_users_missing_ids_skipped(client, seed_user):
    user = await seed_user()
    resp = await client.get(
        "/internal/users", params=[("ids", str(user.id)), ("ids", str(uuid.uuid4()))]
    )
    assert set(resp.json()["users"].keys()) == {str(user.id)}


async def test_internal_users_resolves_current_sub_after_relink(
    client, claims, session_factory, seed_user
):
    """After a re-registration the row id is the old sub and the current sub
    moved; /internal/users must resolve both identifiers."""
    old_sub = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    await seed_user(
        id=uuid.UUID(old_sub),
        keycloak_sub=old_sub,
        email="player@test.local",
        display_name="Andre",
    )
    await client.get("/api/users/me")  # re-links the row to claims["sub"]

    by_new_sub = await client.get("/internal/users", params=[("ids", str(claims["sub"]))])
    assert by_new_sub.status_code == 200
    assert by_new_sub.json()["users"][str(claims["sub"])]["display_name"] == "Andre"

    by_old_id = await client.get("/internal/users", params=[("ids", old_sub)])
    assert by_old_id.json()["users"][old_sub]["display_name"] == "Andre"


async def test_internal_voice_profiles(client, claims, fakes):
    campaign_id = uuid.uuid4()
    fakes.membership[(campaign_id, uuid.UUID(claims["sub"]))] = "player"
    await client.post(
        "/api/voice/enroll",
        data={"campaign_id": str(campaign_id)},
        files=_audio_file(),
    )
    resp = await client.get("/internal/voice-profiles", params={"campaign_id": str(campaign_id)})
    assert resp.status_code == 200
    profiles = resp.json()
    assert len(profiles) == 1
    assert uuid.UUID(profiles[0]["user_id"]) == uuid.UUID(claims["sub"])
    assert profiles[0]["embedding_version"] == 1


async def test_internal_requires_service_token(client, fakes, seed_user):
    user = await seed_user()
    app.dependency_overrides.pop(deps.require_service, None)
    try:
        resp = await client.get("/internal/users", params=[("ids", str(user.id))])
        assert resp.status_code == 401
    finally:
        app.dependency_overrides[deps.require_service] = lambda: None


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
