"""Client for the wiki-service API (service-to-service, dnd-services token).

Creates pending_review draft pages and proposes cross-references. wiki-service
restricts service tokens to draft|pending_review statuses (publishing requires
DM approval) — exactly what the content worker needs.
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
    """Async HTTP client for the wiki draft endpoints."""

    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings
        self._base_url = settings.wiki_service_url.rstrip("/")

    async def create_page(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /api/wiki/pages with a PageCreate payload; returns the page."""
        return await self._post("/api/wiki/pages", payload)

    async def list_campaign_pages(self, campaign_id: str) -> list[dict[str, Any]]:
        """GET /internal/wiki/pages — flat listing used for cross-run dedupe."""
        return await self._get(f"/internal/wiki/pages?campaign_id={campaign_id}")

    async def create_relation(
        self, page_id: str, related_page_id: str, relation_type: str
    ) -> dict[str, Any]:
        """POST /api/wiki/pages/{id}/relations (propose a cross-reference)."""
        return await self._post(
            f"/api/wiki/pages/{page_id}/relations",
            {"related_page_id": related_page_id, "relation_type": relation_type},
        )

    async def list_timeline_events(self, campaign_id: str) -> list[dict[str, Any]]:
        """GET /internal/wiki/timeline — every entry (approved or not)."""
        return await self._get(f"/internal/wiki/timeline?campaign_id={campaign_id}")

    async def upsert_timeline_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /internal/wiki/timeline/upsert — create/refresh the entry of an
        event page (approved stays pending until the DM approves)."""
        return await self._post("/internal/wiki/timeline/upsert", payload)

    async def update_page(
        self, page_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """PATCH /api/wiki/pages/{id} — refresh an existing event page's
        content with newly extracted information (service tokens may only
        touch content, never status/visibility)."""
        return await self._patch(f"/api/wiki/pages/{page_id}", payload)

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
        return await self._send("POST", path, body=body)

    async def _patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._send("PATCH", path, body=body)

    async def _send(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        token = service_token(self._settings)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=self._settings.service_timeout_sec) as client:
                resp = await client.request(
                    method, f"{self._base_url}{path}", json=body, headers=headers
                )
        except httpx.HTTPError as exc:
            logger.warning("wiki-service request failed: %s", exc)
            raise WikiServiceError(f"wiki-service request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise WikiServiceError(f"wiki-service returned {resp.status_code}: {resp.text}")
        return resp.json()
