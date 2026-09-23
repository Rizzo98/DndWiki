"""Shared fixtures: in-memory SQLite DB, fake external clients, canned events."""

from __future__ import annotations

from uuid import UUID

import pytest
from dnd_common import db as dnd_db
from dnd_common.events import Event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — register tables on Base.metadata
from app.clients.session_service import ConflictTransition
from app.core.config import ServiceSettings
from app.speakers import SpeakerReading

SESSION_ID = "11111111-1111-1111-1111-111111111111"
CAMPAIGN_ID = "22222222-2222-2222-2222-222222222222"
USER_ID = "33333333-3333-3333-3333-333333333333"
PAGE_UUIDS = {
    "Aragorn": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "Moria": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    "Entering Moria": "cccccccc-cccc-cccc-cccc-cccccccccccc",
    "Session Summary": "dddddddd-dddd-dddd-dddd-dddddddddddd",
    # the second character of the relation fixture (FakeWikiClient ids pages by
    # creation order, so the 2nd created page is UUID(int=2))
    "Gimli": "00000000-0000-0000-0000-000000000002",
}


def make_event(**overrides) -> Event:
    """speakers.identified with one auto-assigned + one pending speaker."""
    payload = {
        "session_id": SESSION_ID,
        "campaign_id": CAMPAIGN_ID,
        "speakers": [
            {"label": "SPEAKER_00", "user_id": USER_ID, "confidence": 0.91, "status": "auto"},
            {"label": "SPEAKER_01", "user_id": None, "confidence": 0.4, "status": "pending"},
        ],
        "pending_assignment": False,
    }
    payload.update(overrides)
    return Event(type="speakers.identified", payload=payload)


def make_transcript() -> dict:
    """Small transcript: SPEAKER_00 talks, SPEAKER_01 is silent-ish."""
    return {
        "session_id": SESSION_ID,
        "language": "en",
        "model": "large-v3",
        "segments": [
            {"start": 0.0, "end": 3.0, "speaker": "SPEAKER_00", "text": "We must enter Moria."},
            {"start": 3.5, "end": 6.0, "speaker": "SPEAKER_01", "text": "The doors are sealed."},
            {"start": 6.5, "end": 9.0, "speaker": "SPEAKER_00", "text": "Speak friend and enter."},
            {"start": 9.5, "end": 11.0, "speaker": None, "text": ""},  # skipped: empty
            {"start": 12.0, "end": 14.0, "speaker": "SPEAKER_00", "text": "The gate opens."},
        ],
    }


