"""Pydantic request/response models for the wiki-service API."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# The six wiki categories. 'event' pages back the campaign timeline: every
# timeline event links to an event page (page_id), and the LLM pipeline
# drafts them from world-significant moments of a session.
PAGE_KIND = Literal["character", "location", "faction", "item", "quest", "event"]
PAGE_STATUS = Literal["draft", "pending_review", "published", "archived"]
PAGE_VISIBILITY = Literal["public", "dm_only", "hidden"]

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
RELATION_TYPE_PATTERN = r"^[a-z][a-z0-9_]*$"


# ------------------------------------------------------------- pages


class PageCreate(BaseModel):
    """Payload for POST /api/wiki/pages.

    Users (DM) and the content-service (drafts via a service token) both use
    this endpoint; the API layer restricts which statuses each may set.
    """

    campaign_id: UUID
    kind: PAGE_KIND
    title: str = Field(min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=255, pattern=SLUG_PATTERN)
    content_json: dict[str, Any] = Field(default_factory=dict)
    status: PAGE_STATUS = "draft"
    visibility: PAGE_VISIBILITY = "public"
    # 0..1 confidence from the LLM draft
    confidence: float | None = Field(default=None, ge=0, le=1)
    source_session_id: UUID | None = None
    change_note: str | None = Field(default=None, max_length=500)


class PageUpdate(BaseModel):
    """Payload for PATCH /api/wiki/pages/{id} (DM edits content/metadata).

    Status transitions have their own endpoints (approve/archive); edits here
    only touch the content and identity fields.
    """

    title: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=255, pattern=SLUG_PATTERN)
    content_json: dict[str, Any] | None = None
    change_note: str | None = Field(default=None, max_length=500)


class VisibilityUpdate(BaseModel):
    """Payload for PATCH /api/wiki/pages/{id}/visibility (DM only)."""

    visibility: PAGE_VISIBILITY


class PageOut(BaseModel):
    """Full page representation (detail views)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    campaign_id: UUID
    kind: str
    title: str
    slug: str
    content_json: dict[str, Any]
    status: str
    visibility: str
    confidence: float | None
    source_session_id: UUID | None
    created_by: UUID | None
    updated_by: UUID | None
    created_at: datetime
    updated_at: datetime
    # Short-lived presigned URL for the character portrait (filled by the API
    # layer from content_json.image_uri; never persisted).
    image_url: str | None = None


class PageSummaryOut(BaseModel):
    """Lightweight page representation for list views (no body)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    campaign_id: UUID
    kind: str
    title: str
    slug: str
    status: str
    visibility: str
    confidence: float | None
    source_session_id: UUID | None
    created_at: datetime
    updated_at: datetime


class ExistingPageOut(BaseModel):
    """Flat page view for internal consumers (content-service dedupe).

    content_json is included so the content pipeline can MERGE new facts into
    an existing event page instead of replacing it (longest description wins,
    participants union, missing attributes filled).
    """

    id: UUID
    title: str
    slug: str
    kind: str
    status: str
    aliases: list[str] = Field(default_factory=list)
    content_json: dict[str, Any] | None = None


# ------------------------------------------------------------- versions


class PageVersionOut(BaseModel):
    """A single immutable content snapshot (version_no is per-page, 1-based)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    page_id: UUID
    version_no: int
    content_json: dict[str, Any]
    change_note: str | None
    created_by: UUID | None
    created_at: datetime


# ------------------------------------------------------------- relations


class PageRelationCreate(BaseModel):
    """Payload for POST /api/wiki/pages/{id}/relations (DM proposes a link)."""

    related_page_id: UUID
    relation_type: str = Field(min_length=1, max_length=32, pattern=RELATION_TYPE_PATTERN)


class PageRelationOut(BaseModel):
    """A proposed relation plus the related page's title/slug for display."""

    id: UUID
    page_id: UUID
    related_page_id: UUID
    relation_type: str
    created_at: datetime
    related_title: str | None = None
    related_slug: str | None = None


# ------------------------------------------------------------- timeline


class TimelineEventCreate(BaseModel):
    """Payload for POST /api/wiki/timeline (DM creates an entry).

    approved defaults to True: the DM is the one creating it. LLM-proposed
    events (approved=False) would come through the content-service later.
    """

    campaign_id: UUID
    page_id: UUID | None = None
    in_world_date: str | None = Field(default=None, max_length=64)
    summary: str = Field(min_length=1, max_length=2000)
    source_session_id: UUID | None = None
    approved: bool = True


class TimelineEventUpdate(BaseModel):
    """Payload for PATCH /api/wiki/timeline/{id} (DM edits/approves)."""

    page_id: UUID | None = None
    in_world_date: str | None = Field(default=None, max_length=64)
    summary: str | None = Field(default=None, min_length=1, max_length=2000)
    approved: bool | None = None


class TimelineEventOut(BaseModel):
    """A campaign timeline entry as exposed to the UI."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    campaign_id: UUID
    page_id: UUID | None
    # Title/slug of the linked event page (filled by the API layer, never
    # persisted on the event itself) so the UI can render a clickable title.
    page_title: str | None = None
    page_slug: str | None = None
    in_world_date: str | None
    summary: str
    approved: bool
    source_session_id: UUID | None
    created_at: datetime


# ------------------------------------------------------------- internal timeline


class TimelineUpsert(BaseModel):
    """Payload for POST /internal/wiki/timeline/upsert (service token).

    Creates the timeline entry for an event page (approved=False, pending DM
    approval) or, when one already exists for that page, updates its summary
    and in-world date with the freshly extracted information.
    """

    campaign_id: UUID
    page_id: UUID
    in_world_date: str | None = Field(default=None, max_length=64)
    summary: str = Field(min_length=1, max_length=2000)
    source_session_id: UUID | None = None


class TimelineUpsertOut(BaseModel):
    """Internal response: the (possibly updated) entry + whether it is new."""

    event: TimelineEventOut
    created: bool
