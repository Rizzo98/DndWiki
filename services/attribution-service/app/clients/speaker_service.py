"""speaker-service internal API: the per-turn voice embeddings."""

from __future__ import annotations

import logging
from typing import Any

from app.clients.base import BaseClient, ServiceError
from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)


class SpeakerServiceClient(BaseClient):
    """Reads the voice observations speaker-service stored in Qdrant."""

    def __init__(self, settings: ServiceSettings) -> None:
        super().__init__(settings, settings.speaker_service_url)

    async def voice_observations(self, session_id: str) -> list[dict[str, Any]]:
        """Every per-turn embedding of a session ([] when unavailable).

        The embeddings are what make cluster purity, utterance-level defection
        and k-nearest-neighbour edges possible; without them the engine degrades
        to label granularity and says so in the log.
        """
        try:
            payload = await self.request(
                "GET", f"/internal/sessions/{session_id}/voice-observations"
            )
        except ServiceError:
            logger.warning(
                "could not read the voice observations of session %s; "
                "attributing at label granularity",
                session_id,
            )
            return []
        if not isinstance(payload, dict):
            return []
        return list(payload.get("observations") or [])
