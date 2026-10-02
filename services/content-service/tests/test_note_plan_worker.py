"""Tests for the DM toolkit's plan worker: reading notes, writing the wiki.

Two phases, and the row is the job: phase 1 (process_note_plan_generation) reads
the selected notes into a PROPOSED change set and parks the plan on 'draft';
phase 2 (process_note_plan_application) writes the set the DM confirmed. Nothing
reaches the wiki in phase 1, and a failed phase 2 leaves the reviewed set in
place so the DM can fix an item and confirm again.
"""

from uuid import UUID

import pytest
from conftest import FakeLLM, FakePublisher, FakeWikiClient
from dnd_common.events import Event
from sqlalchemy import select

from app.clients.wiki_service import WikiServiceError
from app.models import CampaignNote, NotePlan
from app.services import note_plans, notes
from app.workers.generate import (
    process_note_plan_application,
    process_note_plan_generation,
)

CAMPAIGN_ID = "22222222-2222-2222-2222-222222222222"

PROPOSAL = {
    "language": "en",
    "pages": [
        {
            "kind": "character",
            "title": "Kaelor",
            "content": {"physical_look": "Grey-eyed.", "facts": ["Carries the ash blade."]},
        },
        {
            "kind": "location",
            "title": "Bree",
            "content": {"summary": "A market town on the north road."},
        },
    ],
    "relations": [
        {"from_title": "Kaelor", "to_title": "Bree", "relation_type": "appears_in"}
    ],
    "skipped": [{"title": "the ambush idea", "reason": "not a page yet"}],
}


def request_event(plan_id: str, note_ids: list[str]) -> Event:
    return Event(
        type="note.plan.requested",
        payload={"campaign_id": CAMPAIGN_ID, "plan_id": plan_id, "note_ids": note_ids},
    )


def confirmation_event() -> Event:
    return Event(
        type="note.plan.confirmed",
        payload={"campaign_id": CAMPAIGN_ID, "confirmed_by": None},
    )


async def _seed_notes(session_factory, count: int = 1) -> list[CampaignNote]:
    async with session_factory() as db:
        return [
            await notes.create_note(
                db, UUID(CAMPAIGN_ID), title=f"Note {index}", body=f"Body {index}."
            )
            for index in range(count)
        ]


async def _open_run(session_factory, note_ids) -> NotePlan:
    async with session_factory() as db:
        return await note_plans.start_generation(
            db,
            UUID(CAMPAIGN_ID),
            note_ids=note_ids,
            provider="deepseek",
            model="deepseek/deepseek-chat",
            prompt_version="np1",
        )


async def _plan_row(session_factory) -> NotePlan | None:
    async with session_factory() as db:
        return await db.scalar(select(NotePlan).where(NotePlan.campaign_id == UUID(CAMPAIGN_ID)))


# --------------------------------------------------- phase 1: reading the notes


async def test_reading_the_notes_proposes_pages_without_writing_any(session_factory, settings):
    rows = await _seed_notes(session_factory)
    plan = await _open_run(session_factory, [row.id for row in rows])
    llm = FakeLLM()
    llm.note_plan = PROPOSAL
    wiki_client = FakeWikiClient()
    publisher = FakePublisher()

    async with session_factory() as db:
        await process_note_plan_generation(
            request_event(str(plan.id), [str(row.id) for row in rows]),
            settings, wiki_client, llm, publisher, db,
        )

    # NOTHING reached the wiki: this is a proposal, not a write
    assert wiki_client.created == []
    assert wiki_client.updated == []
    assert wiki_client.applied == []

    stored = await _plan_row(session_factory)
    assert stored.status == "draft"
    assert stored.language == "en"
    assert stored.confirmed_by is None
    assert stored.applied_at is None
    assert [c["title"] for c in stored.changes] == ["Kaelor", "Bree"]
    assert [c["action"] for c in stored.changes] == ["create", "create"]
    assert stored.relations[0]["from_title"] == "Kaelor"
    assert stored.skipped[0]["title"] == "the ambush idea"
    assert [ev.type for ev in publisher.events] == ["content.note_plan.ready"]


