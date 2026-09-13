"""Client for the wiki-service API (service-to-service, dnd-services token).

Reads the campaign's page listing (cross-session dedupe input), reports what a
single SESSION wrote into the wiki (deletion guard) and APPLIES the change set
the DM confirmed: POST /internal/wiki/changes/apply creates the confirmed pages
as published, updates the ones the campaign already has and writes the timeline
entries. Regular page creation through the public API
stays restricted to draft pages for service tokens — the internal
apply endpoint is the only publishing path, and it is reached only after the
DM confirmed the proposed changes on the session page.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from dnd_common.auth import service_token

from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)


class WikiServiceError(Exception):
    """wiki-service call failed (network, auth, unexpected 4xx/5xx, ...)."""


class WikiServiceClient:
    """Async HTTP client for the wiki endpoints used by the content pipeline."""

    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings
        self._base_url = settings.wiki_service_url.rstrip("/")

    async def list_campaign_pages(self, campaign_id: str) -> list[dict[str, Any]]:
        """GET /internal/wiki/pages — flat listing used for cross-run dedupe."""
        return await self._get(f"/internal/wiki/pages?campaign_id={campaign_id}")

    async def apply_changes(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /internal/wiki/changes/apply — write a DM-confirmed change set.

        Returns the created/updated page ids, the timeline entries written and
        the changes that were skipped (a page the campaign already has).
        """
        return await self._post("/internal/wiki/changes/apply", payload)

    async def list_timeline_events(self, campaign_id: str) -> list[dict[str, Any]]:
        """GET /internal/wiki/timeline — every entry (approved or not)."""
        return await self._get(f"/internal/wiki/timeline?campaign_id={campaign_id}")

    async def session_content(self, session_id: str) -> dict[str, Any]:
        """GET /internal/wiki/sessions/{id}/content — what a session wrote.

        Read-only guard for session deletion: the pages (and timeline entries)
        attributed to a session must not outlive it.
        """
        return await self._get(f"/internal/wiki/sessions/{session_id}/content")

    async def _get(self, path: str) -> Any:
        token = service_token(self._settings)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=self._settings.service_timeout_sec) as client:
                resp = await client.get(f"{self._base_url}{path}", headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("wiki-service request failed: %s", exc)
            raise WikiServiceError(f"wiki-service request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise WikiServiceError(f"wiki-service returned {resp.status_code}: {resp.text}")
        return resp.json()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        token = service_token(self._settings)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=self._settings.service_timeout_sec) as client:
                resp = await client.post(
                    f"{self._base_url}{path}", json=body, headers=headers
                )
        except httpx.HTTPError as exc:
            logger.warning("wiki-service request failed: %s", exc)
            raise WikiServiceError(f"wiki-service request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise WikiServiceError(f"wiki-service returned {resp.status_code}: {resp.text}")
        return resp.json()
