"""Forgetting a session: the rows content-service holds for it.

Deleting a session is session-service's call (it owns the session record), but
content-service holds the session's draft summary, its generation jobs and its
proposed change set. They are purged here so a deleted session leaves nothing
behind — and the purge REFUSES while the session's content is already in the
wiki: those pages would outlive the session that produced them.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GenerationJob, SessionSummary, WikiChangeSet


async def purge_session(db: AsyncSession, session_id: UUID) -> dict[str, int]:
    """Delete every row content-service holds for a session (idempotent).

    Returns how many rows of each kind were removed, so the caller can report
    what the deletion actually discarded.
    """
    jobs = await db.execute(delete(GenerationJob).where(GenerationJob.session_id == session_id))
    summaries = await db.execute(
        delete(SessionSummary).where(SessionSummary.session_id == session_id)
    )
    change_sets = await db.execute(
        delete(WikiChangeSet).where(WikiChangeSet.session_id == session_id)
    )
    await db.commit()
    return {
        "jobs": jobs.rowcount or 0,
        "summaries": summaries.rowcount or 0,
        "change_sets": change_sets.rowcount or 0,
    }