def make_extraction(**overrides) -> dict:
    """One chunk extraction consistent with the canned transcript (v2 shape)."""
    base = {
        "language": "en",
        "session_summary": "The party reaches the gates of Moria.",
        "characters": [
            {
                "name": "Aragorn",
                "aliases": ["Strider"],
                "description": "A ranger of the north.",
                "facts": ["Speaks the password."],
                "session_facts": ["Sings 'friend' at the west-gate door."],
                "mentions": 3,
            }
        ],
        "locations": [
            {
                "name": "Moria",
                "aliases": [],
                "description": "An ancient dwarven mine.",
                "facts": [],
                "session_facts": ["Its west-door opened to the password."],
                "mentions": 2,
            }
        ],
        "events": [
            {
                "title": "Entering Moria",
                "description": "The party passes the gate.",
                "participants": ["Aragorn"],
            }
        ],
        "timeline_entries": [
            {"time": "00:00:12", "summary": "The gate opens.", "characters": ["Aragorn"]}
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- fixtures


@pytest.fixture
async def engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(dnd_db.Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def settings() -> ServiceSettings:
    """Settings for a test run, with the ambient environment made explicit.

    ServiceSettings() reads the process environment, and the repo's .env turns
    ATTRIBUTION_ENABLED on for the live stack - which flips the statuses the
    summary phase accepts (a session at 'speakers_identified' is the ENGINE's to
    attribute, not this service's to summarise). Every phase-1 test then returned
    early and the suite was red locally while green in CI, on a flag that has
    nothing to do with what those tests are about. The fixture pins the mode the
    fakes are written for; the attribution path has its own tests.
    """
    return ServiceSettings(attribution_enabled=False)


class FakeStorage:
    """Records downloads; returns the canned transcript."""

    def __init__(self, transcript: dict | None = None):
        self.downloads: list[tuple[str, str]] = []
        self.transcript = transcript or make_transcript()

    async def read_json(self, bucket: str, key: str) -> dict:
        self.downloads.append((bucket, key))
        return self.transcript


class FakeSessionClient:
    """Records status transitions; can be configured to 409 or report a status.

    'status' is what get_session() reports (the phase-1 precondition check)
    and is kept in sync with the transitions, so a fake behaves like the real
    state machine for a single sequential run.
    """

    def __init__(self, conflict_on_start: bool = False, status: str = "speakers_identified"):
        self.status_calls: list[tuple[str, str, str | None]] = []
        self.conflict_on_start = conflict_on_start
        self.status = status

    async def get_session(self, session_id: str) -> dict:
        return {"id": session_id, "campaign_id": CAMPAIGN_ID, "status": self.status}

    async def update_status(self, session_id: str, status: str, error: str | None = None) -> dict:
        if self.conflict_on_start and status in (
            "summarizing",
            "generating_wiki",
            "applying_wiki",
        ):
            raise ConflictTransition("already moved on")
        self.status_calls.append((session_id, status, error))
        self.status = status
        return {"id": session_id, "status": status}


class FakeUserClient:
    """Resolves user ids -> display names from a fixed map."""

    def __init__(self, names: dict[str, str] | None = None):
        self.calls: list[list[str]] = []
        self.names = names or {USER_ID: "Aragorn"}

    async def display_names(self, user_ids: list[str]) -> dict[str, str]:
        self.calls.append(list(user_ids))
        return {uid: name for uid, name in self.names.items() if uid in user_ids}


class FakeCampaignClient:
    """Returns the campaign DM user id and the roster; records what was asked for."""

    def __init__(
        self,
        dm_user_id: str | None = None,
        members: list[dict] | None = None,
        members_error: Exception | None = None,
    ):
        self.dm_user_id = dm_user_id
        self.calls: list[str] = []
        #: What list_members() answers with (campaign-service's member rows), and
        #: the error that drives the "campaign unreachable" path.
        self.members = members if members is not None else []
        self.members_error = members_error

    async def campaign_dm(self, campaign_id) -> str | None:
        self.calls.append(str(campaign_id))
        return self.dm_user_id

    async def list_members(self, campaign_id) -> list[dict]:
        self.calls.append(str(campaign_id))
        if self.members_error is not None:
            raise self.members_error
        return list(self.members)


class FakeWikiClient:
    """Reads the campaign listing and applies confirmed change sets.

    apply_changes() mimics wiki-service's internal apply: it "creates" the
    pages (deterministic ids by title), "updates" the existing ones and shows
    the timeline entries the change set carries — with the same records the
    old per-call worker produced (created/updated/relations/timeline_upserts),
    so tests can assert on the applied result either way.

    'existing_pages' is what list_campaign_pages() reports back, simulating
    what the campaign wiki already documents (cross-run dedupe input).
    """

    def __init__(self, existing_pages: list[dict] | None = None):
        self.created: list[dict] = []
        self.relations: list[tuple[str, str, str]] = []
        self.list_calls: list[str] = []
        self.existing_pages = existing_pages or []
        self.updated: list[tuple[str, dict]] = []  # (page_id, payload)
        self.timeline_upserts: list[dict] = []
        self.timeline_list_calls: list[str] = []
        self.applied: list[dict] = []  # the change-set payloads received
        self.apply_error: Exception | None = None

    async def list_campaign_pages(self, campaign_id: str) -> list[dict]:
        self.list_calls.append(campaign_id)
        return self.existing_pages

    async def apply_changes(self, payload: dict) -> dict:
        """Stand-in for POST /internal/wiki/changes/apply (published pages)."""
        self.applied.append(payload)
        if self.apply_error is not None:
            raise self.apply_error
        created: list[dict] = []
        updated: list[dict] = []
        for change in payload.get("changes", []):
            if change["action"] == "update":
                self.updated.append(
                    (
                        change["page_id"],
                        {
                            "content_json": change["content_json"],
                            "change_note": "Confirmed change set of session "
                            + str(payload["session_id"]),
                        },
                    )
                )
                updated.append(
                    {
                        "change_id": change["change_id"],
                        "page_id": change["page_id"],
                        "title": change["title"],
                        "kind": change["kind"],
                        "action": "update",
                        "reason": None,
                    }
                )
                if change.get("timeline"):
                    self.timeline_upserts.append(
                        {
                            "campaign_id": payload["campaign_id"],
                            "page_id": change["page_id"],
                            "in_world_date": change["timeline"].get("in_world_date"),
                            "summary": change["timeline"]["summary"],
                            "source_session_id": payload["session_id"],
                            "approved": True,
                        }
                    )
                continue
            page = {
                "campaign_id": payload["campaign_id"],
                "kind": change["kind"],
                "title": change["title"],
                "content_json": change["content_json"],
                "status": "published",
                "visibility": change["visibility"],
                "confidence": change["confidence"],
                "source_session_id": payload["session_id"],
            }
            self.created.append(page)
            page_id = str(PAGE_UUIDS.get(change["title"], UUID(int=len(self.created))))
            created.append(
                {
                    "change_id": change["change_id"],
                    "page_id": page_id,
                    "title": change["title"],
                    "kind": change["kind"],
                    "action": "create",
                    "reason": None,
                }
            )
            if change.get("timeline"):
                self.timeline_upserts.append(
                    {
                        "campaign_id": payload["campaign_id"],
                        "page_id": page_id,
                        "in_world_date": change["timeline"].get("in_world_date"),
                        "summary": change["timeline"]["summary"],
                        "source_session_id": payload["session_id"],
                        "approved": True,
                    }
                )
        # titles resolve against the pages this apply created first, then
        # against what the campaign already documents (the real service does
        # exactly that) — PAGE_UUIDS stands in for the created pages.
        by_title: dict[str, str] = dict(PAGE_UUIDS)
        for page in self.existing_pages:
            for name in [page.get("title"), *(page.get("aliases") or [])]:
                if name:
                    by_title.setdefault(str(name), str(page["id"]))
        for relation in payload.get("relations", []):
            source = by_title.get(str(relation.get("from_title") or ""))
            target = relation.get("to_page_id") or by_title.get(
                str(relation.get("to_title") or "")
            )
            if source and target:
                self.relations.append((source, str(target), relation["relation_type"]))
        return {
            "created": created,
            "updated": updated,
            "skipped": [],
            "timeline_entries": len(self.timeline_upserts),
            "relations_created": len(payload.get("relations", [])),
        }


class FakeLLM:
    """Returns one canned extraction per chunk; records the views.

    'revised' is the PATCH revise_summary() returns (defaults to the narrative it
    was given, i.e. a revision that changes nothing); 'revise_error' drives the
    failure path. 'composed' is the story compose_summary() returns (scene
    blocks), None meaning "the model could not write it".
    """

    def __init__(
        self,
        extractions: list[dict] | None = None,
        error: Exception | None = None,
        revised: dict | None = None,
    ):
        self.views: list[str] = []
        self.extractions = extractions or [make_extraction()]
        self.error = error
        self.revised = revised
        self.revise_calls: list[dict] = []
        self.revise_error: Exception | None = None
        #: What compose_summary() returns (scene blocks); None means "the model
        #: could not do it", which is the case the worker has to survive by
        #: keeping the beats.
        self.composed: list[dict] | None = None
        self.compose_calls: list[dict] = []
        self.compose_error: Exception | None = None
        #: What read_speakers() returns (app.speakers.SpeakerReading). The default
        #: is an EMPTY reading, i.e. "the recording established nothing": every
        #: test that does not care about the cast keeps the views it always had.
        #: 'cast_lines' records what the reading was shown.
        self.reading: SpeakerReading | None = SpeakerReading()
        self.reading_error: Exception | None = None
        self.cast_lines: list[str] = []

    async def extract_many(
        self,
        chunk_views: list[str],
        *,
        concurrency: int,
        out_of_world: list[str] | None = None,
        owned: list | None = None,
    ) -> list[dict]:
        self.views = list(chunk_views)
        self.out_of_world = out_of_world
        #: The slice of the session each chunk narrates (chunking.OwnedPart):
        #: where it starts and how many beats it owes. Recorded so a test can
        #: assert both reach the model instead of trusting they were threaded
        #: through.
        self.owned = list(owned or [])
        if self.error is not None:
            raise self.error
        return [self.extractions[min(i, len(self.extractions) - 1)] for i in range(len(chunk_views))]

    async def read_speakers(self, lines: list[str]):
        """The session-wide cast reading (app/speakers.py).

        'reading' is what the model answers with; None means "could not read it",
        which is the case the worker has to survive by summarising exactly as it
        did before the reading existed.
        """
        self.cast_lines = list(lines)
        if self.reading_error is not None:
            raise self.reading_error
        return self.reading

    async def compose_summary(
        self,
        current: dict,
        *,
        language: str | None = None,
        scenes: list[dict] | None = None,
    ) -> list[dict]:
        self.compose_calls.append(
            {"current": current, "language": language, "scenes": scenes}
        )
        if self.compose_error is not None:
            raise self.compose_error
        return [dict(block) for block in self.composed or []]

    async def revise_summary(
        self,
        current: dict,
        edits: list[dict] | None = None,
        summary_text_override: str | None = None,
    ) -> dict:
        self.revise_calls.append(
            {
                "current": current,
                "edits": edits,
                "summary_text": summary_text_override,
            }
        )
        if self.revise_error is not None:
            raise self.revise_error
        if self.revised is not None:
            return self.revised
        return {
            "session_summary": current.get("summary_blocks")
            or current.get("session_summary")
            or ""
        }


class FakePublisher:
    def __init__(self):
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)