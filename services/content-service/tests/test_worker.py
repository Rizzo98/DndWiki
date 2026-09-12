"""Tests for the content worker's phases.

Phase 1 (process_job) distills the transcript into a DRAFT session summary,
phase 1b (process_summary_regeneration) applies the DM's review feedback,
phase 2 (process_plan_generation) turns the confirmed summary into a PROPOSED
change set, and phase 3 (process_plan_application) writes the change set the
DM confirmed into the wiki (pages published, timeline entries approved).

storage / clients / llm / publisher are fakes; the rows are persisted in an
in-memory SQLite DB. litellm is never imported (FakeLLM).
"""

from uuid import UUID

import pytest
from conftest import (
    CAMPAIGN_ID,
    PAGE_UUIDS,
    SESSION_ID,
    USER_ID,
    FakeCampaignClient,
    FakeLLM,
    FakePublisher,
    FakeSessionClient,
    FakeStorage,
    FakeUserClient,
    FakeWikiClient,
    make_event,
    make_extraction,
)
from dnd_common.events import Event
from sqlalchemy import select

from app import services as job_services
from app.models import GenerationJob, SessionSummary, WikiChangeSet
from app.workers.generate import (
    process_job,
    process_plan_application,
    process_plan_generation,
    process_summary_regeneration,
)


def _draft_ids_from(job: GenerationJob) -> set[str]:
    return set(job.draft_ids or [])


def confirmation_event(**overrides) -> Event:
    """The event the API publishes when the DM confirms the SUMMARY."""
    payload = {
        "session_id": SESSION_ID,
        "campaign_id": CAMPAIGN_ID,
        "summary_id": PAGE_UUIDS["Session Summary"],
        "revision": 1,
        "confirmed_by": USER_ID,
    }
    payload.update(overrides)
    return Event(type="summary.confirmed", payload=payload)


def plan_confirmation_event(**overrides) -> Event:
    """The event the API publishes when the DM confirms the PROPOSED CHANGES."""
    payload = {
        "session_id": SESSION_ID,
        "campaign_id": CAMPAIGN_ID,
        "plan_id": PAGE_UUIDS["Session Summary"],
        "confirmed_by": USER_ID,
    }
    payload.update(overrides)
    return Event(type="plan.confirmed", payload=payload)


def regeneration_event(*, edits=None, summary_lines=None) -> Event:
    """The event the API publishes when the DM asks for a rewrite."""
    return Event(
        type="summary.regenerate",
        payload={
            "session_id": SESSION_ID,
            "campaign_id": CAMPAIGN_ID,
            "edits": edits
            if edits is not None
            else [
                {
                    "targets": ["The party reaches the gates of Moria."],
                    "instruction": "It wasn't Aragorn, it was Boromir.",
                }
            ],
            "summary_lines": summary_lines,
            "requested_by": USER_ID,
        },
    )


async def _run_phase1(
    session_factory,
    settings,
    *,
    event=None,
    storage=None,
    session_client=None,
    user_client=None,
    wiki_client=None,
    llm=None,
    publisher=None,
    campaign_client=None,
):
    """Run the summary phase only (no page is created)."""
    async with session_factory() as db:
        await process_job(
            event or make_event(),
            settings,
            storage or FakeStorage(),
            session_client or FakeSessionClient(),
            user_client or FakeUserClient(),
            wiki_client or FakeWikiClient(),
            llm or FakeLLM(),
            publisher or FakePublisher(),
            db,
            campaign_client=campaign_client,
        )


async def _run_full_pipeline(
    session_factory,
    settings,
    *,
    event=None,
    storage=None,
    session_client=None,
    user_client=None,
    wiki_client=None,
    llm=None,
    publisher=None,
    campaign_client=None,
):
    """Phase 1 + summary confirmation + change-set confirmation, as the queue does."""
    session_client = session_client or FakeSessionClient()
    wiki_client = wiki_client or FakeWikiClient()
    publisher = publisher or FakePublisher()
    await _run_phase1(
        session_factory,
        settings,
        event=event,
        storage=storage,
        session_client=session_client,
        user_client=user_client,
        wiki_client=wiki_client,
        llm=llm,
        publisher=publisher,
        campaign_client=campaign_client,
    )
    await _run_plan_generation(
        session_factory, settings, session_client, wiki_client, publisher
    )
    await _run_plan_application(
        session_factory, settings, session_client, wiki_client, publisher
    )


async def _confirm_summary(session_factory) -> None:
    """Stamp the summary confirmed, exactly as the API endpoint does."""
    async with session_factory() as db:
        await job_services.confirm_summary(
            db, UUID(SESSION_ID), confirmed_by=UUID(USER_ID)
        )


