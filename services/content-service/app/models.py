"""SQLAlchemy models for the dnd_content database (database-per-service).

Mirrors docs/data-model.md: 'generation_jobs' records one LLM run per session
(provider/model/prompt_version, the phase it covered, the draft page ids
created, and the overall confidence). 'session_summaries' persists the merged
LLM extraction (the narrative + characters/locations/events/timeline) so the
session page can show it without digging through wiki drafts.

The session summary is the REVIEW LAYER of the pipeline: it is written as a
draft ('review_status=draft'), the DM reviews/edit it on the session page and
only the confirmed summary is turned into wiki pages and timeline events
(review_status='confirmed', revision counts the DM-driven rewrites).

JSON columns use JSONB on Postgres and plain JSON in SQLite test runs so the
models stay portable.
"""

import uuid
from datetime import datetime

from dnd_common.db import Base
from sqlalchemy import JSON, DateTime, Integer, Numeric, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

#: SessionSummary.review_status values.
REVIEW_DRAFT = "draft"
REVIEW_CONFIRMED = "confirmed"

#: GenerationJob.phase values: which part of the pipeline the run covered.
PHASE_SUMMARY = "summary"  # transcript -> reviewable draft summary
PHASE_WIKI = "wiki"  # confirmed summary -> proposed change set
PHASE_APPLY = "apply"  # confirmed change set -> pages/events in the wiki

#: WikiChangeSet.status values.
PLAN_DRAFT = "draft"  # proposed, awaiting the DM's review
PLAN_APPLYING = "applying"  # the DM confirmed it; the wiki is being written
PLAN_APPLIED = "applied"  # written; the pages/events exist in the wiki

#: CampaignNote.status values (the DM toolkit's planning notes).
NOTE_DRAFT = "draft"  # still being written; kept out of the way
NOTE_READY = "ready"  # the DM considers it settled and may plan from it

#: NotePlan.status values: the campaign's note -> wiki change set. It shares
#: the session change set's vocabulary (draft/applying/applied) and adds the
#: two states only a note plan needs, because nothing else traces its progress:
#: the row IS the job. The API flips it to 'generating' before it publishes the
#: event and the worker flips it back to 'draft' (or 'failed'), so the plan page
#: can poll one row instead of a separate generation_jobs record.
NOTE_PLAN_GENERATING = "generating"
NOTE_PLAN_FAILED = "failed"


class GenerationJob(Base):
    """One LLM generation run for a session (summary draft, rewrite or wiki)."""

    __tablename__ = "generation_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    # queued | running | done | failed
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    # summary | wiki | apply: the summary phase distills the transcript
    # (draft or DM-feedback rewrite), the wiki phase turns the confirmed
    # summary into a PROPOSED change set, and the apply phase writes the
    # change set the DM confirmed into the wiki.
    phase: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PHASE_SUMMARY, server_default=PHASE_SUMMARY
    )
    llm_provider: Mapped[str | None] = mapped_column(String(64))
    llm_model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    # page ids created by this run; empty for the summary and wiki phases,
    # which create no page at all (the apply phase creates the confirmed ones).
    draft_ids: Mapped[list | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=False,
        default=list,
        server_default="[]",
    )
    # 0..1 overall confidence of the merged extraction
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SessionSummary(Base):
    """Merged LLM extraction for a session, persisted for the session page.

    One row per session (unique session_id). The row is the intermediate layer
    between the transcript and the wiki: 'summary' holds the reviewable
    NARRATIVE as plain text and 'summary_blocks' the same story in scene
    blocks ([{"location", "text"}], see app/summary.py) the DM highlights
    portions of and corrects, the JSON payloads mirror the merged shape from
    app/merger.py (characters/locations/events/timeline_entries), and
    review_status tracks whether the DM already confirmed it. A DM-driven
    rewrite bumps 'revision' and appends to 'edit_history'; a confirm stamps
    review_status/confirmed_at/confirmed_by and freezes the row until a new
    generation overwrites it.
    """

    __tablename__ = "session_summaries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True, index=True)
    generation_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # The same narrative in scene blocks: [{location, text}] (app/summary.py).
    # Rows written before the blocks existed carry [] here and are rendered
    # from 'summary' alone.
    summary_blocks: Mapped[list | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list, server_default="[]"
    )
    # Beats that look like one moment the session recorded twice, as the
    # pipeline found them: [{score, first: {text, from, to}, second: {...}}]
    # (app/conflicts.py). A FLAG for the DM, not a question: nothing has to be
    # answered, and an empty list - the normal case - renders nothing.
    conflicts: Mapped[list | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list, server_default="[]"
    )
    # Transcript language the extraction was written in (draft page language).
    language: Mapped[str | None] = mapped_column(String(16))
    # Party CHARACTER names resolved at extraction time: the wiki phase tags
    # player characters from this list without re-reading the speaker map.
    party_characters: Mapped[list | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list, server_default="[]"
    )
    characters: Mapped[list | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list, server_default="[]"
    )
    locations: Mapped[list | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list, server_default="[]"
    )
    events: Mapped[list | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list, server_default="[]"
    )
    timeline_entries: Mapped[list | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list, server_default="[]"
    )
    # draft | confirmed — only a confirmed summary may become wiki pages.
    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=REVIEW_DRAFT, server_default=REVIEW_DRAFT
    )
    # 1 for the first draft, +1 per DM-driven rewrite.
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # [{targets: [summary line, ...], instruction: str, requested_by: uuid,
    #   created_at: iso}] — the review feedback that produced this revision.
    edit_history: Mapped[list | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list, server_default="[]"
    )
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    llm_provider: Mapped[str | None] = mapped_column(String(64))
    llm_model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

