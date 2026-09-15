"""Internal evidence API: the vectors the attribution engine needs.

The per-observation EMBEDDINGS live here (Qdrant), not in the
speakers.identified payload: the payload carries the cosines, because that is
what a consumer needs to score a candidate, but the vectors themselves are what
make cluster purity, utterance-level defection and k-nearest-neighbour edges
possible at all (docs/attribution-model.md S4.3, S5.1).

Without this endpoint the engine still runs, but only at label granularity with
time-order adjacency: it can tell that a label is probably the wizard, and it
cannot tell that the label is two people. That is the difference this endpoint
buys.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.core.config import ServiceSettings, get_settings
from app.deps import require_service
from app.qdrant import VoiceObservationStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(require_service)])


@router.get("/sessions/{session_id}/voice-observations")
async def voice_observations(
    session_id: str,
    settings: ServiceSettings = Depends(get_settings),
) -> dict:
    """Every per-turn voice embedding of a session, with its quality and label.

    Returns an empty list rather than 404 when the session was identified before
    the observation store existed: the engine degrades to label granularity, and
    a missing artifact must not fail a session that is otherwise fine.
    """
    store = VoiceObservationStore(settings)
    try:
        points = await store.list_session(session_id)
    except Exception:
        logger.exception("could not read the voice observations of session %s", session_id)
        return {"session_id": session_id, "observations": [], "error": "unavailable"}

    out = []
    for point in points:
        payload = getattr(point, "payload", None) or {}
        vector = getattr(point, "vector", None)
        if not vector:
            continue
        out.append(
            {
                "observation_id": payload.get("observation_id"),
                "label": payload.get("label"),
                "index": payload.get("index"),
                "start": payload.get("start_sec"),
                "end": payload.get("end_sec"),
                "quality": payload.get("quality"),
                "model_version": payload.get("model_version"),
                "vector": list(vector),
            }
        )
    out.sort(key=lambda item: (float(item.get("start") or 0.0), str(item.get("observation_id"))))
    return {"session_id": session_id, "observations": out}
