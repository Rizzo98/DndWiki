"""campaign-service client: campaign language, roster and prompt context.

The AssemblyAI worker asks campaign-service for the campaign (name, language,
description) and its full roster (dm + players). The language becomes the
`language_code` sent to the speech model, the roster size becomes
`speakers_expected` (the number of people at the table) and the names feed
the contextual prompt / keyterms so fantasy names are recognized accurately.

This call is BEST-EFFORT: if campaign-service is unreachable or the campaign
is gone, the worker still transcribes — without the campaign language
(Auto-Detect), without a speaker count (auto diarization) and with only the
built-in prompt. Exactly the same degradation policy as the refiner's cast
fetch.
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
    """The campaign does not exist (404) - no campaign context to inject."""


class CampaignServiceClient:
    """Async HTTP client for the campaign-service internal endpoints."""

    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings
        self._base_url = settings.campaign_service_url.rstrip("/")

    async def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        """The campaign record: {id, name, description, language, dm_user_id}."""
        token = service_token(self._settings)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.campaign_service_timeout_sec
            ) as client:
                resp = await client.get(
                    f"{self._base_url}/internal/campaigns/{campaign_id}",
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
