"""wiki_change_sets persistence: the proposed-changes review layer.

The worker writes the set once (from the confirmed summary), the DM edits it
through the plan API, and the apply phase reads it back and reports success.
Only 'draft' sets are editable: a set that is being applied (or already
applied) is frozen, so what the DM confirmed is exactly what gets written.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    PLAN_APPLIED,
    PLAN_APPLYING,
    PLAN_DRAFT,
    REVIEW_CONFIRMED,
    SessionSummary,
    WikiChangeSet,
)

#: Fields of a change the DM may edit (everything else is pipeline-owned).
EDITABLE_CHANGE_FIELDS = ("title", "content_json", "visibility", "timeline", "dropped")
ALLOWED_VISIBILITIES = ("public", "dm_only", "hidden")


class PlanEditError(ValueError):
    """The submitted review edits do not match the stored change set."""


async def get_plan(db: AsyncSession, session_id: UUID) -> WikiChangeSet | None:
    return await db.scalar(
        select(WikiChangeSet).where(WikiChangeSet.session_id == session_id)
    )


async def save_plan(
    db: AsyncSession,
    session_id: UUID,
    *,
    summary_id: UUID | None,
    generation_job_id: UUID | None,
    change_set: dict[str, list[dict[str, Any]]],
    language: str | None = None,
) -> WikiChangeSet:
    """Persist (or overwrite) the proposed change set of a session as a draft."""
    row = await get_plan(db, session_id)
    if row is None:
        row = WikiChangeSet(session_id=session_id)
        db.add(row)
    row.summary_id = summary_id
    row.generation_job_id = generation_job_id
    row.status = PLAN_DRAFT
    row.language = (language or "").strip() or None
    row.changes = change_set.get("changes") or []
    row.relations = change_set.get("relations") or []
    row.skipped = change_set.get("skipped") or []
    row.confirmed_at = None
    row.confirmed_by = None
    row.applied_at = None
    row.error = None
    await db.commit()
    await db.refresh(row)
    return row


async def confirm_plan(
    db: AsyncSession, session_id: UUID, *, confirmed_by: UUID | None
) -> WikiChangeSet | None:
    """Stamp the DM's confirmation of the proposed change set."""
    row = await get_plan(db, session_id)
    if row is None:
        return None
    row.confirmed_at = datetime.now(UTC)
    row.confirmed_by = confirmed_by
    row.error = None
    await db.commit()
    await db.refresh(row)
    return row


def _validate_change(stored: dict[str, Any], submitted: dict[str, Any]) -> dict[str, Any]:
    """Apply the DM's edits of ONE change on top of the stored (pipeline) one.

    Identity and provenance stay pipeline-owned ('id', 'action', 'kind',
    'page_id', 'before', 'after.confidence'): the DM edits WHAT the change
    says, never which page it targets.
    """
    updated = dict(stored)
    if "title" in submitted and submitted["title"] is not None:
        title = str(submitted["title"]).strip()
        if not title or len(title) > 255:
            raise PlanEditError(f"change {stored.get('id')}: title must be 1-255 characters")
        updated["title"] = title
        updated["after"] = {**(updated.get("after") or {}), "title": title}

    after_patch = submitted.get("after") or {}
    if "content_json" in after_patch and after_patch["content_json"] is not None:
        content = after_patch["content_json"]
        if not isinstance(content, dict):
            raise PlanEditError(f"change {stored.get('id')}: content_json must be an object")
        updated["after"] = {**(updated.get("after") or {}), "content_json": content}
    if "visibility" in after_patch and after_patch["visibility"] is not None:
        visibility = str(after_patch["visibility"])
        if visibility not in ALLOWED_VISIBILITIES:
            raise PlanEditError(
                f"change {stored.get('id')}: visibility must be one of "
                + ", ".join(ALLOWED_VISIBILITIES)
            )
        updated["after"] = {**(updated.get("after") or {}), "visibility": visibility}

    if "timeline" in submitted:
        timeline = submitted["timeline"]
        if timeline is None:
            updated["timeline"] = None
        else:
            if not isinstance(timeline, dict):
                raise PlanEditError(f"change {stored.get('id')}: timeline must be an object")
            summary = str(timeline.get("summary") or "").strip()
            if not summary or len(summary) > 2000:
                raise PlanEditError(
                    f"change {stored.get('id')}: timeline summary must be 1-2000 characters"
                )
            in_world_date = timeline.get("in_world_date")
            if in_world_date is not None:
                in_world_date = str(in_world_date).strip()[:256] or None
            updated["timeline"] = {"summary": summary, "in_world_date": in_world_date}

    if "dropped" in submitted and submitted["dropped"] is not None:
        updated["dropped"] = bool(submitted["dropped"])
    return updated


