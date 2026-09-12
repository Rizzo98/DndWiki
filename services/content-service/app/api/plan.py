"""Proposed-changes API: the 'git status' of a session.

After the DM confirms the session summary, the worker turns it into a set of
PROPOSED wiki changes — pages to create, pages to update and the timeline
entries they back — and parks the session on 'wiki_plan_ready'. These
endpoints are that review:

- GET  /api/content/sessions/{id}/plan           read the proposed changes
- PUT  /api/content/sessions/{id}/plan           save the DM's edits/drops
- POST /api/content/sessions/{id}/plan/confirm   apply them (wiki-service)

Nothing reaches the wiki before the confirmation: the worker writes the
confirmed set through wiki-service's internal apply endpoint, which creates
the pages PUBLISHED (and their timeline entries approved), so no
pipeline-generated page ever sits in 'pending review'.

Authorization: campaign DM, or any user with the Keycloak 'dev' realm role —
the plan contains content that is not in the wiki yet.
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
from app.broker import EventPublisher
from app.clients.campaign_service import CampaignServiceClient
from app.clients.session_service import SessionServiceClient, SessionServiceError
from app.core.config import ServiceSettings, get_settings
from app.models import PLAN_DRAFT
from app.services import plans

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/content", tags=["plan"])

#: Session statuses the review can happen from. 'failed' is the retry path: a
#: failed apply leaves the reviewed set in place (draft again) so the DM can
#: fix an item and confirm once more.
REVIEWABLE_STATUSES = frozenset({"wiki_plan_ready", "failed"})

#: How many changes one set may carry (sanity cap on the review payload).
MAX_CHANGES = 200
MAX_RELATIONS = 200

_session_client: SessionServiceClient | None = None
_campaign_client: CampaignServiceClient | None = None


def _get_session_client() -> SessionServiceClient:
    global _session_client
    if _session_client is None:
        _session_client = SessionServiceClient(get_settings())
    return _session_client


def _get_campaign_client() -> CampaignServiceClient:
    global _campaign_client
    if _campaign_client is None:
        _campaign_client = CampaignServiceClient(get_settings())
    return _campaign_client


def get_publisher(request: Request) -> EventPublisher:
    """app.state accessor (the parameter MUST stay typed as Request)."""
    return request.app.state.publisher


class PlanTimelineEdit(BaseModel):
    """The timeline entry an event change writes."""

    summary: str = Field(min_length=1, max_length=2000)
    in_world_date: str | None = Field(default=None, max_length=256)


class PlanAfterEdit(BaseModel):
    """The page payload a change would write (what the DM may rewrite)."""

    content_json: dict[str, Any] | None = None
    visibility: str | None = None


class PlanChangeEdit(BaseModel):
    """The DM's edit of ONE proposed change (identity fields are server-owned)."""

    id: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=255)
    after: PlanAfterEdit | None = None
    timeline: PlanTimelineEdit | None = None
    dropped: bool | None = None


class PlanRelationEdit(BaseModel):
    """Cross-references are proposed by the pipeline: the DM keeps or drops them."""

    id: str = Field(min_length=1, max_length=64)
    dropped: bool | None = None


class PlanUpdateRequest(BaseModel):
    """Body of PUT /plan: the review state the session page currently shows."""

    changes: list[PlanChangeEdit] | None = Field(default=None, max_length=MAX_CHANGES)
    relations: list[PlanRelationEdit] | None = Field(default=None, max_length=MAX_RELATIONS)


def _counts(changes: list[dict[str, Any]], relations: list[dict[str, Any]]) -> dict[str, int]:
    """The change counters the header of the review card shows."""
    active = [c for c in changes if not c.get("dropped")]
    return {
        "create": sum(1 for c in active if c.get("action") == "create"),
        "update": sum(1 for c in active if c.get("action") == "update"),
        "pages": len(active),
        "events": sum(
            1 for c in active if c.get("kind") == "event" or c.get("timeline")
        ),
        "relations": sum(1 for r in relations if not r.get("dropped")),
        "dropped": sum(1 for c in changes if c.get("dropped"))
        + sum(1 for r in relations if r.get("dropped")),
    }


