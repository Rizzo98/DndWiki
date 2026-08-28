"""Tests for the generation worker end-to-end flow (process_job).

storage / clients / llm / publisher are fakes; the generation_jobs row is
persisted in an in-memory SQLite DB. litellm is never imported (FakeLLM).
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

from app.models import GenerationJob, SessionSummary
from app.workers.generate import build_speaker_map, process_job, resolve_speaker_names


def _draft_ids_from(job: GenerationJob) -> set[str]:
    return set(job.draft_ids or [])


async def test_process_job_happy_path(session_factory, settings):
    storage = FakeStorage()
    session_client = FakeSessionClient()
    user_client = FakeUserClient()
    wiki_client = FakeWikiClient()
    llm = FakeLLM()
    publisher = FakePublisher()

    async with session_factory() as db:
        await process_job(
            make_event(), settings, storage, session_client,
            user_client, wiki_client, llm, publisher, db,
        )

    # pipeline state driven correctly
    assert session_client.status_calls == [
        (SESSION_ID, "generating_wiki", None),
        (SESSION_ID, "content_ready", None),
    ]
    # transcript downloaded from the transcripts bucket, named view resolved
    assert storage.downloads == [("transcripts", f"transcripts/{SESSION_ID}/transcript.json")]
    assert user_client.calls == [[USER_ID]]
    view = llm.views[0]
    assert "Aragorn: We must enter Moria." in view
    assert "SPEAKER_01: The doors are sealed." in view

    # drafts created in order: characters, locations, then event pages -
    # world-significant events become event pages + pending timeline entries
    assert [d["kind"] for d in wiki_client.created] == [
        "character", "location", "event",
    ]
    character = wiki_client.created[0]
    assert character["status"] == "pending_review"
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

    # its timeline entry is upserted as pending (approved=False server-side)
    assert len(wiki_client.timeline_upserts) == 1
    timeline = wiki_client.timeline_upserts[0]
    assert timeline["campaign_id"] == CAMPAIGN_ID
    assert timeline["page_id"] == PAGE_UUIDS["Entering Moria"]
    assert timeline["summary"] == "The party passes the gate."
    assert timeline["source_session_id"] == SESSION_ID

    # no appears_in relations without event pages
    assert wiki_client.relations == []

    # cross-run dedupe input: existing campaign pages fetched once
    assert wiki_client.list_calls == [CAMPAIGN_ID]

    # job row recorded as done with the draft ids
    async with session_factory() as db:
        job = (await db.execute(select(GenerationJob))).scalars().one()
    assert job.status == "done"
    assert job.prompt_version == "v10"
    assert float(job.confidence) == 1.0
    assert _draft_ids_from(job) == {
        PAGE_UUIDS["Aragorn"], PAGE_UUIDS["Moria"], PAGE_UUIDS["Entering Moria"],
    }

    # merged summary persisted for the session page (canned extraction)
    async with session_factory() as db:
        summary = (
            await db.execute(select(SessionSummary))
        ).scalars().one()
    assert summary.session_id == UUID(SESSION_ID)
    assert summary.summary == "The party reaches the gates of Moria."
    assert summary.generation_job_id == job.id
    assert [e["title"] for e in summary.events] == ["Entering Moria"]
    assert summary.timeline_entries[0]["time"] == "00:00:12"
    assert summary.characters[0]["name"] == "Aragorn"
    assert summary.llm_provider == "deepseek"
    assert summary.llm_model == "deepseek/deepseek-chat"

    # content.generated published with the contract payload
    assert len(publisher.events) == 1
    ev = publisher.events[0]
    assert ev.type == "content.generated"
    payload = ev.payload
    assert payload["session_id"] == SESSION_ID
    assert payload["campaign_id"] == CAMPAIGN_ID
    assert payload["generation_job_id"] == str(job.id)
    assert set(payload["draft_ids"]) == {
        PAGE_UUIDS["Aragorn"], PAGE_UUIDS["Moria"], PAGE_UUIDS["Entering Moria"],
    }
    assert payload["confidence"] == 1.0
    assert payload["language"] == "en"
    assert payload["skipped_duplicates"] == []


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
    storage = FakeStorage()
    session_client = FakeSessionClient()
    user_client = FakeUserClient()
    llm = FakeLLM()
    publisher = FakePublisher()

    async with session_factory() as db:
        await process_job(
            make_event(), settings, storage, session_client,
            user_client, wiki_client, llm, publisher, db,
        )

    # no duplicate Moria draft; only the fresh Aragorn page is created. The
    # event page is still drafted (events dedupe against EVENT pages only).
    assert [d["kind"] for d in wiki_client.created] == ["character", "event"]
    assert all(d["title"] != "Moria" for d in wiki_client.created)

    ev = publisher.events[0]
    assert [d["title"] for d in ev.payload["skipped_duplicates"]] == ["Moria"]

    # session still completes normally
    assert session_client.status_calls[-1] == (SESSION_ID, "content_ready", None)


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
    storage = FakeStorage()
    session_client = FakeSessionClient()
    user_client = FakeUserClient()
    llm = FakeLLM()
    publisher = FakePublisher()

    async with session_factory() as db:
        await process_job(
            make_event(), settings, storage, session_client,
            user_client, wiki_client, llm, publisher, db,
        )

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
    async with session_factory() as db:
        await process_job(
            event, settings, FakeStorage(), FakeSessionClient(),
            FakeUserClient(), wiki_client, llm, FakePublisher(), db,
        )

    # transcript + prompt context use the CHARACTER name, never the player's
    assert llm.views[0].startswith("Party (player characters): Strider")
    assert "Strider: We must enter Moria." in llm.views[0]

    # extraction says 'Aragorn' but the alias matches the party -> player page
    character = next(d for d in wiki_client.created if d["kind"] == "character")
    assert character["content_json"]["attributes"] == {"character_type": "player"}


async def test_process_job_pending_assignment_skips(session_factory, settings):
    storage = FakeStorage()
    session_client = FakeSessionClient()
    publisher = FakePublisher()

    async with session_factory() as db:
        await process_job(
            make_event(pending_assignment=True), settings, storage, session_client,
            FakeUserClient(), FakeWikiClient(), FakeLLM(), publisher, db,
        )

    assert session_client.status_calls == []
    assert storage.downloads == []
    assert publisher.events == []
    async with session_factory() as db:
        assert (await db.execute(select(GenerationJob))).scalars().first() is None


async def test_process_job_idempotent_redelivery(session_factory, settings):
    """409 on entering generating_wiki -> ack, no work done."""
    session_client = FakeSessionClient(conflict_on_generating=True)
    storage = FakeStorage()
    publisher = FakePublisher()

    async with session_factory() as db:
        await process_job(
            make_event(), settings, storage, session_client,
            FakeUserClient(), FakeWikiClient(), FakeLLM(), publisher, db,
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
    async with session_factory() as db:
        await process_job(
            event, settings, FakeStorage(), FakeSessionClient(),
            FakeUserClient(), FakeWikiClient(), llm, FakePublisher(), db,
        )

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
    session_client = FakeSessionClient()
    async with session_factory() as db:
        await process_job(
            make_event(), settings, FakeStorage(), session_client,
            FakeUserClient(), wiki_client, llm, FakePublisher(), db,
        )

    # fresh draft -> existing faction (by title); fresh draft -> fresh draft.
    # FakeWikiClient counts len(created) AFTER appending, so the 2nd page
    # (Gimli) gets UUID(int=2).
    gimli_id = str(UUID(int=2))
    relations = sorted(wiki_client.relations, key=lambda rel: rel[2])
    assert relations == [
        (gimli_id, PAGE_UUIDS["Aragorn"], "led_by"),  # Gimli is led by Aragorn
        (PAGE_UUIDS["Aragorn"], "ffffffff-ffff-ffff-ffff-ffffffffffff", "member_of"),
    ]
    # pipeline still completes normally
    assert session_client.status_calls[-1] == (SESSION_ID, "content_ready", None)


async def test_process_job_multi_chunk(session_factory, settings):
    """Small chunk budget -> several chunks, all extracted and merged."""
    settings.chunk_tokens = 30  # tiny budget -> multiple chunks
    llm = FakeLLM()
    async with session_factory() as db:
        await process_job(
            make_event(), settings, FakeStorage(), FakeSessionClient(),
            FakeUserClient(), FakeWikiClient(), llm, FakePublisher(), db,
        )
    assert len(llm.views) > 1


# ------------------------------------------------------------- helpers


def test_build_speaker_map_identified():
    event = make_event()
    # null user_id skipped; values carry user/display/character names
    assert build_speaker_map(event) == {
        "SPEAKER_00": {"user_id": USER_ID, "display_name": None, "character_name": None}
    }


def test_build_speaker_map_assigned():
    event = Event(
        type="speakers.assigned",
        payload={"label": "SPEAKER_01", "user_id": USER_ID, "session_id": SESSION_ID},
    )
    assert build_speaker_map(event) == {
        "SPEAKER_01": {"user_id": USER_ID, "display_name": None, "character_name": None},
    }


def test_build_speaker_map_assigned_userless():
    """A member without a linked user carries its display name in the event."""
    event = Event(
        type="speakers.assigned",
        payload={"label": "SPEAKER_02", "user_id": None,
                 "display_name": "Cedric the Bold", "character_name": "Cedric of the Vale",
                 "session_id": SESSION_ID},
    )
    assert build_speaker_map(event) == {
        "SPEAKER_02": {
            "user_id": None,
            "display_name": "Cedric the Bold",
            "character_name": "Cedric of the Vale",
        },
    }


def test_build_speaker_map_empty():
    assert build_speaker_map(make_event(speakers=[])) == {}
    assert build_speaker_map(Event(type="other", payload={})) == {}


async def test_resolve_speaker_names():
    client = FakeUserClient(names={USER_ID: "Aragorn"})
    names = await resolve_speaker_names(
        {
            "SPEAKER_00": {"user_id": USER_ID, "display_name": None, "character_name": None},
            "SPEAKER_01": {"user_id": "unknown-user", "display_name": None, "character_name": None},
            "SPEAKER_02": {"user_id": None, "display_name": "Cedric the Bold", "character_name": None},
        },
        client,
    )
    # known user -> display name; unknown user -> raw label;
    # userless member -> event display name
    assert names == {
        "SPEAKER_00": "Aragorn",
        "SPEAKER_01": "SPEAKER_01",
        "SPEAKER_02": "Cedric the Bold",
    }


async def test_resolve_speaker_names_prefers_character_names():
    """Wiki convention: party members are labeled by their CHARACTER."""
    client = FakeUserClient(names={USER_ID: "Bobby"})
    names = await resolve_speaker_names(
        {
            "SPEAKER_00": {
                "user_id": USER_ID,
                "display_name": "Bobby",
                "character_name": "Bobblin the Brave",
            },
        },
        client,
    )
    assert names == {"SPEAKER_00": "Bobblin the Brave"}

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
    session_client = FakeSessionClient()
    campaign_client = FakeCampaignClient(dm_user_id=dm_user_id)
    async with session_factory() as db:
        await process_job(
            event, settings, FakeStorage(), session_client,
            FakeUserClient(), wiki_client, llm, FakePublisher(), db,
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
    assert session_client.status_calls[-1] == (SESSION_ID, "content_ready", None)