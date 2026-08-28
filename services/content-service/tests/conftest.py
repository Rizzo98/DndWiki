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

SESSION_ID = "11111111-1111-1111-1111-111111111111"
CAMPAIGN_ID = "22222222-2222-2222-2222-222222222222"
USER_ID = "33333333-3333-3333-3333-333333333333"
PAGE_UUIDS = {
    "Aragorn": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "Moria": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    "Entering Moria": "cccccccc-cccc-cccc-cccc-cccccccccccc",
    "Session Summary": "dddddddd-dddd-dddd-dddd-dddddddddddd",
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


def make_extraction() -> dict:
    """One chunk extraction consistent with the canned transcript (v2 shape)."""
    return {
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
    return ServiceSettings()


class FakeStorage:
    """Records downloads; returns the canned transcript."""

    def __init__(self, transcript: dict | None = None):
        self.downloads: list[tuple[str, str]] = []
        self.transcript = transcript or make_transcript()

    async def read_json(self, bucket: str, key: str) -> dict:
        self.downloads.append((bucket, key))
        return self.transcript


class FakeSessionClient:
    """Records status transitions; can be configured to 409."""

    def __init__(self, conflict_on_generating: bool = False):
        self.status_calls: list[tuple[str, str, str | None]] = []
        self.conflict_on_generating = conflict_on_generating

    async def update_status(self, session_id: str, status: str, error: str | None = None) -> dict:
        if self.conflict_on_generating and status == "generating_wiki":
            raise ConflictTransition("already moved on")
        self.status_calls.append((session_id, status, error))
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
    """Returns the campaign DM user id; records the campaign it was asked for."""

    def __init__(self, dm_user_id: str | None = None):
        self.dm_user_id = dm_user_id
        self.calls: list[str] = []

    async def campaign_dm(self, campaign_id) -> str | None:
        self.calls.append(str(campaign_id))
        return self.dm_user_id


class FakeWikiClient:
    """Creates pages/relations/timeline entries; pages get deterministic ids
    by title.

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

    async def list_campaign_pages(self, campaign_id: str) -> list[dict]:
        self.list_calls.append(campaign_id)
        return self.existing_pages

    async def create_page(self, payload: dict) -> dict:
        self.created.append(payload)
        return {
            "id": PAGE_UUIDS.get(payload["title"], str(UUID(int=len(self.created)))),
            "title": payload["title"],
            "kind": payload["kind"],
        }

    async def create_relation(self, page_id: str, related_page_id: str, relation_type: str) -> dict:
        self.relations.append((page_id, related_page_id, relation_type))
        return {"id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"}

    async def list_timeline_events(self, campaign_id: str) -> list[dict]:
        self.timeline_list_calls.append(campaign_id)
        return []

    async def upsert_timeline_event(self, payload: dict) -> dict:
        self.timeline_upserts.append(payload)
        return {
            "event": {
                "id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
                "campaign_id": payload["campaign_id"],
                "page_id": payload["page_id"],
                "approved": False,
            },
            "created": True,
        }

    async def update_page(self, page_id: str, payload: dict) -> dict:
        self.updated.append((page_id, payload))
        return {"id": page_id, "title": "updated", "kind": "event"}


class FakeLLM:
    """Returns one canned extraction per chunk; records the views."""

    def __init__(self, extractions: list[dict] | None = None, error: Exception | None = None):
        self.views: list[str] = []
        self.extractions = extractions or [make_extraction()]
        self.error = error

    async def extract_many(
        self,
        chunk_views: list[str],
        *,
        concurrency: int,
        out_of_world: list[str] | None = None,
    ) -> list[dict]:
        self.views = list(chunk_views)
        self.out_of_world = out_of_world
        if self.error is not None:
            raise self.error
        return [self.extractions[min(i, len(self.extractions) - 1)] for i in range(len(chunk_views))]


class FakePublisher:
    def __init__(self):
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)