async def _run_plan_generation(
    session_factory, settings, session_client, wiki_client, publisher
) -> None:
    """Phase 2: the confirmed summary becomes a proposed change set."""
    await _confirm_summary(session_factory)
    async with session_factory() as db:
        await process_plan_generation(
            confirmation_event(), settings, session_client, wiki_client, publisher, db
        )


async def _run_plan_application(
    session_factory, settings, session_client, wiki_client, publisher
) -> None:
    """Phase 3: the confirmed change set is written to the wiki."""
    async with session_factory() as db:
        await process_plan_application(
            plan_confirmation_event(), settings, session_client, wiki_client, publisher, db
        )


async def _plan_row(session_factory) -> WikiChangeSet | None:
    async with session_factory() as db:
        return (await db.execute(select(WikiChangeSet))).scalars().first()


# ------------------------------------------------- phase 1: the draft summary


async def test_process_job_creates_a_draft_summary_and_no_page(session_factory, settings):
    """The summary is the intermediate layer: phase 1 writes NOTHING to the
    wiki, it parks the session on summary_ready for the DM to review."""
    storage = FakeStorage()
    session_client = FakeSessionClient()
    wiki_client = FakeWikiClient()
    publisher = FakePublisher()

    await _run_phase1(
        session_factory,
        settings,
        storage=storage,
        session_client=session_client,
        wiki_client=wiki_client,
        publisher=publisher,
    )

    assert session_client.status_calls == [
        (SESSION_ID, "summarizing", None),
        (SESSION_ID, "summary_ready", None),
    ]
    # no page, no relation, no timeline entry was created
    assert wiki_client.created == []
    assert wiki_client.relations == []
    assert wiki_client.timeline_upserts == []
    # and the wiki was not even queried for existing pages
    assert wiki_client.list_calls == []

    # the draft summary is persisted with its review bookkeeping
    async with session_factory() as db:
        summary = (await db.execute(select(SessionSummary))).scalars().one()
        job = (await db.execute(select(GenerationJob))).scalars().one()
    assert summary.summary == "The party reaches the gates of Moria."
    assert summary.review_status == "draft"
    assert summary.revision == 1
    assert summary.language == "en"
    assert summary.confirmed_at is None
    assert summary.llm_provider == "deepseek"
    assert job.phase == "summary"
    assert job.status == "done"
    assert job.draft_ids == []

    # the DM is told a draft summary is waiting for review
    assert [ev.type for ev in publisher.events] == ["content.summary.drafted"]
    ev = publisher.events[0]
    assert ev.payload["session_id"] == SESSION_ID
    assert ev.payload["campaign_id"] == CAMPAIGN_ID
    assert ev.payload["revision"] == 1
    assert ev.payload["summary_id"] == str(summary.id)


