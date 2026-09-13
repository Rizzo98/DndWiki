"""Session-summary review API: rewrite with feedback + confirm.

The session summary is the intermediate layer between the transcript and the
wiki. These two DM-only endpoints drive its review:

- POST /api/content/sessions/{id}/summary/regenerate — the DM selected one or
  more summary lines and described what must change ("it wasn't Character A,
  it was Character B"). The request is queued on the content.generate queue;
  the worker applies it to the whole extraction (summary lines, entities,
  events, timeline entries) and parks the session back on 'summary_ready'
  with a new revision.
- POST /api/content/sessions/{id}/summary/confirm — the DM accepts the
  summary. The worker then turns it into the PROPOSED change set the DM
  reviews on the session page ('generating_wiki' -> 'wiki_plan_ready'); the
  pages and timeline entries are written only after that second confirmation.

Authorization: campaign DM, or any user with the Keycloak 'dev' realm role.
Both endpoints only publish an event: the state machine transition (and its
409 idempotency guard) happens in the worker, which is the single writer of
sessions.status.
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
from app.services import summaries

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/content", tags=["summary-review"])

#: Session statuses from which the summary may still be rewritten or
#: confirmed: the draft is under review, or the phase failed and the DM is
#: retrying it.
REVIEWABLE_STATUSES = frozenset({"summary_ready", "failed"})

#: Most summary lines one review request may touch (a sanity cap on the
#: request body, the DM selects a handful of lines in practice).
MAX_TARGETS_PER_EDIT = 50

# Process-level singletons (tests monkeypatch these to inject fakes).
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
    """app.state accessor. The parameter MUST stay typed as Request: FastAPI
    turns an untyped parameter into a REQUIRED QUERY PARAM."""
    return request.app.state.publisher


class SummaryEdit(BaseModel):
    """One review request: the lines concerned + what must change."""

    targets: list[str] = Field(
        default_factory=list,
        max_length=MAX_TARGETS_PER_EDIT,
        description=(
            "Summary lines the request is about, verbatim. Empty = the request "
            "concerns the summary as a whole."
        ),
    )
    instruction: str = Field(
        min_length=1,
        description='What the DM wants changed, e.g. "It wasn\'t Character A, it was Character B".',
    )


class SummaryRegenerateRequest(BaseModel):
    """Body of the summary rewrite request."""

    edits: list[SummaryEdit] = Field(
        min_length=1, max_length=20, description="One entry per correction."
    )
    summary_lines: list[str] | None = Field(
        default=None,
        description=(
            "The summary lines exactly as the DM sees them. Optional: it lets "
            "the client send back hand-edited lines."
        ),
    )


async def _load_session(session_id: UUID) -> dict[str, Any]:
    """session record + authorization context (campaign id) for an action."""
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
    return session


def _assert_reviewable(session_id: UUID, session: dict[str, Any]) -> None:
    current_status = str(session.get("status") or "")
    if current_status not in REVIEWABLE_STATUSES:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                f"session status is '{current_status}'; the summary can only be "
                f"reviewed from {' or '.join(sorted(REVIEWABLE_STATUSES))}"
            ),
        )


@router.post("/sessions/{session_id}/summary/regenerate")
async def regenerate_summary(
    session_id: UUID,
    body: SummaryRegenerateRequest,
    user: dict[str, Any] = Depends(current_user),
    publisher: EventPublisher = Depends(get_publisher),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Queue a summary rewrite driven by the DM's feedback."""
    settings: ServiceSettings = get_settings()
    session = await _load_session(session_id)
    campaign_id = str(session["campaign_id"])
    await authorize_dm_or_dev(user, campaign_id, _get_campaign_client())
    _assert_reviewable(session_id, session)

    row = await summaries.latest_summary_for_session(db, session_id)
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="session has no summary to rewrite yet",
        )

    edits = [
        {
            "targets": [t.strip() for t in edit.targets if t.strip()],
            "instruction": edit.instruction.strip(),
        }
        for edit in body.edits
        if edit.instruction.strip()
    ]
    if not edits:
        raise HTTPException(
            status_code=422,  # Unprocessable Content (starlette renamed it)
            detail="at least one non-empty instruction is required",
        )

    await publisher.publish(
        Event(
            type="summary.regenerate",
            payload={
                "session_id": str(session_id),
                "campaign_id": campaign_id,
                "summary_id": str(row.id),
                "revision": row.revision,
                "edits": edits,
                "summary_lines": body.summary_lines,
                "requested_by": str(user.get("sub") or "") or None,
            },
        )
    )
    logger.info(
        "summary rewrite queued for session %s by %s (%d request(s), model %s)",
        session_id, user.get("sub"), len(edits), settings.llm_model,
    )
    return {
        "session_id": str(session_id),
        "campaign_id": campaign_id,
        "queued": True,
        "revision": row.revision,
        "edits": len(edits),
        "llm_model": settings.llm_model,
        "prompt_version": settings.prompt_version,
    }


@router.post("/sessions/{session_id}/summary/confirm")
async def confirm_summary(
    session_id: UUID,
    user: dict[str, Any] = Depends(current_user),
    publisher: EventPublisher = Depends(get_publisher),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Confirm the draft summary: the proposed changes may now be generated."""
    settings: ServiceSettings = get_settings()
    session = await _load_session(session_id)
    campaign_id = str(session["campaign_id"])
    await authorize_dm_or_dev(user, campaign_id, _get_campaign_client())
    _assert_reviewable(session_id, session)

    row = await summaries.latest_summary_for_session(db, session_id)
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="session has no summary to confirm yet",
        )

    # The confirmation stamp is written HERE: it is the DM's act, and the
    # generation of the proposed changes refuses to start without it.
    user_id = str(user.get("sub") or "") or None
    row = await summaries.confirm_summary(
        db, session_id, confirmed_by=UUID(user_id) if user_id else None
    )

    await publisher.publish(
        Event(
            type="summary.confirmed",
            payload={
                "session_id": str(session_id),
                "campaign_id": campaign_id,
                "summary_id": str(row.id),
                "revision": row.revision,
                "confirmed_by": str(user.get("sub") or "") or None,
            },
        )
    )
    logger.info(
        "summary revision %s of session %s confirmed by %s; change-set generation queued",
        row.revision, session_id, user_id,
    )
    return {
        "session_id": str(session_id),
        "campaign_id": campaign_id,
        "queued": True,
        "revision": row.revision,
        "llm_model": settings.llm_model,
        "prompt_version": settings.prompt_version,
    }
