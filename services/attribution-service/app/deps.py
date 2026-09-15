"""FastAPI dependencies: who may call what.

Two audiences, two rules:

- the internal pipeline API is service-to-service and takes a client-credentials
  token (require_service);
- the review API is for the DM, and every route under it reads a session's
  transcript and can WRITE its attribution. It validates the caller's own token
  (require_session_member) instead of trusting the gateway, exactly as every
  other service does - the gateway is not an authentication boundary here.
"""

from __future__ import annotations

import uuid

from dnd_common.auth import current_user, decode_token, is_developer
from dnd_common.db import get_session
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.base import MembershipUnavailable
from app.clients.campaign_service import CampaignServiceClient
from app.core.config import ServiceSettings, get_settings
from app.models import SessionBeliefStats

_bearer = HTTPBearer(auto_error=False)


def require_service(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: ServiceSettings = Depends(get_settings),
) -> None:
    """Require a dnd-services client-credentials token (service-to-service).

    The token is validated against the realm JWKS and the azp claim must name the
    service client, so a DM's own token cannot drive the pipeline API.
    """
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token"
        )
    claims = decode_token(creds.credentials, settings)
    if claims.get("azp") != settings.keycloak_service_client_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Service token required"
        )


async def require_session_member(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_session),
    settings: ServiceSettings = Depends(get_settings),
) -> None:
    """The caller must be a member of the campaign the session belongs to.

    Attached to the whole review router rather than to each handler: the rule is
    the same for all of them, and a rule repeated ten times is a rule that will
    be forgotten eleven times.

    A session with no attribution yet passes through - there is nothing to read
    or write, and the handler answers its own 404 with a message the DM can act
    on. Developers may look at any campaign (the same escape hatch the session
    and content APIs have).
    """
    try:
        session_uuid = uuid.UUID(session_id)
    except ValueError:
        return  # not a session id at all; the handler's own 404 is the answer
    stats = await db.get(SessionBeliefStats, session_uuid)
    if stats is None or stats.campaign_id is None:
        return
    if is_developer(user):
        return
    user_id = str(user.get("sub") or "")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing subject claim"
        )
    try:
        role = await CampaignServiceClient(settings).check_membership(
            str(stats.campaign_id), user_id
        )
    except MembershipUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="campaign-service unavailable",
        ) from exc
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not a member of this campaign",
        )
