"""campaign-service client: fetch the campaign cast for the LLM context.

The refiner worker asks campaign-service for the campaign's roster (internal
endpoint, service token) so the LLM contextual pass knows who is at the table:
character names + physical descriptions let it correct misheard names and
attribute speakers to the right person. This call is BEST-EFFORT: if
campaign-service is unreachable or the campaign is gone, the worker still
refines the transcript, just without the cast context.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from dnd_common.auth import service_token

from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)


class CampaignServiceError(Exception):
    """campaign-service call failed (network, auth, unexpected status)."""


class CampaignNotFound(CampaignServiceError):
    """The campaign does not exist (404) - no cast to inject."""


class CampaignServiceClient:
    """Async HTTP client for the campaign-service internal members endpoint."""

    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings
        self._base_url = settings.campaign_service_url.rstrip("/")

    async def list_members(self, campaign_id: str) -> list[dict[str, Any]]:
        """The campaign roster (dm + players) with names and descriptions."""
        token = service_token(self._settings)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.campaign_service_timeout_sec
            ) as client:
                resp = await client.get(
                    f"{self._base_url}/internal/campaigns/{campaign_id}/members",
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            logger.warning("campaign-service request failed: %s", exc)
            raise CampaignServiceError(f"campaign-service request failed: {exc}") from exc
        if resp.status_code == 404:
            raise CampaignNotFound("campaign not found")
        if resp.status_code >= 400:
            raise CampaignServiceError(
                f"campaign-service returned {resp.status_code}: {resp.text}"
            )
        return resp.json()
