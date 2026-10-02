"""note_plans persistence: the proposed wiki changes a DM built from their notes.

The same review layer as a session's change set (app/services/plans.py), with
one structural difference: a note plan has no session behind it, so nothing else
tracks its progress. THIS ROW IS THE JOB. The API stamps it 'generating' before
it publishes the event, the worker moves it to 'draft' when a proposal exists
(or 'failed' with the reason), and the plan page polls that one row.

Only 'draft' sets are editable: a set being applied, or already applied, is
frozen so what the DM confirmed is exactly what gets written.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    NOTE_PLAN_FAILED,
    NOTE_PLAN_GENERATING,
    PLAN_APPLIED,
    PLAN_APPLYING,
    PLAN_DRAFT,
    NotePlan,
)

# The DM's review edits are validated by the SESSION plan's rules: a change is
# a change, and one set of rules for what a reviewing DM may rewrite is what
# stops the two review screens from accepting different things.
from app.services.plans import PlanEditError, validate_change


async def get_plan(db: AsyncSession, campaign_id: UUID) -> NotePlan | None:
    """The campaign's plan (at most one: the row is unique per campaign)."""
    return await db.scalar(select(NotePlan).where(NotePlan.campaign_id == campaign_id))


async def start_generation(
    db: AsyncSession,
    campaign_id: UUID,
    *,
    note_ids: list[UUID],
    provider: str,
    model: str,
    prompt_version: str,
) -> NotePlan:
    """Open a generation run: 'generating', with the selection it will read.

    The row is created (or reused) BEFORE the event is published, so the plan
    page can show "reading your notes…" from the moment the DM clicks. The
    previous proposal's changes are cleared here: they are about to be replaced,
    and leaving them visible under a spinner would let the DM review a set that
    is no longer the one being generated.
    """
    row = await get_plan(db, campaign_id)
    if row is None:
        row = NotePlan(campaign_id=campaign_id)
        db.add(row)
    row.status = NOTE_PLAN_GENERATING
    row.note_ids = [str(note_id) for note_id in note_ids]
    row.llm_provider = provider
    row.llm_model = model
    row.prompt_version = prompt_version
    row.changes = []
    row.relations = []
    row.skipped = []
    row.confirmed_at = None
    row.confirmed_by = None
    row.applied_at = None
    row.error = None
    await db.commit()
    await db.refresh(row)
    return row


async def save_generation(
    db: AsyncSession,
    campaign_id: UUID,
    *,
    change_set: dict[str, list[dict[str, Any]]],
    language: str | None = None,
) -> NotePlan:
    """Store the proposal the worker built and hand it to the DM ('draft')."""
    row = await get_plan(db, campaign_id)
    if row is None:
        row = NotePlan(campaign_id=campaign_id)
        db.add(row)
    row.status = PLAN_DRAFT
    row.language = (language or "").strip() or None
    row.changes = change_set.get("changes") or []
    row.relations = change_set.get("relations") or []
    row.skipped = change_set.get("skipped") or []
    row.error = None
    await db.commit()
    await db.refresh(row)
    return row


async def fail_generation(
    db: AsyncSession, campaign_id: UUID, *, error: str
) -> NotePlan | None:
    """Record why a generation or an apply failed, so the DM is told.

    The proposal itself is NOT discarded on a generation failure (there is
    nothing to keep - it never existed), but an APPLY failure must leave the
    reviewed set in place: mark_draft() is the one that does that, and this is
    only for the case where the set could not even be built.
    """
    row = await get_plan(db, campaign_id)
    if row is None:
        return None
    row.status = NOTE_PLAN_FAILED
    row.error = error[:2000]
    await db.commit()
    await db.refresh(row)
    return row


async def replace_reviewable(
    db: AsyncSession,
    campaign_id: UUID,
    *,
    changes: list[dict[str, Any]] | None = None,
    relations: list[dict[str, Any]] | None = None,
) -> NotePlan:
    """Persist the DM's review edits (only editable fields, only while draft)."""
    row = await get_plan(db, campaign_id)
    if row is None:
        raise PlanEditError("this campaign has no proposed changes")
    if row.status != PLAN_DRAFT:
        raise PlanEditError(
            f"the proposed changes are '{row.status}'; they can only be edited "
            "while they are a draft"
        )

    stored_changes = {str(c.get("id")): c for c in (row.changes or [])}
    if changes is not None:
        unknown = [str(c.get("id") or "") for c in changes if str(c.get("id")) not in stored_changes]
        if unknown:
            raise PlanEditError("unknown change id(s): " + ", ".join(unknown))
        patches = {str(c.get("id")): c for c in changes}
        row.changes = [
            validate_change(stored, patches[str(stored.get("id"))])
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


async def confirm_plan(
    db: AsyncSession, campaign_id: UUID, *, confirmed_by: UUID | None
) -> NotePlan | None:
    """Stamp the DM's confirmation and freeze the set for the write.

    'applying' is set HERE rather than left to the worker, and that is a
    correctness detail, not bookkeeping: this row is also what the plan page
    polls, and the page only polls while the run is in flight. Stamping the
    confirmation without moving the status left the row on 'draft' between the
    DM's click and the worker picking the job up - so the page stopped polling
    at exactly the moment there was something to wait for, and a confirmed plan
    sat there looking unreviewed however long the write took. Setting it here
    makes the confirm response carry 'applying' already, which is also the
    truth: the DM has decided, the wiki is now being written.

    The worker's mark_applying() is idempotent and still runs, so a redelivered
    message reaches the same state.
    """
    row = await get_plan(db, campaign_id)
    if row is None:
        return None
    row.status = PLAN_APPLYING
    row.confirmed_at = datetime.now(UTC)
    row.confirmed_by = confirmed_by
    row.error = None
    await db.commit()
    await db.refresh(row)
    return row


async def mark_applying(db: AsyncSession, campaign_id: UUID) -> NotePlan | None:
    """Freeze the set while the worker writes it into the wiki."""
    row = await get_plan(db, campaign_id)
    if row is None:
        return None
    row.status = PLAN_APPLYING
    await db.commit()
    await db.refresh(row)
    return row


async def mark_applied(db: AsyncSession, campaign_id: UUID) -> NotePlan | None:
    """The proposed pages exist in the wiki now."""
    row = await get_plan(db, campaign_id)
    if row is None:
        return None
    row.status = PLAN_APPLIED
    row.applied_at = datetime.now(UTC)
    row.error = None
    await db.commit()
    await db.refresh(row)
    return row


async def mark_draft(db: AsyncSession, campaign_id: UUID, *, error: str | None = None) -> NotePlan | None:
    """A failed apply goes back to review, so the DM can fix an item and retry."""
    row = await get_plan(db, campaign_id)
    if row is None:
        return None
    row.status = PLAN_DRAFT
    row.error = error
    await db.commit()
    await db.refresh(row)
    return row


async def discard_plan(db: AsyncSession, campaign_id: UUID) -> bool:
    """Throw a proposal away (the DM does not want these changes at all).

    A DELETE rather than a status, because the row is unique per campaign: the
    next generation needs the slot, and an empty 'draft' row would be
    indistinguishable from a proposal that found nothing to write.
    """
    result = await db.execute(delete(NotePlan).where(NotePlan.campaign_id == campaign_id))
    await db.commit()
    return bool(result.rowcount)
