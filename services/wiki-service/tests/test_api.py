"""HTTP API tests with the DB in memory and auth/externals faked.

Identity is controlled through the mutable 'claims' fixture (defaults to
user_id); DM tests switch the caller via claims["sub"] + fake_campaign.roles.
Service-token tests set claims["azp"] to the dnd-services client id.
"""

import uuid

from app.models import WikiPage


async def _seed_page(
    session_factory,
    campaign_id,
    *,
    kind="character",
    title="Aragorn",
    slug=None,
    status="published",
    visibility="public",
    content=None,
):
    async with session_factory() as db:
        page = WikiPage(
            campaign_id=campaign_id,
            kind=kind,
            title=title,
            slug=slug or f"seed-{uuid.uuid4().hex[:8]}",
            status=status,
            visibility=visibility,
            content_json=content or {"body": "text"},
        )
        db.add(page)
        await db.commit()
        await db.refresh(page)
        return page


# ---------------------------------------------------------------- pages


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_create_page_dm_201(client, claims, dm_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    claims["sub"] = str(dm_id)
    fake_campaign.roles[(campaign, dm_id)] = "dm"

    resp = await client.post(
        "/api/wiki/pages",
        json={
            "campaign_id": str(campaign),
            "kind": "character",
            "title": "Aragorn  Son of Arathorn",
            "content_json": {"body": "heir of Isildur"},
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["slug"] == "aragorn-son-of-arathorn"
    assert body["status"] == "draft"
    assert body["visibility"] == "public"
    assert body["campaign_id"] == str(campaign)
    assert body["created_by"] == str(dm_id)
    assert body["content_json"] == {"body": "heir of Isildur"}


async def test_create_page_player_403(client, user_id, fake_campaign):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    resp = await client.post(
        "/api/wiki/pages", json={"campaign_id": str(campaign), "kind": "item", "title": "Ring"}
    )
    assert resp.status_code == 403


async def test_create_page_non_member_403(client, user_id, fake_campaign):
    resp = await client.post(
        "/api/wiki/pages", json={"campaign_id": str(uuid.uuid4()), "kind": "item", "title": "Ring"}
    )
    assert resp.status_code == 403


async def test_create_page_service_token_draft_201(client, claims, fake_publisher, session_factory):
    campaign = uuid.uuid4()
    session = uuid.uuid4()
    claims["sub"] = "dnd-services"
    claims["azp"] = "dnd-services"

    resp = await client.post(
        "/api/wiki/pages",
        json={
            "campaign_id": str(campaign),
            "kind": "character",
            "title": "Gandalf",
            "status": "pending_review",
            "confidence": 0.91,
            "source_session_id": str(session),
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending_review"
    assert body["created_by"] is None
    assert body["confidence"] == 0.91

    assert fake_publisher.events[-1].type == "wiki.draft_ready"
    assert fake_publisher.events[-1].payload["campaign_id"] == str(campaign)
    assert fake_publisher.events[-1].payload["session_id"] == str(session)


async def test_create_page_service_token_cannot_publish_403(client, claims):
    claims["sub"] = "dnd-services"
    claims["azp"] = "dnd-services"
    resp = await client.post(
        "/api/wiki/pages",
        json={
            "campaign_id": str(uuid.uuid4()),
            "kind": "character",
            "title": "Sauron",
            "status": "published",
        },
    )
    assert resp.status_code == 403


async def test_list_pages_player_filtered(client, user_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    await _seed_page(session_factory, campaign, title="Aragorn")
    await _seed_page(session_factory, campaign, title="Secret", visibility="dm_only")
    await _seed_page(session_factory, campaign, title="Draft", status="draft")
    await _seed_page(session_factory, uuid.uuid4(), title="Other campaign")

    resp = await client.get("/api/wiki/pages", params={"campaign_id": str(campaign)})
    assert resp.status_code == 200
    titles = [p["title"] for p in resp.json()]
    assert titles == ["Aragorn"]
    # summary views have no content body
    assert "content_json" not in resp.json()[0]


async def test_list_pages_dm_sees_all(client, claims, dm_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    claims["sub"] = str(dm_id)
    fake_campaign.roles[(campaign, dm_id)] = "dm"
    await _seed_page(session_factory, campaign, title="A")
    await _seed_page(session_factory, campaign, title="Draft", status="draft")

    resp = await client.get("/api/wiki/pages", params={"campaign_id": str(campaign)})
    assert resp.status_code == 200
    assert len(resp.json()) == 2


async def test_list_pages_non_member_403(client, user_id):
    resp = await client.get("/api/wiki/pages", params={"campaign_id": str(uuid.uuid4())})
    assert resp.status_code == 403


async def test_list_pages_campaign_down_503(client, user_id, fake_campaign):
    fake_campaign.unavailable = True
    resp = await client.get("/api/wiki/pages", params={"campaign_id": str(uuid.uuid4())})
    assert resp.status_code == 503


async def test_list_pages_invalid_kind_422(client, user_id, fake_campaign):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    resp = await client.get(
        "/api/wiki/pages", params={"campaign_id": str(campaign), "kind": "bogus"}
    )
    assert resp.status_code == 422


async def test_page_detail_player_403_on_draft(client, user_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    page = await _seed_page(session_factory, campaign, status="draft")
    resp = await client.get(f"/api/wiki/pages/{page.id}")
    assert resp.status_code == 403


async def test_page_detail_player_200_on_published(client, user_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    page = await _seed_page(session_factory, campaign)
    resp = await client.get(f"/api/wiki/pages/{page.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(page.id)
    assert body["content_json"] == {"body": "text"}


async def test_page_detail_dm_sees_draft(client, claims, dm_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    claims["sub"] = str(dm_id)
    fake_campaign.roles[(campaign, dm_id)] = "dm"
    page = await _seed_page(session_factory, campaign, status="pending_review")
    resp = await client.get(f"/api/wiki/pages/{page.id}")
    assert resp.status_code == 200


async def test_page_detail_non_member_403(client, user_id, session_factory):
    page = await _seed_page(session_factory, uuid.uuid4())
    resp = await client.get(f"/api/wiki/pages/{page.id}")
    assert resp.status_code == 403


async def test_page_detail_404(client, user_id):
    resp = await client.get(f"/api/wiki/pages/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_patch_page_dm_200(client, claims, dm_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    claims["sub"] = str(dm_id)
    fake_campaign.roles[(campaign, dm_id)] = "dm"
    page = await _seed_page(session_factory, campaign, status="published")
    resp = await client.patch(
        f"/api/wiki/pages/{page.id}",
        json={"title": "Renamed", "content_json": {"body": "v2"}},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Renamed"
    assert resp.json()["content_json"] == {"body": "v2"}


async def test_patch_page_player_403(client, user_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    page = await _seed_page(session_factory, campaign)
    resp = await client.patch(f"/api/wiki/pages/{page.id}", json={"title": "Nope"})
    assert resp.status_code == 403


async def test_approve_dm_publishes(
    client, claims, dm_id, fake_campaign, fake_publisher, session_factory
):
    campaign = uuid.uuid4()
    claims["sub"] = str(dm_id)
    fake_campaign.roles[(campaign, dm_id)] = "dm"
    page = await _seed_page(session_factory, campaign, status="pending_review")

    resp = await client.post(f"/api/wiki/pages/{page.id}/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "published"

    event = fake_publisher.events[-1]
    assert event.type == "wiki.published"
    assert event.payload["page_id"] == str(page.id)


async def test_approve_player_403(client, user_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    page = await _seed_page(session_factory, campaign, status="pending_review")
    resp = await client.post(f"/api/wiki/pages/{page.id}/approve")
    assert resp.status_code == 403


async def test_approve_already_published_409(client, claims, dm_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    claims["sub"] = str(dm_id)
    fake_campaign.roles[(campaign, dm_id)] = "dm"
    page = await _seed_page(session_factory, campaign, status="published")
    resp = await client.post(f"/api/wiki/pages/{page.id}/approve")
    assert resp.status_code == 409


async def test_archive_dm_emits(
    client, claims, dm_id, fake_campaign, fake_publisher, session_factory
):
    campaign = uuid.uuid4()
    claims["sub"] = str(dm_id)
    fake_campaign.roles[(campaign, dm_id)] = "dm"
    page = await _seed_page(session_factory, campaign)

    resp = await client.post(f"/api/wiki/pages/{page.id}/archive")
    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"
    assert fake_publisher.events[-1].type == "wiki.archived"


async def test_set_visibility_dm(
    client, claims, dm_id, fake_campaign, fake_publisher, session_factory
):
    campaign = uuid.uuid4()
    claims["sub"] = str(dm_id)
    fake_campaign.roles[(campaign, dm_id)] = "dm"
    page = await _seed_page(session_factory, campaign)

    resp = await client.patch(
        f"/api/wiki/pages/{page.id}/visibility", json={"visibility": "hidden"}
    )
    assert resp.status_code == 200
    assert resp.json()["visibility"] == "hidden"
    assert fake_publisher.events[-1].type == "wiki.updated"


async def test_set_visibility_player_403(client, user_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    page = await _seed_page(session_factory, campaign)
    resp = await client.patch(
        f"/api/wiki/pages/{page.id}/visibility", json={"visibility": "hidden"}
    )
    assert resp.status_code == 403


# ------------------------------------------------------------- versions


async def test_versions_member(client, user_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    # a page created through the service always carries an initial version row
    async with session_factory() as db:
        from app import services

        page = await services.create_page(
            db, campaign_id=campaign, kind="character", title="Aragorn", status="published"
        )
    resp = await client.get(f"/api/wiki/pages/{page.id}/versions")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["version_no"] == 1


async def test_versions_draft_hidden_from_player(client, user_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    page = await _seed_page(session_factory, campaign, status="draft")
    resp = await client.get(f"/api/wiki/pages/{page.id}/versions")
    assert resp.status_code == 403


# ------------------------------------------------------------- relations


async def test_relations_dm_crud(client, claims, dm_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    claims["sub"] = str(dm_id)
    fake_campaign.roles[(campaign, dm_id)] = "dm"
    page = await _seed_page(session_factory, campaign, slug="aragorn")
    related = await _seed_page(
        session_factory, campaign, title="Fellowship", slug="fellowship", kind="faction"
    )

    resp = await client.post(
        f"/api/wiki/pages/{page.id}/relations",
        json={"related_page_id": str(related.id), "relation_type": "member_of"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["related_title"] == "Fellowship"
    assert body["related_slug"] == "fellowship"

    resp = await client.get(f"/api/wiki/pages/{page.id}/relations")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = await client.delete(f"/api/wiki/pages/{page.id}/relations/{body['id']}")
    assert resp.status_code == 204

    resp = await client.get(f"/api/wiki/pages/{page.id}/relations")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_relations_player_cannot_create(client, user_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    page = await _seed_page(session_factory, campaign)
    related = await _seed_page(session_factory, campaign, slug="other")
    resp = await client.post(
        f"/api/wiki/pages/{page.id}/relations",
        json={"related_page_id": str(related.id), "relation_type": "member_of"},
    )
    assert resp.status_code == 403


async def test_relations_service_token_can_propose(client, claims, session_factory):
    """content-service tags possible duplicates; no campaign membership needed."""
    campaign = uuid.uuid4()
    claims["sub"] = "dnd-services"
    claims["azp"] = "dnd-services"
    page = await _seed_page(
        session_factory, campaign, title="Fatumastra", slug="fatumastra", kind="location"
    )
    draft = await _seed_page(
        session_factory,
        campaign,
        title="Città di Fatumastra",
        slug="citta-di-fatumastra",
        kind="location",
        status="pending_review",
    )
    resp = await client.post(
        f"/api/wiki/pages/{draft.id}/relations",
        json={"related_page_id": str(page.id), "relation_type": "possible_duplicate"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["related_title"] == "Fatumastra"
    assert body["relation_type"] == "possible_duplicate"


async def test_relations_duplicate_409(client, claims, dm_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    claims["sub"] = str(dm_id)
    fake_campaign.roles[(campaign, dm_id)] = "dm"
    page = await _seed_page(session_factory, campaign)
    related = await _seed_page(session_factory, campaign, slug="other")
    payload = {"related_page_id": str(related.id), "relation_type": "member_of"}
    assert (
        await client.post(f"/api/wiki/pages/{page.id}/relations", json=payload)
    ).status_code == 201
    resp = await client.post(f"/api/wiki/pages/{page.id}/relations", json=payload)
    assert resp.status_code == 409


# ------------------------------------------------------------- debug reset


async def test_delete_all_pages_dm(client, claims, dm_id, fake_campaign, session_factory, fake_publisher):
    campaign = uuid.uuid4()
    claims["sub"] = str(dm_id)
    fake_campaign.roles[(campaign, dm_id)] = "dm"
    published = await _seed_page(session_factory, campaign, slug="published-one")
    draft = await _seed_page(
        session_factory, campaign, title="Draft", slug="draft", status="pending_review"
    )
    other_campaign = await _seed_page(session_factory, uuid.uuid4(), slug="untouched")

    resp = await client.delete(f"/api/wiki/campaigns/{campaign}/pages")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"campaign_id": str(campaign), "deleted": 2}

    # pages of the campaign are gone; other campaigns are untouched
    async with session_factory() as db:
        remaining = list((await db.execute(WikiPage.__table__.select())).mappings())
    ids = {str(row["id"]) for row in remaining}
    assert str(published.id) not in ids
    assert str(draft.id) not in ids
    assert str(other_campaign.id) in ids

    # published pages emitted wiki.archived so search unindexes them
    archived = [e for e in fake_publisher.events if e.type == "wiki.archived"]
    assert [e.payload["page_id"] for e in archived] == [str(published.id)]


async def test_delete_all_pages_player_403(client, claims, user_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    claims["sub"] = str(user_id)
    fake_campaign.roles[(campaign, user_id)] = "player"
    await _seed_page(session_factory, campaign, slug="survivor")
    resp = await client.delete(f"/api/wiki/campaigns/{campaign}/pages")
    assert resp.status_code == 403


async def test_delete_all_pages_dev_role_without_membership(
    client, claims, session_factory, fake_publisher
):
    """The Keycloak 'dev' realm role unlocks the reset outside any campaign."""
    campaign = uuid.uuid4()
    claims["sub"] = str(uuid.uuid4())
    claims["realm_access"] = {"roles": ["dev"]}  # not a member of the campaign
    page = await _seed_page(session_factory, campaign, slug="doomed")

    resp = await client.delete(f"/api/wiki/campaigns/{campaign}/pages")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 1

    archived = [e for e in fake_publisher.events if e.type == "wiki.archived"]
    assert [e.payload["page_id"] for e in archived] == [str(page.id)]


# ------------------------------------------------------------- timeline


async def test_timeline_player_sees_approved_only(client, user_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    async with session_factory() as db:
        from app.models import TimelineEvent

        db.add_all(
            [
                TimelineEvent(campaign_id=campaign, summary="approved", approved=True),
                TimelineEvent(campaign_id=campaign, summary="pending", approved=False),
            ]
        )
        await db.commit()

    resp = await client.get("/api/wiki/timeline", params={"campaign_id": str(campaign)})
    assert resp.status_code == 200
    assert [e["summary"] for e in resp.json()] == ["approved"]


async def test_create_timeline_event_dm(client, claims, dm_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    claims["sub"] = str(dm_id)
    fake_campaign.roles[(campaign, dm_id)] = "dm"
    resp = await client.post(
        "/api/wiki/timeline",
        json={
            "campaign_id": str(campaign),
            "in_world_date": "17 Ches 1492 DR",
            "summary": "The Fellowship departs Rivendell",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["approved"] is True
    assert body["in_world_date"] == "17 Ches 1492 DR"


async def test_create_timeline_event_player_403(client, user_id, fake_campaign):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    resp = await client.post(
        "/api/wiki/timeline",
        json={"campaign_id": str(campaign), "summary": "Nope"},
    )
    assert resp.status_code == 403


async def test_update_timeline_event_dm(client, claims, dm_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    claims["sub"] = str(dm_id)
    fake_campaign.roles[(campaign, dm_id)] = "dm"
    async with session_factory() as db:
        from app.models import TimelineEvent

        event = TimelineEvent(campaign_id=campaign, summary="v1", approved=False)
        db.add(event)
        await db.commit()
        await db.refresh(event)

    resp = await client.patch(
        f"/api/wiki/timeline/{event.id}", json={"summary": "v2", "approved": True}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == "v2"
    assert body["approved"] is True


async def test_update_timeline_event_other_campaign_403(
    client, claims, dm_id, fake_campaign, session_factory
):
    campaign = uuid.uuid4()
    claims["sub"] = str(dm_id)
    fake_campaign.roles[(campaign, dm_id)] = "dm"  # DM of campaign, not of the event's campaign
    async with session_factory() as db:
        from app.models import TimelineEvent

        event = TimelineEvent(campaign_id=uuid.uuid4(), summary="v1")
        db.add(event)
        await db.commit()
        await db.refresh(event)

    resp = await client.patch(f"/api/wiki/timeline/{event.id}", json={"summary": "v2"})
    assert resp.status_code == 403


async def test_create_timeline_event_service_token_pending(
    client, claims, session_factory
):
    """The content pipeline proposes events as pending (approved=False)."""
    campaign = uuid.uuid4()
    async with session_factory() as db:
        from app.models import WikiPage

        page = WikiPage(
            campaign_id=campaign,
            kind="event",
            title="The Siege of Fatumastra",
            slug="siege-of-fatumastra",
            status="pending_review",
        )
        db.add(page)
        await db.commit()
        await db.refresh(page)
    page_id = page.id
    claims["sub"] = "dnd-services"
    claims["azp"] = "dnd-services"
    resp = await client.post(
        "/api/wiki/timeline",
        json={
            "campaign_id": str(campaign),
            "page_id": str(page_id),
            "summary": "The horde breaks against the walls.",
            "approved": True,  # must be ignored for service tokens
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["approved"] is False


async def test_timeline_includes_linked_page_title(
    client, user_id, fake_campaign, session_factory
):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    async with session_factory() as db:
        from app.models import TimelineEvent, WikiPage

        page = WikiPage(
            campaign_id=campaign,
            kind="event",
            title="The Gate of Moria Opens",
            slug="gate-of-moria-opens",
            status="published",
        )
        db.add(page)
        await db.flush()
        db.add(
            TimelineEvent(
                campaign_id=campaign,
                page_id=page.id,
                summary="The party speaks friend and enters.",
                approved=True,
            )
        )
        await db.commit()

    resp = await client.get("/api/wiki/timeline", params={"campaign_id": str(campaign)})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["page_id"] == str(page.id)
    assert body[0]["page_title"] == "The Gate of Moria Opens"
    assert body[0]["page_slug"] == "gate-of-moria-opens"


async def test_delete_campaign_timeline_dm(client, claims, dm_id, fake_campaign, session_factory):
    """Debug reset: the DM wipes every timeline event of the campaign."""
    campaign = uuid.uuid4()
    other_campaign = uuid.uuid4()
    claims["sub"] = str(dm_id)
    fake_campaign.roles[(campaign, dm_id)] = "dm"
    async with session_factory() as db:
        from app.models import TimelineEvent

        db.add_all(
            [
                TimelineEvent(campaign_id=campaign, summary="approved", approved=True),
                TimelineEvent(campaign_id=campaign, summary="pending", approved=False),
                TimelineEvent(campaign_id=other_campaign, summary="untouched"),
            ]
        )
        await db.commit()

    resp = await client.delete(f"/api/wiki/campaigns/{campaign}/timeline")
    assert resp.status_code == 200
    assert resp.json() == {"campaign_id": str(campaign), "deleted": 2}

    async with session_factory() as db:
        remaining = list((await db.execute(TimelineEvent.__table__.select())).mappings())
    ids = {str(row["id"]) for row in remaining}
    assert len(ids) == 1  # only the other campaign's entry survives
    assert all(row["summary"] == "untouched" for row in remaining)


async def test_delete_campaign_timeline_player_403(
    client, claims, user_id, fake_campaign, session_factory
):
    campaign = uuid.uuid4()
    claims["sub"] = str(user_id)
    fake_campaign.roles[(campaign, user_id)] = "player"
    async with session_factory() as db:
        from app.models import TimelineEvent

        db.add(TimelineEvent(campaign_id=campaign, summary="survivor"))
        await db.commit()

    resp = await client.delete(f"/api/wiki/campaigns/{campaign}/timeline")
    assert resp.status_code == 403


async def test_delete_campaign_timeline_dev_role_without_membership(
    client, claims, session_factory
):
    """The Keycloak 'dev' realm role unlocks the reset outside any membership."""
    campaign = uuid.uuid4()
    claims["sub"] = str(uuid.uuid4())
    claims["realm_access"] = {"roles": ["dev"]}  # not a member of the campaign
    async with session_factory() as db:
        from app.models import TimelineEvent

        db.add_all(
            [
                TimelineEvent(campaign_id=campaign, summary="doomed"),
                TimelineEvent(campaign_id=campaign, summary="also doomed"),
            ]
        )
        await db.commit()

    resp = await client.delete(f"/api/wiki/campaigns/{campaign}/timeline")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2

# ------------------------------------------------------------- character image


async def test_page_detail_resolves_image_url(
    client, user_id, fake_campaign, session_factory
):
    """content_json.image_uri ('bucket/key') comes back as a presigned URL."""
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    with_image = await _seed_page(
        session_factory,
        campaign,
        content={"image_uri": "wiki-assets/characters/abc.png"},
    )
    plain = await _seed_page(session_factory, campaign, slug="plain", title="Plain")

    resp = await client.get(f"/api/wiki/pages/{with_image.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["image_url"] == "http://presigned/wiki-assets/characters/abc.png"

    resp = await client.get(f"/api/wiki/pages/{plain.id}")
    assert resp.status_code == 200
    assert resp.json()["image_url"] is None


async def test_upload_image_dm_200(
    client, claims, dm_id, fake_campaign, fake_storage, session_factory
):
    campaign = uuid.uuid4()
    claims["sub"] = str(dm_id)
    fake_campaign.roles[(campaign, dm_id)] = "dm"
    page = await _seed_page(session_factory, campaign, status="pending_review")

    resp = await client.put(
        f"/api/wiki/pages/{page.id}/image",
        files={"file": ("portrait.png", b"\x89PNG\r\n", "image/png")},
    )
    assert resp.status_code == 200
    body = resp.json()

    key = f"characters/{page.id}.png"
    assert body["content_json"]["image_uri"] == f"wiki-assets/{key}"
    assert body["image_url"] == f"http://presigned/wiki-assets/{key}"
    assert fake_storage.uploads == [("wiki-assets", key, "image/png")]


async def test_upload_image_player_403(client, user_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    fake_campaign.roles[(campaign, user_id)] = "player"
    page = await _seed_page(session_factory, campaign)
    resp = await client.put(
        f"/api/wiki/pages/{page.id}/image",
        files={"file": ("p.png", b"data", "image/png")},
    )
    assert resp.status_code == 403


async def test_upload_image_unsupported_mime_415(
    client, claims, dm_id, fake_campaign, session_factory
):
    campaign = uuid.uuid4()
    claims["sub"] = str(dm_id)
    fake_campaign.roles[(campaign, dm_id)] = "dm"
    page = await _seed_page(session_factory, campaign)
    resp = await client.put(
        f"/api/wiki/pages/{page.id}/image",
        files={"file": ("a.gif", b"GIF89a", "image/gif")},
    )
    assert resp.status_code == 415


async def test_upload_image_empty_400(client, claims, dm_id, fake_campaign, session_factory):
    campaign = uuid.uuid4()
    claims["sub"] = str(dm_id)
    fake_campaign.roles[(campaign, dm_id)] = "dm"
    page = await _seed_page(session_factory, campaign)
    resp = await client.put(
        f"/api/wiki/pages/{page.id}/image",
        files={"file": ("empty.png", b"", "image/png")},
    )
    assert resp.status_code == 400
