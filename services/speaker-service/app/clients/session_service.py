"""Client for the session-service internal worker API (service-to-service).

The speaker worker never touches dnd_sessions tables directly; it moves the
session through the pipeline state machine (transcribed -> identifying_speakers
-> speakers_identified | speaker_pending, or -> failed) and upserts the
diarized-label -> user assignments via the internal endpoints, authenticating
with a dnd-services client-credentials token.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from dnd_common.auth import service_token

from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)

STATUS_IDENTIFYING_SPEAKERS = "identifying_speakers"
STATUS_SPEAKERS_IDENTIFIED = "speakers_identified"
STATUS_SPEAKER_PENDING = "speaker_pending"
STATUS_FAILED = "failed"


class ConflictTransition(Exception):
    """session-service rejected the status change (session already moved on).

    Raised on 409 responses: the state machine refuses the transition, which
    happens on redelivered/duplicate jobs (idempotency guard) — callers should
    treat this as "already handled" and ack.
    """


class SessionServiceError(Exception):
    """session-service call failed (network, auth, unexpected 4xx/5xx, ...)."""


class SessionServiceClient:
    """Async HTTP client for session-service /internal/sessions endpoints."""

    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings
        self._base_url = settings.session_service_url.rstrip("/")

    async def update_status(self, session_id: str, status: str, error: str | None = None) -> dict[str, Any]:
        """PATCH /internal/sessions/{id}/status (state machine transition)."""
        return await self._request(
            "PATCH",
            f"/internal/sessions/{session_id}/status",
            {"status": status, "error": error},
        )

    async def upsert_speakers(self, session_id: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """POST /internal/sessions/{id}/speakers (write assignments)."""
        return await self._request(
            "POST",
            f"/internal/sessions/{session_id}/speakers",
            items,
        )

    async def speaker_history(
        self,
        campaign_id: str,
        *,
        exclude_session_id: str | None = None,
        limit_sessions: int = 5,
    ) -> list[dict[str, Any]]:
        """GET /internal/campaigns/{id}/speaker-history (DM-confirmed labels).

        The labels the DM named by hand in earlier sessions of the campaign:
        app.history turns their audio into voice samples for this run. Newest
        sessions first; the session being identified is excluded so it never
        becomes a reference for itself.
        """
        params: dict[str, Any] = {"limit_sessions": limit_sessions}
        if exclude_session_id:
            params["exclude_session_id"] = exclude_session_id
        return await self._get(
            f"/internal/campaigns/{campaign_id}/speaker-history", params
        )

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        token = service_token(self._settings)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=self._settings.session_service_timeout_sec) as client:
                resp = await client.get(f"{self._base_url}{path}", params=params, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("session-service request failed: %s", exc)
            raise SessionServiceError(f"session-service request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise SessionServiceError(f"session-service returned {resp.status_code}: {resp.text}")
        return resp.json()

    async def _request(self, method: str, path: str, body: dict[str, Any] | list[dict[str, Any]]) -> Any:
        token = service_token(self._settings)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=self._settings.session_service_timeout_sec) as client:
                resp = await client.request(method, f"{self._base_url}{path}", json=body, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("session-service request failed: %s", exc)
            raise SessionServiceError(f"session-service request failed: {exc}") from exc
        if resp.status_code == 409:
            raise ConflictTransition(f"session already moved on: {resp.text}")
        if resp.status_code >= 400:
            raise SessionServiceError(f"session-service returned {resp.status_code}: {resp.text}")
        return resp.json()
