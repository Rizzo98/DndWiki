"""Internal pipeline API (service-to-service, dnd-services client token).

session-service calls DELETE /internal/content/sessions/{id} when the DM
deletes a session from the sessions tab: this drops the session's draft
summary, its generation jobs and its proposed change set.

The call is REFUSED (409) while the session's content already exists — either
the change set was applied, or wiki-service reports pages/timeline entries
attributed to the session. Deleting a session must never orphan wiki content
that points at it, so the DM has to deal with the pages first.
"""

from __future__ import annotations

import logging
from uuid import UUID

from dnd_common.db import get_session
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.wiki_service import WikiServiceClient, WikiServiceError
from app.core.config import get_settings
from app.deps import require_service
from app.models import PLAN_APPLIED, PLAN_APPLYING
from app.services import plans, session_data

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal/content",
    tags=["internal"],
    dependencies=[Depends(require_service)],
)

#: How many page titles a refusal names before it just counts them.
MAX_TITLES = 5

_wiki_client: WikiServiceClient | None = None


def _get_wiki_client() -> WikiServiceClient:
    global _wiki_client
    if _wiki_client is None:
        _wiki_client = WikiServiceClient(get_settings())
    return _wiki_client


def _describe(pages: list[dict], timeline: list[dict]) -> str:
    """Human-readable 'what is in the wiki' summary for the 409 body."""
    parts: list[str] = []
    if pages:
        titles = ", ".join(str(p.get("title") or "?") for p in pages[:MAX_TITLES])
        more = "" if len(pages) <= MAX_TITLES else f" and {len(pages) - MAX_TITLES} more"
        parts.append(f"{len(pages)} page(s) ({titles}{more})")
    if timeline:
        parts.append(f"{len(timeline)} timeline entr(ies)")
    return " and ".join(parts)


@router.delete("/sessions/{session_id}")
async def delete_session_data(
    session_id: UUID,
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Drop everything content-service holds for a session (deletion guard).

    Called by session-service only after its own status guard passed; this is
    the second line of defence, because the wiki content is what actually
    makes a session undeletable.
    """
    plan = await plans.get_plan(db, session_id)
    if plan is not None and plan.status in (PLAN_APPLYING, PLAN_APPLIED):
        raise HTTPException(
            status_code=409,
            detail=(
                "the wiki updates of this session were already generated "
                f"(change set is '{plan.status}')"
            ),
        )
    try:
        content = await _get_wiki_client().session_content(str(session_id))
    except WikiServiceError as exc:
        raise HTTPException(
            status_code=503, detail=f"wiki-service unavailable: {exc}"
        ) from exc
    pages = list(content.get("pages") or [])
    timeline = list(content.get("timeline") or [])
    if pages or timeline:
        raise HTTPException(
            status_code=409,
            detail=(
                "this session already wrote "
                + _describe(pages, timeline)
                + " into the wiki; delete or archive that content first"
            ),
        )
    counts = await session_data.purge_session(db, session_id)
    logger.info("purged content rows of session %s: %s", session_id, counts)
    return {"session_id": str(session_id), "deleted": True, **counts}