async def replace_reviewable(
    db: AsyncSession,
    session_id: UUID,
    *,
    changes: list[dict[str, Any]] | None = None,
    relations: list[dict[str, Any]] | None = None,
) -> WikiChangeSet:
    """Persist the DM's review edits (only editable fields, only while draft)."""
    row = await get_plan(db, session_id)
    if row is None:
        raise PlanEditError(f"session {session_id} has no proposed change set")
    if row.status != PLAN_DRAFT:
        raise PlanEditError(
            f"the change set is '{row.status}'; it can only be edited while it is a draft"
        )

    stored_changes = {str(c.get("id")): c for c in (row.changes or [])}
    if changes is not None:
        submitted_ids = [str(c.get("id") or "") for c in changes]
        unknown = [i for i in submitted_ids if i not in stored_changes]
        if unknown:
            raise PlanEditError("unknown change id(s): " + ", ".join(unknown))
        patches = {str(c.get("id")): c for c in changes}
        row.changes = [
            _validate_change(stored, patches[str(stored.get("id"))])
            if str(stored.get("id")) in patches
            else stored
            for stored in (row.changes or [])
        ]

    if relations is not None:
        stored_relations = {str(r.get("id")): r for r in (row.relations or [])}
        unknown = [
            str(r.get("id") or "") for r in relations if str(r.get("id")) not in stored_relations
        ]
        if unknown:
            raise PlanEditError("unknown relation id(s): " + ", ".join(unknown))
        patches = {str(r.get("id")): r for r in relations}
        row.relations = [
            {**stored, "dropped": bool(patches[str(stored.get("id"))].get("dropped"))}
            if str(stored.get("id")) in patches
            else stored
            for stored in (row.relations or [])
        ]

    await db.commit()
    await db.refresh(row)
    return row


async def mark_applying(db: AsyncSession, session_id: UUID) -> WikiChangeSet | None:
    """Freeze the set while the worker writes it to the wiki."""
    row = await get_plan(db, session_id)
    if row is None:
        return None
    row.status = PLAN_APPLYING
    await db.commit()
    await db.refresh(row)
    return row


async def mark_applied(
    db: AsyncSession, session_id: UUID, *, generation_job_id: UUID | None = None
) -> WikiChangeSet | None:
    """The change set exists in the wiki now."""
    row = await get_plan(db, session_id)
    if row is None:
        return None
    row.status = PLAN_APPLIED
    if generation_job_id is not None:
        row.generation_job_id = generation_job_id
    row.applied_at = datetime.now(UTC)
    row.error = None
    await db.commit()
    await db.refresh(row)
    return row


async def mark_draft(
    db: AsyncSession, session_id: UUID, *, error: str | None = None
) -> WikiChangeSet | None:
    """A failed apply goes back to review, so the DM can fix and retry."""
    row = await get_plan(db, session_id)
    if row is None:
        return None
    row.status = PLAN_DRAFT
    row.error = error
    await db.commit()
    await db.refresh(row)
    return row


async def confirmed_summary(db: AsyncSession, session_id: UUID) -> SessionSummary | None:
    """The session's summary, but only when the DM already confirmed it."""
    summary = await db.scalar(
        select(SessionSummary).where(SessionSummary.session_id == session_id)
    )
    if summary is None or summary.review_status != REVIEW_CONFIRMED:
        return None
    return summary