async def test_process_job_happy_path(session_factory, settings):
    """Phase 1 + both confirmations -> published pages, events and timeline."""
    storage = FakeStorage()
    session_client = FakeSessionClient()
    user_client = FakeUserClient()
    wiki_client = FakeWikiClient()
    llm = FakeLLM()
    publisher = FakePublisher()

    await _run_full_pipeline(
        session_factory,
        settings,
        storage=storage,
        session_client=session_client,
        user_client=user_client,
        wiki_client=wiki_client,
        llm=llm,
        publisher=publisher,
    )

    # pipeline state driven correctly through every phase
    assert session_client.status_calls == [
        (SESSION_ID, "summarizing", None),
        (SESSION_ID, "summary_ready", None),
        (SESSION_ID, "generating_wiki", None),
        (SESSION_ID, "wiki_plan_ready", None),
        (SESSION_ID, "applying_wiki", None),
        (SESSION_ID, "content_ready", None),
    ]
    # transcript downloaded from the transcripts bucket, named view resolved
    assert storage.downloads == [("transcripts", "transcripts/" + SESSION_ID + "/transcript.json")]
    assert user_client.calls == [[USER_ID]]
    view = llm.views[0]
    assert "Aragorn: We must enter Moria." in view
    assert "SPEAKER_01: The doors are sealed." in view

    # pages created in order: characters, locations, then event pages -
    # world-significant events become event pages + timeline entries
    assert [d["kind"] for d in wiki_client.created] == [
        "character", "location", "event",
    ]
    character = wiki_client.created[0]
    # the DM confirmed the change set: nothing lands in "pending review"
    assert character["status"] == "published"
    assert character["source_session_id"] == SESSION_ID
    assert character["confidence"] == 1.0
    # no party info on the event -> plain NPC tagging
    assert character["content_json"]["attributes"] == {"character_type": "npc"}

    # the event page carries the extracted description + participants
    event_draft = wiki_client.created[2]
    assert event_draft["kind"] == "event"
    assert event_draft["title"] == "Entering Moria"
    assert event_draft["content_json"]["summary"] == "The party passes the gate."
    assert event_draft["content_json"]["attributes"]["participants"] == ["Aragorn"]

    # its timeline entry is written approved (the DM confirmed it)
    assert len(wiki_client.timeline_upserts) == 1
    timeline = wiki_client.timeline_upserts[0]
    assert timeline["approved"] is True
    assert timeline["campaign_id"] == CAMPAIGN_ID
    assert timeline["page_id"] == PAGE_UUIDS["Entering Moria"]
    assert timeline["summary"] == "The party passes the gate."
    assert timeline["source_session_id"] == SESSION_ID

    # no appears_in relations without event pages
    assert wiki_client.relations == []

    # cross-run dedupe input: existing campaign pages fetched once
    assert wiki_client.list_calls == [CAMPAIGN_ID]

    # the apply run records the page ids it wrote
    async with session_factory() as db:
        jobs = (await db.execute(select(GenerationJob))).scalars().all()
    apply_job = next(j for j in jobs if j.phase == "apply")
    assert apply_job.status == "done"
    assert apply_job.prompt_version == "v11"
    assert float(apply_job.confidence) == 1.0
    assert _draft_ids_from(apply_job) == {
        PAGE_UUIDS["Aragorn"], PAGE_UUIDS["Moria"], PAGE_UUIDS["Entering Moria"],
    }
    wiki_job = next(j for j in jobs if j.phase == "wiki")
    assert wiki_job.status == "done"
    # the planning run creates no page
    assert wiki_job.draft_ids == []

    # the change set is applied and keeps the DM's confirmation stamp
    async with session_factory() as db:
        plan = (await db.execute(select(WikiChangeSet))).scalars().one()
    assert plan.status == "applied"
    assert plan.applied_at is not None
    assert plan.confirmed_by == UUID(USER_ID)

    # merged summary persisted and CONFIRMED by the DM
    async with session_factory() as db:
        summary = (await db.execute(select(SessionSummary))).scalars().one()
    assert summary.session_id == UUID(SESSION_ID)
    assert summary.summary == "The party reaches the gates of Moria."
    assert summary.review_status == "confirmed"
    assert summary.confirmed_by == UUID(USER_ID)
    assert summary.confirmed_at is not None
    assert [e["title"] for e in summary.events] == ["Entering Moria"]
    assert summary.timeline_entries[0]["time"] == "00:00:12"
    assert summary.characters[0]["name"] == "Aragorn"

    # the DM was told a change set was waiting, then what was written
    assert [ev.type for ev in publisher.events] == [
        "content.summary.drafted", "content.plan.ready", "content.generated",
    ]
    ready = publisher.events[1]
    assert ready.payload["create"] == 3
    assert ready.payload["update"] == 0
    assert ready.payload["session_id"] == SESSION_ID

    ev = publisher.events[-1]
    assert ev.type == "content.generated"
    payload = ev.payload
    assert payload["session_id"] == SESSION_ID
    assert payload["campaign_id"] == CAMPAIGN_ID
    assert payload["generation_job_id"] == str(apply_job.id)
    assert set(payload["draft_ids"]) == {
        PAGE_UUIDS["Aragorn"], PAGE_UUIDS["Moria"], PAGE_UUIDS["Entering Moria"],
    }
    assert payload["confidence"] == 1.0
    assert payload["language"] == "en"
    assert {item["title"] for item in payload["created"]} == {
        "Aragorn", "Moria", "Entering Moria",
    }
    assert payload["skipped"] == []

async def test_process_job_skips_entities_already_documented(session_factory, settings):
    """An entity the wiki already has is not re-drafted; its new facts stay
    visible on the persisted session summary (session page)."""
    wiki_client = FakeWikiClient(
        existing_pages=[
            {
                "id": PAGE_UUIDS["Moria"],
                "title": "Moria",
                "slug": "moria",
                "kind": "location",
                "status": "published",
                "aliases": ["Mines of Moria"],
            }
        ]
    )
    publisher = FakePublisher()

    await _run_full_pipeline(
        session_factory, settings, wiki_client=wiki_client, publisher=publisher
    )

    # no duplicate Moria page; only the fresh Aragorn page is created. The
    # event page is still proposed (events dedupe against EVENT pages only).
    assert [d["kind"] for d in wiki_client.created] == ["character", "event"]
    assert all(d["title"] != "Moria" for d in wiki_client.created)

    # the DM sees it as context ("already documented"), not as a change
    plan = await _plan_row(session_factory)
    assert [s["title"] for s in plan.skipped] == ["Moria"]
    ready = next(ev for ev in publisher.events if ev.type == "content.plan.ready")
    assert ready.payload["skipped"] == 1
    assert ready.payload["create"] == 2