async def test_the_notes_and_the_campaigns_pages_are_both_shown_to_the_model(
    session_factory, settings
):
    rows = await _seed_notes(session_factory)
    plan = await _open_run(session_factory, [row.id for row in rows])
    llm = FakeLLM()
    wiki_client = FakeWikiClient(
        existing_pages=[
            {"id": "p1", "title": "Bree", "kind": "location", "status": "published",
             "aliases": [], "content_json": {"summary": "A town."}}
        ]
    )

    async with session_factory() as db:
        await process_note_plan_generation(
            request_event(str(plan.id), [str(row.id) for row in rows]),
            settings, wiki_client, llm, FakePublisher(), db,
        )

    assert wiki_client.list_calls == [CAMPAIGN_ID]
    call = llm.note_plan_calls[0]
    assert call["notes"] == [{"title": "Note 0", "body": "Body 0."}]
    assert [p["title"] for p in call["existing_pages"]] == ["Bree"]


async def test_a_note_about_a_page_the_campaign_has_becomes_an_update(session_factory, settings):
    """Regenerating after the first apply is what makes the wiki an update."""
    rows = await _seed_notes(session_factory)
    plan = await _open_run(session_factory, [row.id for row in rows])
    llm = FakeLLM()
    llm.note_plan = PROPOSAL
    wiki_client = FakeWikiClient(
        existing_pages=[
            {"id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "title": "Bree",
             "kind": "location", "status": "published", "aliases": [],
             "content_json": {"summary": "A town."}}
        ]
    )

    async with session_factory() as db:
        await process_note_plan_generation(
            request_event(str(plan.id), [str(row.id) for row in rows]),
            settings, wiki_client, llm, FakePublisher(), db,
        )

    stored = await _plan_row(session_factory)
    bree = next(c for c in stored.changes if c["title"] == "Bree")
    assert bree["action"] == "update"
    assert bree["page_id"] == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    # the diff the DM reviews is anchored on what the page says today
    assert bree["before"]["content_json"] == {"summary": "A town."}
    assert "A market town on the north road." in bree["after"]["content_json"]["summary"]


async def test_notes_that_propose_nothing_yield_an_empty_review_not_a_failure(
    session_factory, settings
):
    """A note that is not about a page is a legitimate answer."""
    rows = await _seed_notes(session_factory)
    plan = await _open_run(session_factory, [row.id for row in rows])
    llm = FakeLLM()  # the default reading proposes nothing
    publisher = FakePublisher()

    async with session_factory() as db:
        await process_note_plan_generation(
            request_event(str(plan.id), [str(row.id) for row in rows]),
            settings, FakeWikiClient(), llm, publisher, db,
        )

    stored = await _plan_row(session_factory)
    assert stored.status == "draft"
    assert stored.changes == []
    assert [ev.type for ev in publisher.events] == ["content.note_plan.ready"]


async def test_a_discarded_or_superseded_run_is_a_no_op(session_factory, settings):
    """A redelivery must not resurrect a proposal nobody is waiting for."""
    rows = await _seed_notes(session_factory)
    await _open_run(session_factory, [row.id for row in rows])
    llm = FakeLLM()
    publisher = FakePublisher()

    async with session_factory() as db:
        # the plan id the message names is NOT the stored row's: a newer run
        # (or a discarded one) has taken the slot since it was published
        await process_note_plan_generation(
            request_event("99999999-9999-9999-9999-999999999999", [str(rows[0].id)]),
            settings, FakeWikiClient(), llm, publisher, db,
        )

    assert llm.note_plan_calls == []
    assert publisher.events == []
    assert (await _plan_row(session_factory)).changes == []


