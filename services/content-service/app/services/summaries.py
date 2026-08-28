"""session_summaries persistence (worker writes, API reads).

One row per session (unique session_id): the merged LLM extraction plus the
run metadata, so the session page can render the summary without digging
through wiki drafts. A regeneration overwrites the previous row.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SessionSummary


async def save_summary(
    db: AsyncSession,
    session_id: UUID,
    *,
    generation_job_id: UUID,
    merged: dict[str, Any],
    llm_provider: str,
    llm_model: str,
    prompt_version: str,
) -> SessionSummary:
    """Persist (or overwrite) the merged extraction for a session."""
    row = await db.scalar(
        select(SessionSummary).where(SessionSummary.session_id == session_id)
    )
    if row is None:
        row = SessionSummary(session_id=session_id)
        db.add(row)
    row.generation_job_id = generation_job_id
    row.summary = (merged.get("session_summary") or "").strip()
    row.characters = merged.get("characters") or []
    row.locations = merged.get("locations") or []
    row.events = merged.get("events") or []
    row.timeline_entries = merged.get("timeline_entries") or []
    row.confidence = merged.get("confidence")
    row.llm_provider = llm_provider
    row.llm_model = llm_model
    row.prompt_version = prompt_version
    await db.commit()
    await db.refresh(row)
    return row


async def latest_summary_for_session(db: AsyncSession, session_id: UUID) -> SessionSummary | None:
    """The persisted summary for a session (or None when generation never ran)."""
    return await db.scalar(
        select(SessionSummary).where(SessionSummary.session_id == session_id)
    )
