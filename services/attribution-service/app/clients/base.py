"""Shared HTTP plumbing for the internal service clients."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from dnd_common.auth import service_token

from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)


class ConflictTransition(Exception):
    """session-service rejected the status change (the session already moved on).

    Raised on 409: the state machine refused the transition, which happens on
    redelivered jobs. Callers treat it as "already handled" and ack.
    """


class ServiceError(Exception):
    """A downstream service call failed (network, auth, unexpected status)."""


class MembershipUnavailable(Exception):
    """campaign-service could not answer, so membership is UNKNOWN.

    Kept separate from "not a member" on purpose: one is a caller who must be
    refused, the other is an outage. Answering 403 for an outage would tell a
    real DM they do not belong to their own campaign.
    """


class BaseClient:
    """Async HTTP client authenticated with a dnd-services client-credentials token."""

    def __init__(self, settings: ServiceSettings, base_url: str) -> None:
        self._settings = settings
        self._base_url = base_url.rstrip("/")

    async def request(
        self, method: str, path: str, *, json: Any = None, params: dict | None = None
    ) -> Any:
        token = service_token(self._settings)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.service_timeout_sec
            ) as client:
                response = await client.request(
                    method,
                    f"{self._base_url}{path}",
                    json=json,
                    params=params,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise ServiceError(f"{self._base_url}{path} failed: {exc}") from exc
        if response.status_code == 409:
            raise ConflictTransition(response.text)
        if response.status_code >= 400:
            raise ServiceError(
                f"{self._base_url}{path} returned {response.status_code}: {response.text}"
            )
        if not response.content:
            return None
        return response.json()