async def test_a_run_whose_notes_are_gone_fails_with_a_reason(session_factory, settings):
    rows = await _seed_notes(session_factory)
    plan = await _open_run(session_factory, [row.id for row in rows])
    async with session_factory() as db:
        await notes.delete_note(db, UUID(CAMPAIGN_ID), rows[0].id)

    with pytest.raises(ValueError, match="no longer exist"):
        async with session_factory() as db:
            await process_note_plan_generation(
                request_event(str(plan.id), [str(rows[0].id)]),
                settings, FakeWikiClient(), FakeLLM(), FakePublisher(), db,
            )

    stored = await _plan_row(session_factory)
    assert stored.status == "failed"
    assert "no longer exist" in stored.error


async def test_a_model_failure_is_recorded_not_swallowed(session_factory, settings):
    rows = await _seed_notes(session_factory)
    plan = await _open_run(session_factory, [row.id for row in rows])
    llm = FakeLLM()
    llm.note_plan_error = RuntimeError("the provider is down")

    with pytest.raises(RuntimeError):
        async with session_factory() as db:
            await process_note_plan_generation(
                request_event(str(plan.id), [str(row.id) for row in rows]),
                settings, FakeWikiClient(), llm, FakePublisher(), db,
            )

    stored = await _plan_row(session_factory)
    assert stored.status == "failed"
    assert "provider is down" in stored.error


async def test_a_listing_failure_does_not_block_the_proposal(session_factory, settings):
    """Without the dedupe net every page is a create, which the DM can drop."""
    rows = await _seed_notes(session_factory)
    plan = await _open_run(session_factory, [row.id for row in rows])
    llm = FakeLLM()
    llm.note_plan = PROPOSAL

    class BrokenWikiClient(FakeWikiClient):
        async def list_campaign_pages(
            self, campaign_id: str, *, limit: int = 200
        ) -> list[dict]:
            raise WikiServiceError("wiki-service is down")

    async with session_factory() as db:
        await process_note_plan_generation(
            request_event(str(plan.id), [str(row.id) for row in rows]),
            settings, BrokenWikiClient(), llm, FakePublisher(), db,
        )

    stored = await _plan_row(session_factory)
    assert stored.status == "draft"
    assert [c["action"] for c in stored.changes] == ["create", "create"]


async def test_the_plan_asks_for_the_whole_listing(session_factory, settings):
    """The pages a note TAGS are resolved out of this listing, so it asks for all
    of it (app/note_references.py): a tagged page that fell off the end of the
    default page of results would look like a page that does not exist yet, and
    the proposal would create a second one beside it."""
    rows = await _seed_notes(session_factory)
    plan = await _open_run(session_factory, [row.id for row in rows])
    wiki = FakeWikiClient(
        existing_pages=[
            {
                "id": "p1",
                "title": "Bree",
                "slug": "bree",
                "kind": "location",
                "status": "published",
                "content_json": {"summary": "A market town."},
            }
        ]
    )
    llm = FakeLLM()
    llm.note_plan = PROPOSAL

    async with session_factory() as db:
        await process_note_plan_generation(
            request_event(str(plan.id), [str(row.id) for row in rows]),
            settings, wiki, llm, FakePublisher(), db,
        )

    assert wiki.list_limits == [500]
    # the listing - content included - is what the model reads the notes against
    (call,) = llm.note_plan_calls
    assert call["existing_pages"][0]["content_json"]["summary"] == "A market town."


# --------------------------------------------------- phase 2: writing the wiki


