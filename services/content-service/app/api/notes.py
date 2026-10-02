"""The DM toolkit's 'plan' tool: planning notes and the wiki they become.

Two things live here, and they are separate on purpose:

- the NOTES (free text the DM writes to themselves, with all the ordinary
  furniture around it: rename, edit, draft/settled, delete). Writing a note
  never touches the wiki and never calls a model - it is a text box;
- the PLAN built from a selection of them, reviewed and confirmed like a
  session's proposed changes. Generating is asynchronous (an LLM reads the
  notes, and that can take a while), so the POST only opens the run and the
  plan row carries the progress; the plan page polls GET /plan.

Authorization: the campaign's DM, or any user with the Keycloak 'dev' realm
role. A plan contains text that is not in the wiki yet, and the notes are the
DM's private working material - a player must never read either, so every route
below goes through authorize_dm_or_dev.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from dnd_common.auth import current_user
from dnd_common.db import get_session
from dnd_common.events import Event
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.authorization import authorize_dm_or_dev
from app.api.plan import plan_counts
from app.broker import EventPublisher
from app.clients.campaign_service import CampaignServiceClient
from app.core.config import ServiceSettings, get_settings
from app.models import (
    NOTE_DRAFT,
    NOTE_PLAN_FAILED,
    NOTE_READY,
    PLAN_APPLIED,
    PLAN_DRAFT,
)
from app.prompts import NOTE_PLAN_PROMPT_VERSION
from app.services import note_plans, notes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/content/campaigns", tags=["toolkit"])

#: How many notes one generation may read (sanity cap on the prompt size).
MAX_NOTES_PER_GENERATION = 50
MAX_CHANGES = 200
MAX_RELATIONS = 200
MAX_BODY_CHARS = 200_000

_campaign_client: CampaignServiceClient | None = None


def _get_campaign_client() -> CampaignServiceClient:
    global _campaign_client
    if _campaign_client is None:
        _campaign_client = CampaignServiceClient(get_settings())
    return _campaign_client


def get_publisher(request: Request) -> EventPublisher:
    """app.state accessor (the parameter MUST stay typed as Request)."""
    return request.app.state.publisher


# --------------------------------------------------------------------- bodies


class NoteCreateRequest(BaseModel):
    title: str = Field(default="", max_length=255)
    body: str = Field(default="", max_length=MAX_BODY_CHARS)
    status: str = Field(default=NOTE_DRAFT)


class NoteUpdateRequest(BaseModel):
    """The fields the editor sends; anything omitted is left alone."""

    title: str | None = Field(default=None, max_length=255)
    body: str | None = Field(default=None, max_length=MAX_BODY_CHARS)
    status: str | None = None


class GenerateRequest(BaseModel):
    """Which notes to read. At least one, and it must exist."""

    note_ids: list[UUID] = Field(min_length=1, max_length=MAX_NOTES_PER_GENERATION)


class PlanTimelineEdit(BaseModel):
    summary: str = Field(min_length=1, max_length=2000)
    in_world_date: str | None = Field(default=None, max_length=256)


class PlanAfterEdit(BaseModel):
    content_json: dict[str, Any] | None = None
    visibility: str | None = None


class PlanChangeEdit(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=255)
    after: PlanAfterEdit | None = None
    timeline: PlanTimelineEdit | None = None
    dropped: bool | None = None


class PlanRelationEdit(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    dropped: bool | None = None


class PlanUpdateRequest(BaseModel):
    changes: list[PlanChangeEdit] | None = Field(default=None, max_length=MAX_CHANGES)
    relations: list[PlanRelationEdit] | None = Field(default=None, max_length=MAX_RELATIONS)


# ---------------------------------------------------------------- serializing


def serialize_note(row: Any, *, full: bool = False) -> dict[str, Any]:
    """A note for the toolkit: the list gets a preview, the editor the body."""
    return {
        "id": str(row.id),
        "campaign_id": str(row.campaign_id),
        "title": row.title,
        "body": row.body if full else notes.excerpt(row),
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def serialize_plan(row: Any) -> dict[str, Any]:
    """The plan as the toolkit page consumes it (mirrors the session plan)."""
    changes = list(row.changes or [])
    relations = list(row.relations or [])
    return {
        "id": str(row.id),
        "campaign_id": str(row.campaign_id),
        "status": row.status,
        "note_ids": list(row.note_ids or []),
        "language": row.language,
        "changes": changes,
        "relations": relations,
        "skipped": list(row.skipped or []),
        "counts": plan_counts(changes, relations),
        "error": row.error,
        "llm_provider": row.llm_provider,
        "llm_model": row.llm_model,
        "prompt_version": row.prompt_version,
        "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
        "confirmed_by": str(row.confirmed_by) if row.confirmed_by else None,
        "applied_at": row.applied_at.isoformat() if row.applied_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _authorize(campaign_id: UUID, user: dict[str, Any]) -> None:
    await authorize_dm_or_dev(user, str(campaign_id), _get_campaign_client())


def _user_id(user: dict[str, Any]) -> UUID | None:
    raw = str(user.get("sub") or "")
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------- notes


@router.get("/{campaign_id}/notes")
async def list_notes(
    campaign_id: UUID,
    user: dict[str, Any] = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Every note of the campaign, newest change first."""
    await _authorize(campaign_id, user)
    rows = await notes.list_notes(db, campaign_id)
    return {"notes": [serialize_note(row) for row in rows]}


