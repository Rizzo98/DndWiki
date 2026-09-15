"""Internal API: the pipeline's own surface (service-to-service)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from dnd_common.db import get_session
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import ServiceSettings, get_settings
from app.deps import require_service
from app.models import (
    PropagationEvent,
    ReviewQuestion,
    ReviewRun,
    SessionBeliefStats,
    Utterance,
    UtteranceAttribution,
    UtteranceEvidence,
    VoiceIdentity,
)
from app.workers import compute as compute_worker

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(require_service)])


def _session_uuid(session_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown session"
        ) from exc


@router.post("/attribution/{session_id}/compute")
async def compute(
    session_id: str,
    body: dict[str, Any] | None = None,
    db: AsyncSession = Depends(get_session),
    settings: ServiceSettings = Depends(get_settings),
) -> dict[str, Any]:
    """Run (or re-run) the attribution pass for one session, synchronously."""
    payload = body or {}
    result = await compute_worker.compute_session(
        db,
        session_id=_session_uuid(session_id),
        campaign_id=str(payload.get("campaign_id") or "") or None,
        settings=settings,
        revision=int(payload.get("revision") or 0) or None,
    )
    return result


@router.get("/attribution/{session_id}/transcript")
async def attributed_transcript(
    session_id: str,
    db: AsyncSession = Depends(get_session),
    settings: ServiceSettings = Depends(get_settings),
) -> dict[str, Any]:
    """The attributed artifact content-service reads instead of a label map."""
    artifact = await compute_worker.load_artifact(_session_uuid(session_id))
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no attributed transcript for this session yet",
        )
    return artifact


@router.get("/attribution/{session_id}/status")
async def attribution_status(
    session_id: str,
    db: AsyncSession = Depends(get_session),
    settings: ServiceSettings = Depends(get_settings),
) -> dict[str, Any]:
    session_uuid = _session_uuid(session_id)
    stats = await db.get(SessionBeliefStats, session_uuid)
    run = await db.scalar(select_run(session_uuid))
    if stats is None:
        return {"session_id": session_id, "computed": False}
    return {
        "session_id": session_id,
        "computed": True,
        "coverage": float(stats.coverage),
        "unresolved": stats.unresolved,
        "unresolved_stakes": float(stats.unresolved_stakes),
        "revision": stats.revision,
        "engine_version": stats.engine_version,
        "converged": stats.converged,
        "questions_planned": stats.questions_planned,
        "stop_reason": stats.stop_reason,
        "run_status": run.status if run else None,
    }


def select_run(session_uuid: uuid.UUID):
    from sqlalchemy import select

    return select(ReviewRun).where(ReviewRun.session_id == session_uuid)


@router.delete("/attribution/{session_id}")
async def purge(
    session_id: str,
    db: AsyncSession = Depends(get_session),
    settings: ServiceSettings = Depends(get_settings),
) -> dict[str, Any]:
    """Drop everything this service stored for a session (session deletion).

    Deleting a session must delete the audio-derived rows with it: the voice
    identities carry no personal data of their own, but they are derived from
    the recording and must not outlive it.
    """
    session_uuid = _session_uuid(session_id)
    utterance_ids = (
        await db.scalars(select_utterance_ids(session_uuid))
    ).all()
    removed = 0
    if utterance_ids:
        await db.execute(
            delete(UtteranceEvidence).where(UtteranceEvidence.utterance_id.in_(utterance_ids))
        )
        await db.execute(
            delete(UtteranceAttribution).where(
                UtteranceAttribution.utterance_id.in_(utterance_ids)
            )
        )
        removed = len(utterance_ids)
        await db.execute(delete(Utterance).where(Utterance.id.in_(utterance_ids)))
    await db.execute(delete(VoiceIdentity).where(VoiceIdentity.session_id == session_uuid))
    await db.execute(delete(ReviewQuestion).where(ReviewQuestion.session_id == session_uuid))
    await db.execute(delete(ReviewRun).where(ReviewRun.session_id == session_uuid))
    await db.execute(
        delete(PropagationEvent).where(PropagationEvent.session_id == session_uuid)
    )
    await db.execute(
        delete(SessionBeliefStats).where(SessionBeliefStats.session_id == session_uuid)
    )
    await db.commit()
    return {"session_id": session_id, "utterances_removed": removed}


def select_utterance_ids(session_uuid: uuid.UUID):
    from sqlalchemy import select

    return select(Utterance.id).where(Utterance.session_id == session_uuid)