async def test_process_job_ignores_late_speaker_assignment(session_factory, settings):
    """A speakers.assigned arriving while the DM reviews the summary (or after
    the wiki was generated) must NOT throw the reviewed summary away; a session
    that still has unnamed speakers waits too."""
    for status in (
        "summary_ready",
        "generating_wiki",
        "content_ready",
        "reviewed",
        "published",
        "speaker_pending",
    ):
        session_client = FakeSessionClient(status=status)
        storage = FakeStorage()
        publisher = FakePublisher()
        await _run_phase1(
            session_factory,
            settings,
            storage=storage,
            session_client=session_client,
            publisher=publisher,
        )
        assert session_client.status_calls == [], status
        assert storage.downloads == [], status
        assert publisher.events == [], status


async def test_process_job_updates_existing_event_page(session_factory, settings):
    """An event the timeline already documents is NOT re-drafted: its page is
    updated with the new information and its timeline entry is refreshed."""
    wiki_client = FakeWikiClient(
        existing_pages=[
            {
                "id": PAGE_UUIDS["Entering Moria"],
                "title": "Entering Moria",
                "slug": "entering-moria",
                "kind": "event",
                "status": "published",
                "aliases": [],
                "content_json": {
                    "summary": "The party passes the gate.",
                    "attributes": {"participants": ["Aragorn"]},
                },
            }
        ]
    )

    await _run_full_pipeline(session_factory, settings, wiki_client=wiki_client)

    # no event draft: the existing page is updated instead
    assert [d["kind"] for d in wiki_client.created] == ["character", "location"]
    assert len(wiki_client.updated) == 1
    page_id, payload = wiki_client.updated[0]
    assert page_id == PAGE_UUIDS["Entering Moria"]
    assert "session" in payload["change_note"]
    assert payload["content_json"]["summary"] == "The party passes the gate."

    # timeline entry refreshed for the SAME page (no duplicate entry)
    assert len(wiki_client.timeline_upserts) == 1
    assert wiki_client.timeline_upserts[0]["page_id"] == PAGE_UUIDS["Entering Moria"]


async def test_process_job_tags_player_characters(session_factory, settings):
    """A character_name on the speaker map labels the transcript by CHARACTER,
    prepends the party line to every chunk view and tags the matching page
    (by name or alias) as a player character."""
    event = make_event(
        speakers=[
            {
                "label": "SPEAKER_00",
                "user_id": USER_ID,
                "confidence": 0.91,
                "status": "auto",
                "character_name": "Strider",
            },
            {"label": "SPEAKER_01", "user_id": None, "confidence": 0.4, "status": "pending"},
        ],
    )
    wiki_client = FakeWikiClient()
    llm = FakeLLM()
    await _run_full_pipeline(
        session_factory, settings, event=event, wiki_client=wiki_client, llm=llm
    )

    # transcript + prompt context use the CHARACTER name, never the player's
    assert llm.views[0].startswith("Party (player characters): Strider")
    assert "Strider: We must enter Moria." in llm.views[0]

    # extraction says 'Aragorn' but the alias matches the party -> player page
    character = next(d for d in wiki_client.created if d["kind"] == "character")
    assert character["content_json"]["attributes"] == {"character_type": "player"}

    # the party names are persisted with the summary: phase 2 tags the drafts
    # without re-reading the speaker map
    async with session_factory() as db:
        summary = (await db.execute(select(SessionSummary))).scalars().one()
    assert summary.party_characters == ["Strider"]


async def test_process_job_pending_assignment_skips(session_factory, settings):
    storage = FakeStorage()
    session_client = FakeSessionClient()
    publisher = FakePublisher()

    await _run_phase1(
        session_factory,
        settings,
        event=make_event(pending_assignment=True),
        storage=storage,
        session_client=session_client,
        publisher=publisher,
    )

    assert session_client.status_calls == []
    assert storage.downloads == []
    assert publisher.events == []
    async with session_factory() as db:
        assert (await db.execute(select(GenerationJob))).scalars().first() is None


async def test_process_job_idempotent_redelivery(session_factory, settings):
    """409 on entering the summary phase -> ack, no work done."""
    session_client = FakeSessionClient(conflict_on_start=True)
    storage = FakeStorage()
    publisher = FakePublisher()

    await _run_phase1(
        session_factory,
        settings,
        storage=storage,
        session_client=session_client,
        publisher=publisher,
    )

    assert session_client.status_calls == []
    assert storage.downloads == []
    assert publisher.events == []
    async with session_factory() as db:
        assert (await db.execute(select(GenerationJob))).scalars().first() is None


