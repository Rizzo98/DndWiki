"""Debug regenerate API: re-run wiki generation from the SAME transcript.

POST /api/content/sessions/{id}/regenerate re-publishes a synthetic
'speakers.identified' event built from the session's stored speaker map.
The regular worker picks it up through the existing content.generate queue
binding — no new topology, no duplicate pipelines (the state machine refuses
concurrent runs, and regeneration overwrites the previous summary).

Authorization: campaign DM, or any user with the 'dev' realm role.
Allowed source states: content_ready | reviewed - unpublish first for
published sessions; failed sessions need their own retry flow.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from dnd_common.auth import current_user, is_developer
from dnd_common.db import get_session
from dnd_common.events import Event
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.broker import EventPublisher
from app.clients.campaign_service import CampaignServiceClient, MembershipUnavailable
from app.clients.session_service import SessionServiceClient, SessionServiceError
from app.core.config import ServiceSettings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/content", tags=["debug"])

#: States a (debug) regeneration may start from — the state machine must
#: accept GENERATING_WIKI from each of these.
REGENERABLE_STATUSES = {"content_ready", "reviewed"}


def get_publisher(request: Request) -> EventPublisher:
    """app.state accessor. NOTE: the parameter MUST be typed as Request —
    FastAPI turns an untyped/Any parameter into a REQUIRED QUERY PARAM,
    which made every call fail with 422 before this was fixed."""
    return request.app.state.publisher


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


@router.post("/sessions/{session_id}/regenerate")
async def regenerate_session(
    session_id: UUID,
    user: dict[str, Any] = Depends(current_user),
    publisher: EventPublisher = Depends(get_publisher),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Queue a fresh wiki-generation run for an already-transcribed session."""
    del db  # dependency kept for parity with the rest of the platform APIs
    settings: ServiceSettings = get_settings()
    session_client = _get_session_client()
    campaign_client = _get_campaign_client()

    try:
        session = await session_client.get_session(str(session_id))
    except SessionServiceError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    campaign_id = str(session.get("campaign_id") or "")
    if not campaign_id:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="session has no campaign")

    # Authorization: campaign DM, or any user carrying the 'dev' realm role
    # (debug tooling is not scoped to one campaign).
    user_id = str(user["sub"])
    if not is_developer(user):
        try:
            await campaign_client.assert_dm(UUID(campaign_id), UUID(user_id))
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except MembershipUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="campaign-service unavailable",
            ) from exc

    current_status = str(session.get("status") or "")
    if current_status not in REGENERABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"session status is '{current_status}'; regeneration is possible "
                f"from {' or '.join(sorted(REGENERABLE_STATUSES))} only"
            ),
        )

    try:
        speakers = await session_client.list_speakers(str(session_id))
    except SessionServiceError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    resolved = [
        {
            "label": str(s["label"]),
            "user_id": str(s["user_id"]) if s.get("user_id") else None,
            "display_name": s.get("display_name"),
            "character_name": s.get("character_name"),
        }
        for s in speakers
        if s.get("user_id") or s.get("display_name")
    ]
    unresolved = sorted({str(s["label"]) for s in speakers if not (s.get("user_id") or s.get("display_name"))})
    if unresolved:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"session still has unnamed speakers: {', '.join(unresolved)}",
        )

    await publisher.publish(
        Event(
            type="speakers.identified",
            payload={
                "session_id": str(session_id),
                "campaign_id": campaign_id,
                "pending_assignment": False,
                "regenerated": True,
                "speakers": [
                    {
                        "label": s["label"],
                        "user_id": s["user_id"],
                        "display_name": s["display_name"],
                        "character_name": s.get("character_name"),
                    }
                    for s in resolved
                ],
            },
        )
    )
    logger.info(
        "regeneration queued for session %s by %s (%d named speakers)",
        session_id, user_id, len(resolved),
    )
    return {
        "session_id": str(session_id),
        "campaign_id": campaign_id,
        "queued": True,
        "speakers": len(resolved),
        "llm_model": settings.llm_model,
        "prompt_version": settings.prompt_version,
    }
