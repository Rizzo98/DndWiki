"""Client for the content-service internal API (service-to-service).

Deleting a session means forgetting everything it produced. session-service
owns the session row, its recording and its speakers; content-service owns the
session's draft summary, its generation jobs and its proposed change set.
DELETE /internal/content/sessions/{id} drops those, and REFUSES (409) while the
session's content already exists in the wiki — a session whose pages are live
must not be deleted, or the pages would point at a session that is gone.
"""

from __future__ import annotations

import logging

import httpx
from dnd_common.auth import service_token

from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)


class SessionNotDeletable(Exception):
    """content-service refused: the session's content is already in the wiki."""


class ContentServiceError(Exception):
    """content-service call failed (network, auth, unexpected status, ...)."""


class ContentServiceClient:
    """Async HTTP client for the content-service internal endpoints."""

    def __init__(self, base_url: str, settings: ServiceSettings) -> None:
        self._base_url = base_url.rstrip("/")
        self._settings = settings

    async def delete_session_data(self, session_id: str) -> dict:
        """DELETE /internal/content/sessions/{id} — purge the session's rows.

        Raises SessionNotDeletable on 409 (wiki content exists) and
        ContentServiceError on anything else that goes wrong.
        """
        try:
            async with httpx.AsyncClient(timeout=self._settings.service_timeout_sec) as client:
                resp = await client.delete(
                    f"{self._base_url}/internal/content/sessions/{session_id}",
                    headers={"Authorization": f"Bearer {service_token(self._settings)}"},
                )
        except httpx.HTTPError as exc:
            logger.warning("content-service unreachable: %s", exc)
            raise ContentServiceError("content-service unreachable") from exc
        if resp.status_code == 409:
            raise SessionNotDeletable(_detail(resp))
        if resp.status_code >= 400:
            raise ContentServiceError(
                f"content-service returned {resp.status_code}: {resp.text}"
            )
        return resp.json()


def _detail(resp: httpx.Response) -> str:
    """The FastAPI 'detail' of an error response (fallback: the raw body)."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text
    if isinstance(body, dict) and body.get("detail"):
        return str(body["detail"])
    return resp.text
