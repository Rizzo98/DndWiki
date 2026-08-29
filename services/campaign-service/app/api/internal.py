"""Internal service API (service-to-service, dnd-services client token).

Other services (session/wiki/...) never read dnd_campaigns tables directly;
they ask campaign-service for membership through this endpoint.
"""

from __future__ import annotations

from uuid import UUID

from dnd_common.db import get_session
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.deps import require_service
from app.schemas import CampaignInternalOut, CampaignMemberOut, MembershipOut

router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(require_service)],
)


@router.get("/membership", response_model=MembershipOut)
async def check_membership(
    campaign_id: UUID,
    user_id: UUID,
    db: AsyncSession = Depends(get_session),
):
    """Return the user's role ("dm" | "player") in a campaign.

    404 when the campaign does not exist; role is null when the user is not
    a member. Consumers (e.g. session-service's CampaignServiceClient) treat
    404 as "not a member".
    """
    role = await services.membership_role(db, campaign_id, user_id)
    return MembershipOut(campaign_id=campaign_id, user_id=user_id, role=role)


@router.get("/campaigns/{campaign_id}", response_model=CampaignInternalOut)
async def get_campaign_internal(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_session),
):
    """Return the campaign's DM identity for internal consumers.

    Used by content-service so wiki generation can tell the DM's speaker
    (the narrator - out of the world) apart from the players' characters.
    404 when the campaign does not exist.
    """
    campaign = await services.get_campaign(db, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
    return CampaignInternalOut(id=campaign.id, name=campaign.name, dm_user_id=campaign.dm_user_id)


@router.get("/campaigns/{campaign_id}/members", response_model=list[CampaignMemberOut])
async def list_campaign_members_internal(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_session),
):
    """Full roster of a campaign for internal consumers.

    Used by refiner-service so the LLM contextual pass knows the campaign's
    cast: character names + physical descriptions let it correct misheard
    names and attribute speakers to the right person. 404 when the campaign
    does not exist.
    """
    return await services.list_members(db, campaign_id)


@router.get("/members/{member_id}", response_model=CampaignMemberOut)
async def get_member_by_id(
    member_id: UUID,
    campaign_id: UUID,
    db: AsyncSession = Depends(get_session),
):
    """Resolve a member row scoped to a campaign (404 otherwise).

    Used by session-service when the DM names a speaker: it validates the
    member belongs to the session campaign and returns the display data
    (player_name, optional user link) for the speakers.assigned event.
    """
    member = await services.get_member_by_id(db, member_id)
    if member is None or member.campaign_id != campaign_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    return member