class WikiChangeSet(Base):
    """The PROPOSED wiki changes of a session, reviewable before anything is written.

    The confirmed session summary is turned into a change set: one entry per
    page to create, page to update and timeline entry to write, each carrying
    the payload to apply ('after') plus, for updates, the current state of the
    page ('before') so the session page can render a per-field diff — the
    'git status' of the session.

    Nothing reaches the wiki until the DM confirms the set: 'draft' is what
    the DM reviews/edits/drops, 'applying' means the worker is writing it, and
    'applied' means the pages and timeline entries exist. A failed apply flips
    the row back to 'draft' so the DM can fix an item and confirm again.

    One row per session (unique session_id): a session produces a single
    change set.
    """

    __tablename__ = "wiki_change_sets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True, index=True)
    summary_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    generation_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # draft | applying | applied
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PLAN_DRAFT, server_default=PLAN_DRAFT
    )
    # Transcript language of the drafted text (recorded for the UI/audit).
    language: Mapped[str | None] = mapped_column(String(16))
    # [{'id', 'action': create|update, 'kind', 'title', 'page_id', 'before',
    #   'after': {title, content_json, visibility, confidence},
    #   'timeline': {summary, in_world_date} | None, 'dropped': bool}]
    changes: Mapped[list | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list, server_default="[]"
    )
    # [{'id', 'from_title', 'to_title', 'to_page_id', 'relation_type',
    #   'dropped'}]: cross-references proposed with the pages above.
    relations: Mapped[list | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list, server_default="[]"
    )
    # Entities the campaign already documents (exact title/alias match): NOT
    # changes, just context for the DM ('skipped': [{title, kind, matched_title}]).
    skipped: Mapped[list | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list, server_default="[]"
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CampaignNote(Base):
    """A DM's free-form planning note (the 'plan' toolkit).

    The session pipeline reads what the TABLE said; a note is what the DM has in
    mind before anyone sits down — the world they are setting up, or the ground
    they are preparing between two sessions. It is the only content in the
    platform a human writes from scratch, so it is plain text: no schema, no
    chunks, and no review layer of its own. The review happens later, on the
    change set a set of notes is turned into.

    'status' is the DM's own bookkeeping, not a pipeline state: 'draft' while
    they are still writing it, 'ready' once they consider it settled. Only the
    note page reads it — planning does not require it, because the DM picks the
    notes to plan from explicitly, note by note.
    """

    __tablename__ = "campaign_notes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # The note itself, as the DM typed it. May carry '@slug' page references
    # (the editor's autocomplete writes them): they are notes for a human to
    # read, and the campaign's own renderer links them like any other prose.
    body: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=NOTE_DRAFT, server_default=NOTE_DRAFT
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class NotePlan(Base):
    """The PROPOSED wiki changes a DM built from their notes (one per campaign).

    Same review shape as a session's change set (WikiChangeSet): one entry per
    page to create or update, carrying the payload to write ('after') plus, for
    updates, the page's current state ('before') so the plan page can render a
    diff. Nothing reaches the wiki before the DM confirms it, and the confirmed
    set is written through wiki-service's internal apply endpoint, which creates
    the pages published.

    Unlike a session — which has a state machine of its own living in
    session-service — nothing else carries the progress of a note plan, so this
    row does: 'status' goes generating -> draft -> applying -> applied, and a
    failure parks it on 'failed' with 'error' explaining what happened. That is
    why there is no generation_jobs row here: a job table exists to track work a
    session owns, and this work is owned by the plan itself.

    One row per campaign (unique campaign_id): a campaign has ONE plan under
    construction, which is what makes regenerating an update rather than a
    second, competing proposal. Regenerating from a different set of notes
    overwrites the draft, and a set that was already applied slides back to
    'draft' as the next proposal — the pages it wrote keep their own history in
    the wiki's page_versions.
    """

    __tablename__ = "note_plans"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True, index=True)
    # generating | draft | applying | applied | failed
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=NOTE_PLAN_GENERATING, server_default=NOTE_PLAN_GENERATING
    )
    # The notes this proposal was built from, kept so the review can say what it
    # read and a regeneration can be repeated over the same selection.
    note_ids: Mapped[list | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list, server_default="[]"
    )
    llm_provider: Mapped[str | None] = mapped_column(String(64))
    llm_model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    # The language the notes were written in (drafted page language).
    language: Mapped[str | None] = mapped_column(String(16))
    # [{id, action: create|update, kind, title, page_id, before, after, dropped}]
    changes: Mapped[list | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list, server_default="[]"
    )
    # [{id, from_title, to_title, to_page_id, relation_type, dropped}]
    relations: Mapped[list | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list, server_default="[]"
    )
    # Notes that produced nothing: [{title, reason}] - context for the DM, never
    # a change to review, so a note about nothing does not look like a failure.
    skipped: Mapped[list | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list, server_default="[]"
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
