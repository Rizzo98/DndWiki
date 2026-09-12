"""session_summaries persistence (worker writes, API reads).

One row per session (unique session_id): the merged LLM extraction plus the
run metadata and the DM REVIEW state, so the session page can render the
summary without digging through wiki drafts.

The summary is the pipeline's intermediate layer:

- `save_summary` writes a fresh DRAFT (revision 1) straight from the merged
  extraction and clears any earlier confirmation — regenerating always sends
  the session back to the DM for review.
- `apply_revision` persists a DM-driven rewrite (feedback applied to the
  previous extraction): revision +1, the feedback appended to edit_history,
  still a draft.
- `confirm_summary` stamps the DM's approval; only from there does the worker
  materialize pages/events (`summary_to_merged` rebuilds the merger-shaped
  dict the draft builders consume).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import REVIEW_CONFIRMED, REVIEW_DRAFT, SessionSummary


def summary_lines(text: str) -> list[str]:
    """The reviewable lines of a summary (newline separated, blank-free)."""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def summary_to_merged(row: SessionSummary) -> dict[str, Any]:
    """Rebuild the merger-shaped extraction from a persisted summary row.

    The wiki phase (build_page_drafts / build_event_drafts) consumes the same
    dict shape merge_extractions() produces, so the confirmed summary can be
    materialized without re-reading the transcript.
    """
    return {
        "language": row.language or "",
        "session_summary": row.summary or "",
        "characters": row.characters or [],
        "locations": row.locations or [],
        "events": row.events or [],
        "timeline_entries": row.timeline_entries or [],
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "party_characters": row.party_characters or [],
    }


async def _get(db: AsyncSession, session_id: UUID) -> SessionSummary | None:
    return await db.scalar(
        select(SessionSummary).where(SessionSummary.session_id == session_id)
    )


async def save_summary(
    db: AsyncSession,
    session_id: UUID,
    *,
    generation_job_id: UUID,
    merged: dict[str, Any],
    llm_provider: str,
    llm_model: str,
    prompt_version: str,
    party_characters: list[str] | None = None,
) -> SessionSummary:
    """Persist (or overwrite) the merged extraction as a fresh DRAFT summary."""
    row = await _get(db, session_id)
    if row is None:
        row = SessionSummary(session_id=session_id)
        db.add(row)
    row.generation_job_id = generation_job_id
    row.summary = (merged.get("session_summary") or "").strip()
    row.language = (merged.get("language") or "").strip() or None
    row.party_characters = list(party_characters or [])
    row.characters = merged.get("characters") or []
    row.locations = merged.get("locations") or []
    row.events = merged.get("events") or []
    row.timeline_entries = merged.get("timeline_entries") or []
    row.confidence = merged.get("confidence")
    row.llm_provider = llm_provider
    row.llm_model = llm_model
    row.prompt_version = prompt_version
    # A fresh draft always invalidates an earlier confirmation: the DM reviews
    # the new text before anything is written to the wiki.
    row.review_status = REVIEW_DRAFT
    row.revision = 1
    row.confirmed_at = None
    row.confirmed_by = None
    row.edit_history = []
    await db.commit()
    await db.refresh(row)
    return row


async def apply_revision(
    db: AsyncSession,
    session_id: UUID,
    *,
    generation_job_id: UUID,
    merged: dict[str, Any],
    llm_provider: str,
    llm_model: str,
    prompt_version: str,
    edits: list[dict[str, Any]] | None = None,
) -> SessionSummary:
    """Persist a DM-driven rewrite of the existing draft summary (revision +1).

    The rewritten extraction replaces the summary lines and the entity/event
    payloads; the review state stays 'draft' so the DM keeps iterating until
    they confirm. Every feedback request that produced this revision is
    appended to the row's edit_history.
    """
    row = await _get(db, session_id)
    if row is None:
        raise ValueError(f"session {session_id} has no summary to revise")
    row.generation_job_id = generation_job_id
    row.summary = (merged.get("session_summary") or "").strip()
    if merged.get("language"):
        row.language = str(merged["language"]).strip() or row.language
    if merged.get("characters") is not None:
        row.characters = merged.get("characters") or []
    if merged.get("locations") is not None:
        row.locations = merged.get("locations") or []
    if merged.get("events") is not None:
        row.events = merged.get("events") or []
    if merged.get("timeline_entries") is not None:
        row.timeline_entries = merged.get("timeline_entries") or []
    revised_confidence = merged.get("confidence")
    if revised_confidence is not None:
        row.confidence = revised_confidence
    row.llm_provider = llm_provider
    row.llm_model = llm_model
    row.prompt_version = prompt_version
    row.review_status = REVIEW_DRAFT
    row.revision = int(row.revision or 1) + 1
    if edits:
        row.edit_history = [*(row.edit_history or []), *edits]
    await db.commit()
    await db.refresh(row)
    return row


async def confirm_summary(
    db: AsyncSession, session_id: UUID, *, confirmed_by: UUID | None
) -> SessionSummary | None:
    """Stamp the DM's confirmation of the draft summary."""
    row = await _get(db, session_id)
    if row is None:
        return None
    row.review_status = REVIEW_CONFIRMED
    row.confirmed_at = datetime.now(UTC)
    row.confirmed_by = confirmed_by
    await db.commit()
    await db.refresh(row)
    return row


async def latest_summary_for_session(db: AsyncSession, session_id: UUID) -> SessionSummary | None:
    """The persisted summary for a session (or None when generation never ran)."""
    return await _get(db, session_id)
