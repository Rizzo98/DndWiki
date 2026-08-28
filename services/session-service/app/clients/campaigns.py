"""campaign-service client: membership & role checks.

session-service never reads dnd_campaigns tables directly; it asks
campaign-service GET /internal/membership with a service token.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from dnd_common.auth import service_token

from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)


class MembershipUnavailable(RuntimeError):
    """campaign-service could not be reached; treat as a 503."""


class CampaignServiceClient:
    """Calls campaign-service internal endpoints with cached token + membership."""

    def __init__(self, base_url: str, settings: ServiceSettings) -> None:
        self._base_url = base_url.rstrip("/")
        self._settings = settings
        self._cached_token: str | None = None
        self._token_expires: datetime = datetime.fromtimestamp(0, tz=UTC)
        # (campaign_id, user_id) -> (fetched_at, role|None); only successful
        # lookups are cached, never MembershipUnavailable (transient).
        self._membership_cache: dict[tuple[UUID, UUID], tuple[float, str | None]] = {}
        self._membership_ttl = settings.membership_cache_ttl_sec

    def _token(self) -> str:
        # Keycloak access tokens are short-lived; refresh 30s before expiry
        if self._cached_token is None or datetime.now(UTC) >= self._token_expires - timedelta(seconds=30):
            self._cached_token = service_token(self._settings)
            self._token_expires = datetime.now(UTC) + timedelta(minutes=4)
        return self._cached_token

    async def _check_membership_uncached(self, campaign_id: UUID, user_id: UUID) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self._base_url}/internal/membership",
                    params={"campaign_id": str(campaign_id), "user_id": str(user_id)},
                    headers={"Authorization": f"Bearer {self._token()}"},
                )
        except httpx.HTTPError as exc:
            logger.warning("campaign-service unreachable: %s", exc)
            raise MembershipUnavailable("campaign-service unreachable") from exc
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("role")

    async def check_membership(self, campaign_id: UUID, user_id: UUID) -> str | None:
        """Return the user's role ("dm" | "player") in a campaign, or None.

        Cached per (campaign, user) for membership_cache_ttl_sec so the UI
        progress-poll loop stops hammering campaign-service on every request.
        Only successful lookups are cached — a transient campaign-service
        outage re-checks on the next call instead of serving stale 403s.
        """
        key = (campaign_id, user_id)
        now = time.monotonic()
        cached = self._membership_cache.get(key)
        if cached is not None and now - cached[0] < self._membership_ttl:
            return cached[1]
        role = await self._check_membership_uncached(campaign_id, user_id)
        self._membership_cache[key] = (time.monotonic(), role)
        return role

    async def assert_member(self, campaign_id: UUID, user_id: UUID) -> str:
        """Raise ValueError unless the user is a member; return their role."""
        role = await self.check_membership(campaign_id, user_id)
        if role is None:
            raise ValueError("not a member of this campaign")
        return role

    async def assert_dm(self, campaign_id: UUID, user_id: UUID) -> None:
        """Raise PermissionError unless the user is the campaign DM."""
        role = await self.assert_member(campaign_id, user_id)
        if role != "dm":
            raise PermissionError("dm role required")

    async def get_member(self, campaign_id: UUID, member_id: UUID) -> dict[str, Any]:
        """Resolve a campaign member by its surrogate id.

        Raises ValueError when the member does not exist in the campaign;
        used to validate speaker assignments and to fetch the member
        display data (player_name, optional user link) for the
        speakers.assigned event.
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self._base_url}/internal/members/{member_id}",
                    params={"campaign_id": str(campaign_id)},
                    headers={"Authorization": f"Bearer {self._token()}"},
                )
        except httpx.HTTPError as exc:
            logger.warning("campaign-service unreachable: %s", exc)
            raise MembershipUnavailable("campaign-service unreachable") from exc
        if resp.status_code == 404:
            raise ValueError("member not found in this campaign")
        resp.raise_for_status()
        return resp.json()