def serialize_plan(row: Any) -> dict[str, Any]:
    """The plan as the session page consumes it."""
    changes = list(row.changes or [])
    relations = list(row.relations or [])
    return {
        "id": str(row.id),
        "session_id": str(row.session_id),
        "summary_id": str(row.summary_id) if row.summary_id else None,
        "status": row.status,
        "language": row.language,
        "changes": changes,
        "relations": relations,
        "skipped": list(row.skipped or []),
        "counts": _counts(changes, relations),
        "error": row.error,
        "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
        "confirmed_by": str(row.confirmed_by) if row.confirmed_by else None,
        "applied_at": row.applied_at.isoformat() if row.applied_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _authorize(session_id: UUID, user: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Session record + authorization; returns (session, campaign_id)."""
    session_client = _get_session_client()
    try:
        session = await session_client.get_session(str(session_id))
    except SessionServiceError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    campaign_id = str(session.get("campaign_id") or "")
    if not campaign_id:
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY, detail="session has no campaign"
        )
    await authorize_dm_or_dev(user, campaign_id, _get_campaign_client())
    return session, campaign_id


@router.get("/sessions/{session_id}/plan")
async def get_session_plan(
    session_id: UUID,
    user: dict[str, Any] = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """The proposed wiki changes of a session (null while there is none)."""
    await _authorize(session_id, user)
    row = await plans.get_plan(db, session_id)
    return {"session_id": str(session_id), "plan": serialize_plan(row) if row else None}


@router.put("/sessions/{session_id}/plan")
async def update_session_plan(
    session_id: UUID,
    body: PlanUpdateRequest,
    user: dict[str, Any] = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Save the DM's review: edited page payloads, dropped changes, dropped links."""
    session, _campaign_id = await _authorize(session_id, user)
    if str(session.get("status") or "") not in REVIEWABLE_STATUSES:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                "the proposed changes of this session are not under review "
                f"(status '{session.get('status')}')"
            ),
        )
    try:
        row = await plans.replace_reviewable(
            db,
            session_id,
            changes=[c.model_dump() for c in body.changes] if body.changes is not None else None,
            relations=(
                [r.model_dump() for r in body.relations]
                if body.relations is not None
                else None
            ),
        )
    except (plans.PlanEditError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"session_id": str(session_id), "plan": serialize_plan(row)}


@router.post("/sessions/{session_id}/plan/confirm")
async def confirm_session_plan(
    session_id: UUID,
    user: dict[str, Any] = Depends(current_user),
    publisher: EventPublisher = Depends(get_publisher),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Confirm the proposed changes: the worker writes them into the wiki."""
    settings: ServiceSettings = get_settings()
    session, campaign_id = await _authorize(session_id, user)
    if str(session.get("status") or "") not in REVIEWABLE_STATUSES:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                "the proposed changes of this session are not under review "
                f"(status '{session.get('status')}')"
            ),
        )
    row = await plans.get_plan(db, session_id)
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="this session has no proposed changes yet",
        )
    if row.status != PLAN_DRAFT:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"the proposed changes are '{row.status}'",
        )
    user_id = str(user.get("sub") or "") or None
    row = await plans.confirm_plan(db, session_id, confirmed_by=UUID(user_id) if user_id else None)
    counts = _counts(list(row.changes or []), list(row.relations or []))
    await publisher.publish(
        Event(
            type="plan.confirmed",
            payload={
                "session_id": str(session_id),
                "campaign_id": campaign_id,
                "plan_id": str(row.id),
                "confirmed_by": user_id,
                **counts,
            },
        )
    )
    logger.info(
        "change set %s of session %s confirmed by %s (%d pages); applying",
        row.id, session_id, user_id, counts["pages"],
    )
    return {
        "session_id": str(session_id),
        "campaign_id": campaign_id,
        "queued": True,
        "plan_id": str(row.id),
        "pages": counts["pages"],
        "llm_model": settings.llm_model,
        "prompt_version": settings.prompt_version,
    }