@router.post("/{campaign_id}/notes", status_code=http_status.HTTP_201_CREATED)
async def create_note(
    campaign_id: UUID,
    body: NoteCreateRequest,
    user: dict[str, Any] = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Open a blank note (the editor creates one, then types into it)."""
    await _authorize(campaign_id, user)
    row = await notes.create_note(
        db,
        campaign_id,
        title=body.title,
        body=body.body,
        status=body.status,
        user_id=_user_id(user),
    )
    return {"note": serialize_note(row, full=True)}


@router.get("/{campaign_id}/notes/{note_id}")
async def get_note(
    campaign_id: UUID,
    note_id: UUID,
    user: dict[str, Any] = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _authorize(campaign_id, user)
    row = await notes.get_note(db, campaign_id, note_id)
    if row is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Note not found")
    return {"note": serialize_note(row, full=True)}


@router.put("/{campaign_id}/notes/{note_id}")
async def update_note(
    campaign_id: UUID,
    note_id: UUID,
    body: NoteUpdateRequest,
    user: dict[str, Any] = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Save a note: rename, rewrite the body, mark it draft or settled."""
    await _authorize(campaign_id, user)
    if body.status is not None and body.status not in (NOTE_DRAFT, NOTE_READY):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"status must be '{NOTE_DRAFT}' or '{NOTE_READY}'",
        )
    row = await notes.get_note(db, campaign_id, note_id)
    if row is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Note not found")
    row = await notes.update_note(
        db,
        row,
        title=body.title,
        body=body.body,
        status=body.status,
        user_id=_user_id(user),
    )
    return {"note": serialize_note(row, full=True)}


@router.delete("/{campaign_id}/notes/{note_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_note(
    campaign_id: UUID,
    note_id: UUID,
    user: dict[str, Any] = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    await _authorize(campaign_id, user)
    if not await notes.delete_note(db, campaign_id, note_id):
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Note not found")


# ----------------------------------------------------------------------- plan


@router.get("/{campaign_id}/plan")
async def get_plan(
    campaign_id: UUID,
    user: dict[str, Any] = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """The campaign's proposed wiki changes (null while there is none).

    Also the poll target while a generation runs: 'generating' means the worker
    is reading the notes, 'draft' that the review can start.
    """
    await _authorize(campaign_id, user)
    row = await note_plans.get_plan(db, campaign_id)
    return {"plan": serialize_plan(row) if row else None}


@router.post("/{campaign_id}/plan/generate", status_code=http_status.HTTP_202_ACCEPTED)
async def generate_plan(
    campaign_id: UUID,
    body: GenerateRequest,
    user: dict[str, Any] = Depends(current_user),
    publisher: EventPublisher = Depends(get_publisher),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Read the selected notes and propose the wiki changes they describe.

    Asynchronous: the row is opened as 'generating' and the event is published,
    so the answer comes back immediately and the page polls GET /plan. The
    previous proposal is replaced - regenerating is how the DM folds a new note
    into a plan they already generated, which is also what makes the wiki an
    update rather than a second, competing draft.
    """
    settings: ServiceSettings = get_settings()
    await _authorize(campaign_id, user)

    # The selection must be readable IN THIS CAMPAIGN: a note id from somewhere
    # else (or one just deleted) is refused rather than silently dropped, so the
    # DM does not get a proposal built from fewer notes than they picked.
    unique_ids = list(dict.fromkeys(body.note_ids))
    selected = await notes.get_notes_by_ids(db, campaign_id, unique_ids)
    missing = [str(note_id) for note_id in unique_ids if note_id not in {n.id for n in selected}]
    if missing:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="unknown note(s) in this campaign: " + ", ".join(missing),
        )

    row = await note_plans.start_generation(
        db,
        campaign_id,
        note_ids=[note.id for note in selected],
        provider=settings.llm_provider,
        model=settings.llm_model,
        prompt_version=NOTE_PLAN_PROMPT_VERSION,
    )
    await publisher.publish(
        Event(
            type="note.plan.requested",
            payload={
                "campaign_id": str(campaign_id),
                "plan_id": str(row.id),
                "note_ids": [str(note.id) for note in selected],
                "requested_by": str(_user_id(user) or ""),
            },
        )
    )
    logger.info(
        "note plan %s of campaign %s requested over %d note(s)",
        row.id, campaign_id, len(selected),
    )
    return {"plan": serialize_plan(row), "queued": True}


@router.put("/{campaign_id}/plan")
async def update_plan(
    campaign_id: UUID,
    body: PlanUpdateRequest,
    user: dict[str, Any] = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Save the DM's review: edited page payloads, dropped changes and links.

    The status codes match the session page's review exactly: 409 when there is
    nothing under review (or it is no longer a draft, so the edit would land on
    a set that is being written), and 422 when the edit itself is not something
    the stored set can take (an unknown change id, an impossible visibility).
    """
    await _authorize(campaign_id, user)
    current = await note_plans.get_plan(db, campaign_id)
    if current is None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="this campaign has no proposed changes yet",
        )
    if current.status != PLAN_DRAFT:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                "the proposed changes are not under review "
                f"(status '{current.status}')"
            ),
        )
    try:
        row = await note_plans.replace_reviewable(
            db,
            campaign_id,
            changes=[c.model_dump() for c in body.changes] if body.changes is not None else None,
            relations=(
                [r.model_dump() for r in body.relations] if body.relations is not None else None
            ),
        )
    except (note_plans.PlanEditError, ValueError) as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return {"plan": serialize_plan(row)}


