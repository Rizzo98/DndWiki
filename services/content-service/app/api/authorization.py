"""Shared authorization for the content-service session endpoints.

The session-summary control endpoints (rewrite with feedback, confirmation)
are DM-only, with the Keycloak 'dev' realm role as the escape hatch: the dev
role is not scoped to a campaign, so a developer can drive the review of any
campaign.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from dnd_common.auth import is_developer
from fastapi import HTTPException
from fastapi import status as http_status

from app.clients.campaign_service import CampaignServiceClient, MembershipUnavailable


async def authorize_dm_or_dev(
    user: dict[str, Any], campaign_id: str, campaign_client: CampaignServiceClient
) -> None:
    """Raise 403 (not the DM) / 503 (membership unknown) unless allowed.

    A failure to reach campaign-service is NOT a denial: answering 503 keeps
    the caller from mistaking an outage for a permission error.
    """
    if is_developer(user):
        return
    user_id = str(user.get("sub") or "")
    try:
        await campaign_client.assert_dm(UUID(campaign_id), UUID(user_id))
    except PermissionError as exc:
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except MembershipUnavailable as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="campaign-service unavailable",
        ) from exc