async def test_process_job_failure_marks_failed_and_reraises(session_factory, settings):
    storage = FakeStorage()
    session_client = FakeSessionClient()
    llm = FakeLLM(error=RuntimeError("llm down"))
    publisher = FakePublisher()

    async with session_factory() as db:
        with pytest.raises(RuntimeError, match="llm down"):
            await process_job(
                make_event(), settings, storage, session_client,
                FakeUserClient(), FakeWikiClient(), llm, publisher, db,
            )

    assert session_client.status_calls[-1] == (SESSION_ID, "failed", "llm down")
    assert publisher.events == []
    async with session_factory() as db:
        job = (await db.execute(select(GenerationJob))).scalars().one()
    assert job.status == "failed"
    assert job.phase == "summary"
    assert "llm down" in job.error


async def test_process_job_speakers_assigned_event(session_factory, settings):
    """speakers.assigned carries one label -> the worker names just that one."""
    event = Event(
        type="speakers.assigned",
        payload={
            "session_id": SESSION_ID,
            "campaign_id": CAMPAIGN_ID,
            "label": "SPEAKER_01",
            "user_id": USER_ID,
            "assigned_by": "44444444-4444-4444-4444-444444444444",
            "enrolled_voiceprint": False,
        },
    )
    llm = FakeLLM()
    await _run_phase1(session_factory, settings, event=event, llm=llm)

    view = llm.views[0]
    assert "Aragorn: The doors are sealed." in view   # SPEAKER_01 -> named
    assert "SPEAKER_00: We must enter Moria." in view  # unknown -> raw label


async def test_process_job_proposes_durable_relations(session_factory, settings):
    """Auto-extracted relationships become relations: targets resolved by name
    through the just-created drafts, or through already-existing pages."""
    extraction = make_extraction()
    extraction["characters"] = [
        {
            "name": "Aragorn",
            "aliases": [],
            "description": "A ranger.",
            "facts": [],
            "session_facts": [],
            "mentions": 2,
            "relationships": {"member_of": ["Fellowship of the Ring"]},
        },
        {
            "name": "Gimli",
            "aliases": [],
            "description": "A dwarf.",
            "facts": [],
            "session_facts": [],
            "mentions": 1,
            "relationships": {"led_by": ["Aragorn"]},
        },
    ]
    extraction["locations"] = []
    wiki_client = FakeWikiClient(
        existing_pages=[
            {
                "id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
                "title": "Fellowship of the Ring",
                "slug": "fellowship-of-the-ring",
                "kind": "faction",
                "status": "published",
                "aliases": ["The Fellowship"],
            }
        ]
    )
    llm = FakeLLM(extractions=[extraction])
    await _run_full_pipeline(session_factory, settings, wiki_client=wiki_client, llm=llm)

    # fresh draft -> existing faction (by title); fresh draft -> fresh draft.
    # FakeWikiClient counts len(created) AFTER appending, so the 2nd page
    # (Gimli) gets UUID(int=2).
    gimli_id = str(UUID(int=2))
    relations = sorted(wiki_client.relations, key=lambda rel: rel[2])
    assert relations == [
        (gimli_id, PAGE_UUIDS["Aragorn"], "led_by"),  # Gimli is led by Aragorn
        (PAGE_UUIDS["Aragorn"], "ffffffff-ffff-ffff-ffff-ffffffffffff", "member_of"),
    ]


async def test_process_job_multi_chunk(session_factory, settings):
    """Small chunk budget -> several chunks, all extracted and merged."""
    settings.chunk_tokens = 30  # tiny budget -> multiple chunks
    llm = FakeLLM()
    await _run_phase1(session_factory, settings, llm=llm)
    assert len(llm.views) > 1


async def test_process_job_excludes_dm_speaker(session_factory, settings):
    """The DM narrates but is not part of the world: no character page is
    drafted for them, they are left out of the party line, and their resolved
    name reaches the prompt as an out-of-world speaker."""
    dm_user_id = "44444444-4444-4444-4444-444444444444"
    event = make_event(
        speakers=[
            {
                "label": "SPEAKER_00",
                "user_id": USER_ID,
                "confidence": 0.91,
                "status": "auto",
                "character_name": "Strider",
            },
            {
                "label": "SPEAKER_01",
                "user_id": dm_user_id,
                "confidence": 0.5,
                "status": "auto",
                "character_name": "Dungeon Master",
            },
        ],
    )
    extraction = make_extraction()
    extraction["characters"].append(
        {
            "name": "Dungeon Master",
            "aliases": [],
            "description": "Narrates the scene.",
            "facts": [],
            "session_facts": [],
            "mentions": 5,
        }
    )
    wiki_client = FakeWikiClient()
    llm = FakeLLM(extractions=[extraction])
    campaign_client = FakeCampaignClient(dm_user_id=dm_user_id)
    await _run_full_pipeline(
        session_factory,
        settings,
        event=event,
        wiki_client=wiki_client,
        llm=llm,
        campaign_client=campaign_client,
    )

    # DM kept in the transcript view (their narration is world content) but
    # excluded from the party line and from the drafted pages
    assert "Dungeon Master: The doors are sealed." in llm.views[0]
    assert llm.views[0].splitlines()[0] == "Party (player characters): Strider"
    assert [d["title"] for d in wiki_client.created] == ["Aragorn", "Moria", "Entering Moria"]
    assert campaign_client.calls == [CAMPAIGN_ID]
    # the out-of-world note reaches the model through the user message
    assert llm.out_of_world == ["Dungeon Master"]


