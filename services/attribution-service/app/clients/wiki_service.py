"""wiki-service internal API: character pages, for the capability store."""

from __future__ import annotations

import logging
from typing import Any

from app.clients.base import BaseClient, ServiceError
from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)


class WikiServiceClient(BaseClient):
    def __init__(self, settings: ServiceSettings) -> None:
        super().__init__(settings, settings.wiki_service_url)

    async def character_pages(self, campaign_id: str) -> list[dict[str, Any]]:
        """The campaign's CHARACTER pages; [] when unreachable.

        This is the real route wiki-service serves (GET /internal/wiki/pages,
        the same flat listing content-service uses for dedupe), filtered to
        kind == "character". The character page is the only place the campaign
        records a class, which is what the capability channel reads ("only the
        wizard can cast Fireball").

        Called nothing else: an unreachable wiki only costs the capability
        channel, never the session.
        """
        try:
            payload = await self.request(
                "GET",
                "/internal/wiki/pages",
                params={"campaign_id": campaign_id, "limit": 500},
            )
        except ServiceError:
            logger.warning("could not read the character pages of %s", campaign_id)
            return []
        pages = payload if isinstance(payload, list) else payload.get("pages", [])
        return [
            page
            for page in pages
            if isinstance(page, dict) and page.get("kind") == "character"
        ]
