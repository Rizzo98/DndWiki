"""campaign-service client: membership & role checks.

wiki-service never reads dnd_campaigns tables directly; every query is scoped
by campaign_id and authorized by asking campaign-service
GET /internal/membership with a service token.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from dnd_common.auth import service_token

from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)


class MembershipUnavailable(RuntimeError):
    """campaign-service could not be reached; treat as a 503."""


class CampaignServiceClient:
    """Calls campaign-service internal endpoints with a cached service token."""

    def __init__(self, base_url: str, settings: ServiceSettings) -> None:
        self._base_url = base_url.rstrip("/")
        self._settings = settings
        self._cached_token: str | None = None
        self._token_expires: datetime = datetime.fromtimestamp(0, tz=UTC)

    def _token(self) -> str:
        # Keycloak access tokens are short-lived; refresh 30s before expiry
        if self._cached_token is None or datetime.now(UTC) >= self._token_expires - timedelta(seconds=30):
            self._cached_token = service_token(self._settings)
            self._token_expires = datetime.now(UTC) + timedelta(minutes=4)
        return self._cached_token

    async def check_membership(self, campaign_id: UUID, user_id: UUID) -> str | None:
        """Return the user's role ("dm" | "player") in a campaign, or None."""
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