# ------------------------------------------- phase 1b: the DM review feedback


async def test_process_summary_regeneration_applies_feedback(session_factory, settings):
    """The DM's correction is applied to the persisted extraction and parked
    back on summary_ready as a new revision."""
    revised = make_extraction()
    revised["session_summary"] = "Boromir was going to the city center."
    revised["characters"][0]["name"] = "Boromir"
    llm = FakeLLM(revised=revised)
    session_client = FakeSessionClient()
    publisher = FakePublisher()

    await _run_phase1(
        session_factory, settings, session_client=session_client, publisher=publisher
    )
    async with session_factory() as db:
        await process_summary_regeneration(
            regeneration_event(), settings, session_client, llm, publisher, db
        )

    # the session goes back into the summary phase and lands on summary_ready
    assert session_client.status_calls[-2:] == [
        (SESSION_ID, "summarizing", None),
        (SESSION_ID, "summary_ready", None),
    ]
    # the model saw the persisted extraction plus the DM's request
    call = llm.revise_calls[0]
    assert call["current"]["session_summary"] == "The party reaches the gates of Moria."
    assert call["edits"][0]["instruction"] == "It wasn't Aragorn, it was Boromir."

    async with session_factory() as db:
        summary = (await db.execute(select(SessionSummary))).scalars().one()
        jobs = (await db.execute(select(GenerationJob))).scalars().all()
    assert summary.summary == "Boromir was going to the city center."
    assert summary.revision == 2
    assert summary.review_status == "draft"
    assert summary.characters[0]["name"] == "Boromir"
    # the feedback is kept for auditability
    assert summary.edit_history[0]["instruction"] == "It wasn't Aragorn, it was Boromir."
    assert summary.edit_history[0]["targets"] == ["The party reaches the gates of Moria."]
    assert summary.edit_history[0]["requested_by"] == USER_ID
    assert len(jobs) == 2
    assert all(job.status == "done" for job in jobs)

    events = [ev for ev in publisher.events if ev.type == "content.summary.drafted"]
    assert len(events) == 2
    assert events[-1].payload["revision"] == 2


async def test_process_summary_regeneration_keeps_confidences(session_factory, settings):
    """A rewrite must not reset the confidence badges: the model cannot
    recompute them, the worker carries the previous values over."""
    session_client = FakeSessionClient()
    await _run_phase1(session_factory, settings, session_client=session_client)

    # the model echoes the entities back without any confidence field
    revised = make_extraction()
    revised["session_summary"] = "Rewritten."
    llm = FakeLLM(revised=revised)
    async with session_factory() as db:
        await process_summary_regeneration(
            regeneration_event(), settings, session_client, llm, FakePublisher(), db
        )

    async with session_factory() as db:
        summary = (await db.execute(select(SessionSummary))).scalars().one()
    assert summary.confidence == 1.0
    assert summary.characters[0]["confidence"] == 1.0
    assert summary.events[0]["confidence"] == 1.0


async def test_process_summary_regeneration_accepts_client_lines(session_factory, settings):
    """The client sends the lines as displayed, so hand edits are honored."""
    session_client = FakeSessionClient()
    await _run_phase1(session_factory, settings, session_client=session_client)
    llm = FakeLLM()
    async with session_factory() as db:
        await process_summary_regeneration(
            regeneration_event(summary_lines=["hand edited line"]),
            settings,
            session_client,
            llm,
            FakePublisher(),
            db,
        )
    assert llm.revise_calls[0]["summary_lines"] == ["hand edited line"]


async def test_process_summary_regeneration_skips_when_not_reviewable(session_factory, settings):
    """409 (the DM confirmed in the meantime, or another rewrite is running)."""
    session_client = FakeSessionClient(conflict_on_start=True)
    llm = FakeLLM()
    publisher = FakePublisher()
    async with session_factory() as db:
        await process_summary_regeneration(
            regeneration_event(), settings, session_client, llm, publisher, db
        )
    assert llm.revise_calls == []
    assert publisher.events == []
    async with session_factory() as db:
        assert (await db.execute(select(GenerationJob))).scalars().first() is None


