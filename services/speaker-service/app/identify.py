"""Pure speaker-matching logic (no I/O), unit-testable without torch/Qdrant.

The worker extracts an embedding per diarized label, searches the campaign
voiceprints, and calls match_label() to decide auto vs pending. Keeping the
decision here separates the ML/IO plumbing from the policy.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)


def match_label(
    label: str,
    *,
    settings: ServiceSettings,
    embedding: list[float] | None,
    duration_sec: float,
    hits: list,
) -> dict[str, Any]:
    """Decide one label's assignment from its embedding + search hits.

    Returns a SpeakerAssignmentIn-shaped dict:
        {speaker_label, user_id, confidence, status}

    - no usable embedding / too-short clip -> pending (no user, no confidence)
    - best hit above SPEAKER_MATCH_THRESHOLD -> auto with that user's id
    - best hit below threshold -> pending, confidence kept for the DM
    """
    if embedding is None or duration_sec < settings.voice_sample_min_sec:
        return {
            "speaker_label": label,
            "user_id": None,
            "confidence": None,
            "status": "pending",
        }

    best = hits[0] if hits else None
    if best is not None and best.score >= settings.speaker_match_threshold:
        logger.info(
            "label %s auto-matched user %s (cosine %.3f)",
            label,
            best.payload.get("user_id"),
            best.score,
        )
        return {
            "speaker_label": label,
            "user_id": best.payload.get("user_id"),
            "confidence": best.score,
            "status": "auto",
        }

    confidence = best.score if best is not None else None
    logger.info("label %s has no confident match (cosine %s); pending", label, confidence)
    return {
        "speaker_label": label,
        "user_id": None,
        "confidence": confidence,
        "status": "pending",
    }
