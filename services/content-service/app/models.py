"""SQLAlchemy models for the dnd_content database (database-per-service).

Mirrors docs/data-model.md: 'generation_jobs' records one LLM generation run
per session (provider/model/prompt_version, the draft page ids created, and
the overall confidence). 'session_summaries' persists the merged LLM
extraction (summary paragraph + characters/locations/events/timeline) so the
session page can show it without digging through wiki drafts.

JSON columns use JSONB on Postgres and plain JSON in SQLite test runs so the
models stay portable.
"""

import uuid
from datetime import datetime

from dnd_common.db import Base
from sqlalchemy import JSON, DateTime, Numeric, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class GenerationJob(Base):
    """One LLM wiki-draft generation run for a session."""

    __tablename__ = "generation_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    # queued | running | done | failed
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    llm_provider: Mapped[str | None] = mapped_column(String(64))
    llm_model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    # draft page ids created by this run (pending_review pages)
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

    One row per session: regenerating the wiki drafts overwrites the previous
    summary (unique session_id). The JSON payloads mirror the merged shape from
    app/merger.py (characters/locations/events/timeline_entries + the summary
    paragraph and the run metadata).
    """

    __tablename__ = "session_summaries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True, index=True)
    generation_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
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
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    llm_provider: Mapped[str | None] = mapped_column(String(64))
    llm_model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