async def test_process_summary_regeneration_requires_a_summary(session_factory, settings):
    session_client = FakeSessionClient()
    async with session_factory() as db:
        with pytest.raises(ValueError, match="no summary to rewrite"):
            await process_summary_regeneration(
                regeneration_event(), settings, session_client, FakeLLM(), FakePublisher(), db
            )
    assert session_client.status_calls[-1] == (SESSION_ID, "failed", "session " + SESSION_ID + " has no summary to rewrite")


async def test_process_summary_regeneration_failure_marks_failed(session_factory, settings):
    session_client = FakeSessionClient()
    await _run_phase1(session_factory, settings, session_client=session_client)
    llm = FakeLLM()
    llm.revise_error = RuntimeError("llm down")
    async with session_factory() as db:
        with pytest.raises(RuntimeError, match="llm down"):
            await process_summary_regeneration(
                regeneration_event(), settings, session_client, llm, FakePublisher(), db
            )
    assert session_client.status_calls[-1] == (SESSION_ID, "failed", "llm down")
    async with session_factory() as db:
        summary = (await db.execute(select(SessionSummary))).scalars().one()
    # the draft the DM is reviewing is left untouched
    assert summary.summary == "The party reaches the gates of Moria."
    assert summary.revision == 1


# --------------------------------- phase 2: the proposed change set


async def test_process_plan_generation_proposes_without_writing(session_factory, settings):
    """The confirmed summary becomes a PROPOSED change set: nothing in the wiki."""
    session_client = FakeSessionClient()
    wiki_client = FakeWikiClient()
    publisher = FakePublisher()
    await _run_phase1(session_factory, settings, session_client=session_client)
    await _confirm_summary(session_factory)

    async with session_factory() as db:
        await process_plan_generation(
            confirmation_event(), settings, session_client, wiki_client, publisher, db
        )

    # the session parks on the review status and NOTHING was written
    assert session_client.status_calls[-2:] == [
        (SESSION_ID, "generating_wiki", None),
        (SESSION_ID, "wiki_plan_ready", None),
    ]
    assert wiki_client.created == []
    assert wiki_client.updated == []
    assert wiki_client.timeline_upserts == []
    assert wiki_client.applied == []

    async with session_factory() as db:
        plan = (await db.execute(select(WikiChangeSet))).scalars().one()
    assert plan.status == "draft"
    assert plan.session_id == UUID(SESSION_ID)
    assert plan.language == "en"
    # the plan itself is not confirmed yet: the DM reviews it next
    assert plan.confirmed_by is None
    assert plan.summary_id is not None
    assert plan.applied_at is None
    assert [c["title"] for c in plan.changes] == ["Aragorn", "Moria", "Entering Moria"]
    assert [c["action"] for c in plan.changes] == ["create", "create", "create"]
    event_change = plan.changes[2]
    assert event_change["timeline"]["summary"] == "The party passes the gate."
    assert event_change["after"]["content_json"]["summary"] == "The party passes the gate."

    # the phase publishes the change-set-ready event (the summary draft event
    # of phase 1 went to the publisher of that phase)
    assert [ev.type for ev in publisher.events] == ["content.plan.ready"]


async def test_process_plan_generation_requires_a_confirmed_summary(session_factory, settings):
    """Generation starts only AFTER the summary confirmation."""
    session_client = FakeSessionClient()
    await _run_phase1(session_factory, settings, session_client=session_client)
    async with session_factory() as db:
        summary = (await db.execute(select(SessionSummary))).scalars().one()
        summary.review_status = "draft"   # the summary was never confirmed
        await db.commit()

    with pytest.raises(ValueError, match="no confirmed summary"):
        async with session_factory() as db:
            await process_plan_generation(
                confirmation_event(), settings, session_client, FakeWikiClient(),
                FakePublisher(), db,
            )
    assert session_client.status_calls[-1] == (
        SESSION_ID, "failed", "session " + SESSION_ID + " has no confirmed summary to expand"
    )
    assert await _plan_row(session_factory) is None


async def test_process_plan_generation_is_idempotent(session_factory, settings):
    """A redelivered summary.confirmed (already staging/finished) is a no-op."""
    session_client = FakeSessionClient()
    await _run_phase1(session_factory, settings, session_client=session_client)
    wiki_client = FakeWikiClient()
    await _run_plan_generation(
        session_factory, settings, session_client, wiki_client, FakePublisher()
    )

    session_client.conflict_on_start = True
    publisher = FakePublisher()
    async with session_factory() as db:
        await process_plan_generation(
            confirmation_event(), settings, session_client, wiki_client, publisher, db
        )
    assert publisher.events == []
    async with session_factory() as db:
        jobs = (await db.execute(select(GenerationJob))).scalars().all()
    assert [job.phase for job in jobs] == ["summary", "wiki"]


