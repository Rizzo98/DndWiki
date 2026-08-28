"""Client for the user-service internal API (service-to-service).

Resolves user ids (Keycloak subjects) to display names so the worker can build
the 'named transcript' view: [HH:MM:SS] DISPLAY_NAME: text.
"""

from __future__ import annotations

import logging

import httpx
from dnd_common.auth import service_token

from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)


class UserServiceError(Exception):
    """user-service call failed (network, auth, unexpected 4xx/5xx, ...)."""


class UserServiceClient:
    """Async HTTP client for user-service /internal/users."""

    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings
        self._base_url = settings.user_service_url.rstrip("/")

    async def display_names(self, user_ids: list[str]) -> dict[str, str]:
        """Batch-resolve user ids -> display names (missing ids are skipped).

        Returns {'<user_id>': '<display_name>', ...} for the ids the
        user-service knows about.
        """
        if not user_ids:
            return {}
        token = service_token(self._settings)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=self._settings.service_timeout_sec) as client:
                resp = await client.get(
                    f"{self._base_url}/internal/users",
                    params=[("ids", uid) for uid in user_ids],
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            logger.warning("user-service request failed: %s", exc)
            raise UserServiceError(f"user-service request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise UserServiceError(f"user-service returned {resp.status_code}: {resp.text}")
        users = resp.json().get("users", {})
        return {
            uid: profile.get("display_name", uid)
            for uid, profile in users.items()
            if isinstance(profile, dict)
        }
