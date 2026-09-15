"""campaign-service internal API: the roster."""

from __future__ import annotations

import logging
from typing import Any

from app.clients.base import BaseClient, MembershipUnavailable, ServiceError
from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)


class CampaignServiceClient(BaseClient):
    def __init__(self, settings: ServiceSettings) -> None:
        super().__init__(settings, settings.campaign_service_url)

    async def list_members(self, campaign_id: str) -> list[dict[str, Any]]:
        """The campaign roster; [] when unreachable (text-only attribution).

        The roster is what turns a posterior into a NAME. Without it the engine
        still attributes - to 'someone in the campaign' at best - so a failure
        here degrades the output rather than failing the session.
        """
        try:
            payload = await self.request(
                "GET", f"/internal/campaigns/{campaign_id}/members"
            )
        except ServiceError:
            logger.warning("could not read the roster of campaign %s", campaign_id)
            return []
        return payload if isinstance(payload, list) else payload.get("members", [])

    async def check_membership(self, campaign_id: str, user_id: str) -> str | None:
        """The caller's role in a campaign ("dm" | "player"), or None.

        This is the AUTHORIZATION half of the review API: reading a session's
        review means reading its transcript, and answering one WRITES its
        attribution, so an authenticated stranger is not enough.

        A 404 from campaign-service means the campaign does not exist, which is
        not a member either. A transport failure is different and raises: see
        MembershipUnavailable.
        """
        try:
            payload = await self.request(
                "GET",
                "/internal/membership",
                params={"campaign_id": campaign_id, "user_id": user_id},
            )
        except ServiceError as exc:
            if "404" in str(exc):
                return None
            raise MembershipUnavailable(str(exc)) from exc
        if not isinstance(payload, dict):
            return None
        role = payload.get("role")
        return str(role) if role else None