async def test_applying_writes_the_confirmed_pages_and_marks_the_plan(session_factory, settings):
    rows = await _seed_notes(session_factory)
    await _open_run(session_factory, [row.id for row in rows])
    async with session_factory() as db:
        await note_plans.save_generation(
            db,
            UUID(CAMPAIGN_ID),
            change_set={
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
                            "content_json": {"summary": "Grey-eyed."},
                            "visibility": "public",
                            "confidence": None,
                        },
                        "timeline": None,
                        "dropped": False,
                    },
                    {
                        "id": "c2",
                        "action": "create",
                        "kind": "character",
                        "title": "Dropped One",
                        "page_id": None,
                        "before": None,
                        "after": {
                            "title": "Dropped One",
                            "content_json": {"summary": "x"},
                            "visibility": "public",
                            "confidence": None,
                        },
                        "timeline": None,
                        "dropped": True,
                    },
                ],
                "relations": [],
                "skipped": [],
            },
        )
    wiki_client = FakeWikiClient()
    publisher = FakePublisher()

    async with session_factory() as db:
        await process_note_plan_application(confirmation_event(), settings, wiki_client, publisher, db)

    # only what the DM kept is written
    assert [change["title"] for change in wiki_client.applied[0]["changes"]] == ["Kaelor"]
    # and a note plan attributes no page to a session
    assert wiki_client.applied[0]["session_id"] is None

    stored = await _plan_row(session_factory)
    assert stored.status == "applied"
    assert stored.applied_at is not None
    assert [ev.type for ev in publisher.events] == ["content.note_plan.applied"]
    assert publisher.events[0].payload["created"][0]["title"] == "Kaelor"


async def test_applying_an_already_applied_plan_is_a_no_op(session_factory, settings):
    rows = await _seed_notes(session_factory)
    await _open_run(session_factory, [row.id for row in rows])
    async with session_factory() as db:
        await note_plans.save_generation(db, UUID(CAMPAIGN_ID), change_set={"changes": []})
        await note_plans.mark_applied(db, UUID(CAMPAIGN_ID))
    wiki_client = FakeWikiClient()
    publisher = FakePublisher()

    async with session_factory() as db:
        await process_note_plan_application(confirmation_event(), settings, wiki_client, publisher, db)

    assert wiki_client.applied == []
    assert publisher.events == []


async def test_applying_without_a_plan_is_a_no_op(session_factory, settings):
    async with session_factory() as db:
        await process_note_plan_application(
            confirmation_event(), settings, FakeWikiClient(), FakePublisher(), db
        )
    assert await _plan_row(session_factory) is None


async def test_a_failed_write_keeps_the_review_so_the_dm_can_retry(session_factory, settings):
    rows = await _seed_notes(session_factory)
    await _open_run(session_factory, [row.id for row in rows])
    async with session_factory() as db:
        await note_plans.save_generation(
            db,
            UUID(CAMPAIGN_ID),
            change_set={
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
                            "content_json": {"summary": "x"},
                            "visibility": "public",
                            "confidence": None,
                        },
                        "timeline": None,
                        "dropped": False,
                    }
                ],
                "relations": [],
                "skipped": [],
            },
        )
    wiki_client = FakeWikiClient()
    wiki_client.apply_error = WikiServiceError("wiki-service is down")

    with pytest.raises(WikiServiceError):
        async with session_factory() as db:
            await process_note_plan_application(
                confirmation_event(), settings, wiki_client, FakePublisher(), db
            )

    stored = await _plan_row(session_factory)
    # back under review, with the reason, and the set the DM reviewed intact
    assert stored.status == "draft"
    assert "wiki-service is down" in stored.error
    assert [c["title"] for c in stored.changes] == ["Kaelor"]


async def test_the_worker_routes_the_two_note_events(session_factory, settings, monkeypatch):
    """handle() dispatches them; nothing else in the queue changes."""
    import app.workers.generate as worker

    seen: list[str] = []

    async def fake_generation(event, *args, **kwargs):
        seen.append("generation")

    async def fake_application(event, *args, **kwargs):
        seen.append("application")

    monkeypatch.setattr(worker, "process_note_plan_generation", fake_generation)
    monkeypatch.setattr(worker, "process_note_plan_application", fake_application)
    monkeypatch.setattr(worker, "_get_settings", lambda: settings)

    class FakeConnection:
        pass

    await worker.handle(
        Event(type="note.plan.requested", payload={"campaign_id": CAMPAIGN_ID}), FakeConnection()
    )
    await worker.handle(
        Event(type="note.plan.confirmed", payload={"campaign_id": CAMPAIGN_ID}), FakeConnection()
    )
    assert seen == ["generation", "application"]