@router.post("/{campaign_id}/plan/confirm", status_code=http_status.HTTP_202_ACCEPTED)
async def confirm_plan(
    campaign_id: UUID,
    user: dict[str, Any] = Depends(current_user),
    publisher: EventPublisher = Depends(get_publisher),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Confirm the proposed changes: the worker writes them into the wiki."""
    settings: ServiceSettings = get_settings()
    await _authorize(campaign_id, user)
    row = await note_plans.get_plan(db, campaign_id)
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="this campaign has no proposed changes yet",
        )
    if row.status != PLAN_DRAFT:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"the proposed changes are '{row.status}'",
        )
    user_id = _user_id(user)
    row = await note_plans.confirm_plan(db, campaign_id, confirmed_by=user_id)
    counts = plan_counts(list(row.changes or []), list(row.relations or []))
    await publisher.publish(
        Event(
            type="note.plan.confirmed",
            payload={
                "campaign_id": str(campaign_id),
                "plan_id": str(row.id),
                "confirmed_by": str(user_id) if user_id else None,
                **counts,
            },
        )
    )
    logger.info(
        "note plan %s of campaign %s confirmed by %s (%d pages); applying",
        row.id, campaign_id, user_id, counts["pages"],
    )
    return {
        "plan": serialize_plan(row),
        "queued": True,
        "llm_model": settings.llm_model,
        "prompt_version": NOTE_PLAN_PROMPT_VERSION,
    }


@router.delete("/{campaign_id}/plan", status_code=http_status.HTTP_204_NO_CONTENT)
async def discard_plan(
    campaign_id: UUID,
    user: dict[str, Any] = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    """Throw the proposal away without writing anything to the wiki.

    Only a set that is settled can go: a 'draft' waiting for the DM or a 'failed'
    run they gave up on. Everything else is refused, and each for its own reason:

    - 'applied': the pages exist. The way to undo them is to edit or archive them
      in the wiki, not to pretend the proposal never ran.
    - 'generating' / 'applying': the worker is reading the notes or writing the
      pages RIGHT NOW. Deleting the row under it would leave the worker with no
      row to record its result on - after the wiki had already been written - so
      the run would fail having actually succeeded.
    """
    await _authorize(campaign_id, user)
    row = await note_plans.get_plan(db, campaign_id)
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="No proposed changes to discard"
        )
    if row.status == PLAN_APPLIED:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="these changes are already in the wiki; edit or archive the pages instead",
        )
    # An ALLOW-list rather than a list of refused statuses: any status this
    # endpoint has not been taught about is refused, which is the safe default
    # for an operation that deletes the row a running worker reports against.
    if row.status not in (PLAN_DRAFT, NOTE_PLAN_FAILED):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                "this plan is still running ('"
                + row.status
                + "'); wait for it to finish before discarding it"
            ),
        )
    await note_plans.discard_plan(db, campaign_id)
