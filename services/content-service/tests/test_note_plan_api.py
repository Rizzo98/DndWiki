"""Tests for the DM toolkit's plan tool: notes CRUD, generation, review, apply.

The API is the only place the DM's notes are reachable, so these tests are
mostly about what it REFUSES: a note of another campaign, a generation over
notes that do not exist, an edit of a set that is no longer a draft, and every
route for a caller who is not the campaign's DM.
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import httpx
import pytest
from dnd_common import auth as dnd_auth
from dnd_common import db as dnd_db
from dnd_common.events import Event
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import notes as notes_api
from app.clients.campaign_service import MembershipUnavailable
from app.main import app
from app.services import note_plans, notes

CAMPAIGN_ID = "22222222-2222-2222-2222-222222222222"
OTHER_CAMPAIGN_ID = "33333333-3333-3333-3333-333333333333"
DM_ID = "829e495c-a552-4f95-81ce-7efa62d0ef53"

CHANGE_SET = {
    "changes": [
        {
            "id": "c1",
            "action": "create",
            "kind": "character",
            "title": "Kaelor",
            "page_id": None,
            "before": None,
            "after": {
                "title": "Kaelor",
                "content_json": {"physical_look": "Grey-eyed.", "facts": ["Grey cloak."]},
                "visibility": "public",
                "confidence": None,
            },
            "timeline": None,
            "dropped": False,
        },
        {
            "id": "c2",
            "action": "update",
            "kind": "location",
            "title": "Bree",
            "page_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "before": {"title": "Bree", "content_json": {"summary": "A town."}},
            "after": {
                "title": "Bree",
                "content_json": {"summary": "A town.\n\nIt has a new gate."},
                "visibility": "public",
                "confidence": None,
            },
            "timeline": None,
            "dropped": False,
        },
    ],
    "relations": [
        {
            "id": "r1",
            "from_title": "Kaelor",
            "to_title": "Bree",
            "to_page_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "relation_type": "appears_in",
            "dropped": False,
        }
    ],
    "skipped": [{"title": "the ambush idea", "reason": "not a page yet"}],
}


class FakeCampaignClient:
    async def assert_dm(self, campaign_id, user_id) -> None:
        pass


class NotDmCampaignClient(FakeCampaignClient):
    async def assert_dm(self, campaign_id, user_id) -> None:
        raise PermissionError("dm role required")


class DownCampaignClient(FakeCampaignClient):
    async def assert_dm(self, campaign_id, user_id) -> None:
        raise MembershipUnavailable("campaign-service unreachable")


class FakePublisher:
    def __init__(self):
        self.events: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.events.append(event)


@pytest.fixture
async def client(session_factory) -> AsyncIterator[httpx.AsyncClient]:
    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    claims = {"sub": DM_ID}
    publisher = FakePublisher()
    app.dependency_overrides[dnd_db.get_session] = _override_session
    app.dependency_overrides[dnd_auth.current_user] = lambda: claims
    app.dependency_overrides[notes_api.get_publisher] = lambda: publisher
    notes_api._campaign_client = FakeCampaignClient()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c._publisher = publisher  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()


def notes_url(campaign_id: str = CAMPAIGN_ID) -> str:
    return f"/api/content/campaigns/{campaign_id}/notes"


def plan_url(campaign_id: str = CAMPAIGN_ID) -> str:
    return f"/api/content/campaigns/{campaign_id}/plan"


async def _seed_note(session_factory, title="The Ash Company", body="They hold the north road."):
    async with session_factory() as db:
        return await notes.create_note(
            db, UUID(CAMPAIGN_ID), title=title, body=body, user_id=UUID(DM_ID)
        )


async def _seed_plan(session_factory, *, change_set=None, status=None):
    async with session_factory() as db:
        row = await note_plans.save_generation(
            db, UUID(CAMPAIGN_ID), change_set=change_set or CHANGE_SET, language="en"
        )
        if status:
            row.status = status
            await db.commit()
            await db.refresh(row)
        return row


# ------------------------------------------------------------------- the notes


async def test_a_campaign_starts_with_no_notes(client):
    resp = await client.get(notes_url())
    assert resp.status_code == 200
    assert resp.json() == {"notes": []}


async def test_a_dm_can_write_rename_settle_and_delete_a_note(client):
    created = await client.post(notes_url(), json={"title": "Ash Company"})
    assert created.status_code == 201
    note = created.json()["note"]
    assert note["title"] == "Ash Company"
    assert note["status"] == "draft"
    assert note["body"] == ""
    note_id = note["id"]

    saved = await client.put(
        notes_url() + "/" + note_id,
        json={"title": "The Ash Company", "body": "They hold the north road.", "status": "ready"},
    )
    assert saved.status_code == 200
    assert saved.json()["note"]["body"] == "They hold the north road."
    assert saved.json()["note"]["status"] == "ready"

    listed = await client.get(notes_url())
    assert [n["title"] for n in listed.json()["notes"]] == ["The Ash Company"]

    deleted = await client.delete(notes_url() + "/" + note_id)
    assert deleted.status_code == 204
    assert (await client.get(notes_url())).json() == {"notes": []}


async def test_a_blank_title_does_not_rename_the_note_to_nothing(client):
    note = (await client.post(notes_url(), json={"title": ""})).json()["note"]
    assert note["title"] == "Untitled note"
    # clearing the field while retyping must not rename it behind the DM's back
    saved = await client.put(notes_url() + "/" + note["id"], json={"title": "   "})
    assert saved.json()["note"]["title"] == "Untitled note"


async def test_a_partial_save_leaves_the_fields_it_omits_alone(client):
    note = (
        await client.post(notes_url(), json={"title": "Kaelor", "body": "Grey-eyed."})
    ).json()["note"]
    saved = await client.put(notes_url() + "/" + note["id"], json={"status": "ready"})
    body = saved.json()["note"]
    assert body["title"] == "Kaelor"
    assert body["body"] == "Grey-eyed."
    assert body["status"] == "ready"


async def test_an_unknown_status_is_refused(client):
    note = (await client.post(notes_url(), json={"title": "Kaelor"})).json()["note"]
    resp = await client.put(notes_url() + "/" + note["id"], json={"status": "finished"})
    assert resp.status_code == 422


async def test_a_note_of_another_campaign_is_not_reachable(client, session_factory):
    async with session_factory() as db:
        foreign = await notes.create_note(db, UUID(OTHER_CAMPAIGN_ID), title="Theirs")
    note_id = str(foreign.id)
    assert (await client.get(notes_url() + "/" + note_id)).status_code == 404
    assert (await client.put(notes_url() + "/" + note_id, json={"title": "Mine"})).status_code == 404
    assert (await client.delete(notes_url() + "/" + note_id)).status_code == 404
    # ...and deleting it through the other campaign would have been a 404 too
    assert (await client.get(notes_url(OTHER_CAMPAIGN_ID) + "/" + note_id)).status_code == 200


async def test_deleting_a_note_that_is_not_there_is_a_404(client):
    assert (await client.delete(notes_url() + "/" + str(uuid4()))).status_code == 404


# -------------------------------------------------------------- authorization


async def test_a_player_cannot_read_the_notes(client, session_factory):
    notes_api._campaign_client = NotDmCampaignClient()
    resp = await client.get(notes_url())
    assert resp.status_code == 403


async def test_a_player_cannot_write_a_note_or_generate_a_plan(client, session_factory):
    notes_api._campaign_client = NotDmCampaignClient()
    assert (await client.post(notes_url(), json={"title": "sneaky"})).status_code == 403
    assert (await client.get(plan_url())).status_code == 403
    assert (
        await client.post(plan_url() + "/generate", json={"note_ids": [str(uuid4())]})
    ).status_code == 403
    assert (await client.post(plan_url() + "/confirm")).status_code == 403


async def test_an_unreachable_campaign_service_is_503_not_403(client):
    """An outage must not read as "you are not allowed"."""
    notes_api._campaign_client = DownCampaignClient()
    assert (await client.get(notes_url())).status_code == 503


# ------------------------------------------------------------------ generation


async def test_generating_opens_the_run_and_asks_the_worker_to_read_the_notes(
    client, session_factory
):
    note = await _seed_note(session_factory)
    resp = await client.post(plan_url() + "/generate", json={"note_ids": [str(note.id)]})
    assert resp.status_code == 202
    plan = resp.json()["plan"]
    # the row IS the job: it says so before the worker has done anything
    assert plan["status"] == "generating"
    assert plan["note_ids"] == [str(note.id)]
    assert plan["changes"] == []

    events = client._publisher.events  # type: ignore[attr-defined]
    assert [e.type for e in events] == ["note.plan.requested"]
    assert events[0].payload["campaign_id"] == CAMPAIGN_ID
    assert events[0].payload["note_ids"] == [str(note.id)]


async def test_generating_from_notes_that_do_not_exist_is_refused(client):
    """Silently dropping them would propose from fewer notes than were picked."""
    resp = await client.post(plan_url() + "/generate", json={"note_ids": [str(uuid4())]})
    assert resp.status_code == 404
    assert "unknown note" in resp.json()["detail"]


async def test_generating_from_another_campaigns_note_is_refused(client, session_factory):
    async with session_factory() as db:
        foreign = await notes.create_note(db, UUID(OTHER_CAMPAIGN_ID), title="Theirs")
    resp = await client.post(plan_url() + "/generate", json={"note_ids": [str(foreign.id)]})
    assert resp.status_code == 404


async def test_generating_needs_at_least_one_note(client):
    resp = await client.post(plan_url() + "/generate", json={"note_ids": []})
    assert resp.status_code == 422


async def test_a_duplicated_selection_is_read_once(client, session_factory):
    note = await _seed_note(session_factory)
    resp = await client.post(
        plan_url() + "/generate", json={"note_ids": [str(note.id), str(note.id)]}
    )
    assert resp.json()["plan"]["note_ids"] == [str(note.id)]


async def test_regenerating_replaces_the_previous_proposal(client, session_factory):
    note = await _seed_note(session_factory)
    await _seed_plan(session_factory)
    resp = await client.post(plan_url() + "/generate", json={"note_ids": [str(note.id)]})
    plan = resp.json()["plan"]
    # the set under review is the one being generated, never the stale one
    assert plan["status"] == "generating"
    assert plan["changes"] == []
    assert plan["relations"] == []


# ------------------------------------------------------------------- the plan


async def test_the_plan_is_null_before_a_generation(client):
    resp = await client.get(plan_url())
    assert resp.status_code == 200
    assert resp.json()["plan"] is None


async def test_the_plan_is_served_with_its_counts_and_diff_bases(client, session_factory):
    await _seed_plan(session_factory)
    plan = (await client.get(plan_url())).json()["plan"]
    assert plan["status"] == "draft"
    assert plan["language"] == "en"
    assert [c["title"] for c in plan["changes"]] == ["Kaelor", "Bree"]
    # an update carries what the page says today, so the UI can diff it
    assert plan["changes"][1]["before"]["content_json"] == {"summary": "A town."}
    assert plan["skipped"][0]["title"] == "the ambush idea"
    assert plan["counts"] == {
        "create": 1, "update": 1, "pages": 2, "events": 0, "relations": 1, "dropped": 0,
    }


async def test_the_dm_can_edit_and_drop_single_changes(client, session_factory):
    await _seed_plan(session_factory)
    resp = await client.put(
        plan_url(),
        json={
            "changes": [
                {"id": "c1", "title": "Kaelor the Grey", "after": {"content_json": {"summary": "Edited."}}},
                {"id": "c2", "dropped": True},
            ],
            "relations": [{"id": "r1", "dropped": True}],
        },
    )
    assert resp.status_code == 200
    plan = resp.json()["plan"]
    assert plan["changes"][0]["title"] == "Kaelor the Grey"
    assert plan["changes"][0]["after"]["title"] == "Kaelor the Grey"
    assert plan["changes"][0]["after"]["content_json"] == {"summary": "Edited."}
    assert plan["changes"][1]["dropped"] is True
    assert plan["relations"][0]["dropped"] is True
    assert plan["counts"]["pages"] == 1
    assert plan["counts"]["dropped"] == 2


async def test_an_edit_cannot_retarget_a_change_at_another_page(client, session_factory):
    """Identity is pipeline-owned: the DM edits WHAT a change says, not its target."""
    await _seed_plan(session_factory)
    resp = await client.put(
        plan_url(),
        json=[{"id": "c2"}],
    )
    assert resp.status_code == 422  # a list is not the request body

    resp = await client.put(
        plan_url(),
        json={"changes": [{"id": "c2", "after": {"visibility": "nonsense"}}]},
    )
    assert resp.status_code == 422

    # a payload that TRIES to retarget the change is ignored, not obeyed: the
    # stored action/kind/page_id/before survive the edit
    resp = await client.put(
        plan_url(),
        json={
            "changes": [
                {
                    "id": "c2",
                    "action": "create",
                    "kind": "character",
                    "page_id": "99999999-9999-9999-9999-999999999999",
                    "before": None,
                }
            ]
        },
    )
    assert resp.status_code == 200
    change = next(c for c in resp.json()["plan"]["changes"] if c["id"] == "c2")
    assert change["action"] == "update"
    assert change["kind"] == "location"
    assert change["page_id"] == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    assert change["before"] == {"title": "Bree", "content_json": {"summary": "A town."}}


async def test_editing_a_change_that_is_not_in_the_set_is_refused(client, session_factory):
    await _seed_plan(session_factory)
    resp = await client.put(plan_url(), json={"changes": [{"id": "c99", "title": "x"}]})
    assert resp.status_code == 422
    assert "unknown change id" in resp.json()["detail"]


async def test_a_set_that_is_not_a_draft_cannot_be_edited(client, session_factory):
    await _seed_plan(session_factory, status="applied")
    resp = await client.put(plan_url(), json={"changes": [{"id": "c1", "title": "x"}]})
    assert resp.status_code == 409


async def test_editing_when_there_is_no_proposal_is_refused(client):
    resp = await client.put(plan_url(), json={"changes": [{"id": "c1", "title": "x"}]})
    assert resp.status_code == 409


# ------------------------------------------------------------------ confirming


async def test_confirming_stamps_the_dm_and_asks_the_worker_to_write(client, session_factory):
    await _seed_plan(session_factory)
    resp = await client.post(plan_url() + "/confirm")
    assert resp.status_code == 202
    body = resp.json()
    assert body["queued"] is True
    assert body["plan"]["confirmed_by"] == DM_ID
    # 'applying' is already set in the ANSWER, not left to the worker: the plan
    # page polls this row, and it only polls while a run is in flight. Were the
    # status still 'draft' here, the page would stop polling at the exact moment
    # the DM had something to wait for.
    assert body["plan"]["status"] == "applying"

    events = client._publisher.events  # type: ignore[attr-defined]
    assert [e.type for e in events] == ["note.plan.confirmed"]
    assert events[0].payload["campaign_id"] == CAMPAIGN_ID
    assert events[0].payload["pages"] == 2


async def test_confirming_without_a_proposal_is_refused(client):
    resp = await client.post(plan_url() + "/confirm")
    assert resp.status_code == 409


async def test_a_set_that_is_not_a_draft_cannot_be_confirmed(client, session_factory):
    await _seed_plan(session_factory, status="generating")
    resp = await client.post(plan_url() + "/confirm")
    assert resp.status_code == 409
    assert "generating" in resp.json()["detail"]


async def test_an_applied_set_cannot_be_confirmed_again(client, session_factory):
    await _seed_plan(session_factory, status="applied")
    assert (await client.post(plan_url() + "/confirm")).status_code == 409


# ------------------------------------------------------------------ discarding


async def test_the_dm_can_throw_a_proposal_away(client, session_factory):
    await _seed_plan(session_factory)
    assert (await client.delete(plan_url())).status_code == 204
    assert (await client.get(plan_url())).json()["plan"] is None


async def test_an_applied_proposal_is_not_thrown_away(client, session_factory):
    """The pages exist: the way back is to edit or archive them, not to forget."""
    await _seed_plan(session_factory, status="applied")
    resp = await client.delete(plan_url())
    assert resp.status_code == 409


async def test_a_plan_being_written_is_not_thrown_away(client, session_factory):
    """Deleting the row under the worker would fail a run that in fact succeeded."""
    await _seed_plan(session_factory, status="applying")
    resp = await client.delete(plan_url())
    assert resp.status_code == 409
    assert "'applying'" in resp.json()["detail"]
    # ...and the proposal is still there for the worker to report against
    assert (await client.get(plan_url())).json()["plan"]["status"] == "applying"


async def test_a_plan_still_reading_the_notes_is_not_thrown_away(client, session_factory):
    await _seed_plan(session_factory, status="generating")
    resp = await client.delete(plan_url())
    assert resp.status_code == 409
    assert "'generating'" in resp.json()["detail"]


async def test_a_failed_proposal_can_be_discarded(client, session_factory):
    """A run the DM gave up on is theirs to clear."""
    await _seed_plan(session_factory, status="failed")
    assert (await client.delete(plan_url())).status_code == 204


async def test_discarding_nothing_is_a_404(client):
    assert (await client.delete(plan_url())).status_code == 404