async def test_process_plan_generation_failure_marks_failed(session_factory, settings):
    """A failure while proposing leaves the confirmed summary untouched."""
    session_client = FakeSessionClient()
    await _run_phase1(session_factory, settings, session_client=session_client)
    await _confirm_summary(session_factory)

    class BrokenWikiClient(FakeWikiClient):
        async def list_campaign_pages(self, campaign_id: str) -> list[dict]:
            raise RuntimeError("wiki down")

    async with session_factory() as db:
        with pytest.raises(RuntimeError, match="wiki down"):
            await process_plan_generation(
                confirmation_event(), settings, session_client,
                BrokenWikiClient(), FakePublisher(), db,
            )

    assert session_client.status_calls[-1] == (SESSION_ID, "failed", "wiki down")
    async with session_factory() as db:
        summary = (await db.execute(select(SessionSummary))).scalars().one()
    assert summary.review_status == "confirmed"


# ------------------------------------ phase 3: writing the change set


async def test_process_plan_application_writes_the_confirmed_changes(session_factory, settings):
    """The confirmed set is applied through wiki-service, dropped items out."""
    session_client = FakeSessionClient()
    wiki_client = FakeWikiClient()
    publisher = FakePublisher()
    await _run_phase1(session_factory, settings, session_client=session_client)
    await _run_plan_generation(
        session_factory, settings, session_client, wiki_client, publisher
    )
    # the DM drops the location and edits the character title before confirming
    async with session_factory() as db:
        plan = await job_services.get_plan(db, UUID(SESSION_ID))
        changes = [dict(c) for c in plan.changes]
        changes[1] = {**changes[1], "dropped": True}
        changes[0] = {
            **changes[0],
            "title": "Aragorn the Strider",
            "after": {**changes[0]["after"], "title": "Aragorn the Strider"},
        }
        await job_services.replace_reviewable(db, UUID(SESSION_ID), changes=changes)
        await job_services.confirm_plan(db, UUID(SESSION_ID), confirmed_by=UUID(USER_ID))

    await _run_plan_application(
        session_factory, settings, session_client, wiki_client, publisher
    )

    assert session_client.status_calls[-2:] == [
        (SESSION_ID, "applying_wiki", None),
        (SESSION_ID, "content_ready", None),
    ]
    applied = wiki_client.applied[0]
    assert {c["title"] for c in applied["changes"]} == {
        "Aragorn the Strider", "Entering Moria",
    }
    assert all(c["action"] == "create" for c in applied["changes"])
    assert applied["session_id"] == SESSION_ID
    assert applied["confirmed_by"] == USER_ID

    async with session_factory() as db:
        plan = (await db.execute(select(WikiChangeSet))).scalars().one()
    assert plan.status == "applied"
    assert plan.applied_at is not None


async def test_process_plan_application_is_idempotent(session_factory, settings):
    """A redelivered plan.confirmed is a no-op: no page is written twice."""
    session_client = FakeSessionClient()
    wiki_client = FakeWikiClient()
    await _run_phase1(session_factory, settings, session_client=session_client)
    await _run_plan_generation(
        session_factory, settings, session_client, wiki_client, FakePublisher()
    )
    session_client.conflict_on_start = True

    async with session_factory() as db:
        await process_plan_application(
            plan_confirmation_event(), settings, session_client, wiki_client,
            FakePublisher(), db,
        )
    assert wiki_client.applied == []


async def test_process_plan_application_requires_a_plan(session_factory, settings):
    session_client = FakeSessionClient()
    with pytest.raises(ValueError, match="no proposed change set"):
        async with session_factory() as db:
            await process_plan_application(
                plan_confirmation_event(), settings, session_client,
                FakeWikiClient(), FakePublisher(), db,
            )
    assert session_client.status_calls[-1] == (
        SESSION_ID, "failed", "session " + SESSION_ID + " has no proposed change set"
    )


async def test_process_plan_application_failure_keeps_the_review(session_factory, settings):
    """A wiki-service failure puts the set back in review: the DM can fix an
    item and confirm again instead of losing the whole proposal."""
    session_client = FakeSessionClient()
    wiki_client = FakeWikiClient()
    await _run_phase1(session_factory, settings, session_client=session_client)
    await _run_plan_generation(
        session_factory, settings, session_client, wiki_client, FakePublisher()
    )
    wiki_client.apply_error = RuntimeError("wiki down")

    with pytest.raises(RuntimeError, match="wiki down"):
        async with session_factory() as db:
            await process_plan_application(
                plan_confirmation_event(), settings, session_client, wiki_client,
                FakePublisher(), db,
            )

    assert session_client.status_calls[-1] == (SESSION_ID, "failed", "wiki down")
    async with session_factory() as db:
        plan = (await db.execute(select(WikiChangeSet))).scalars().one()
    assert plan.status == "draft"
    assert plan.error == "wiki down"


# ------------------------------------------------------------- helpers
