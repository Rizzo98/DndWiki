"""FastAPI dependencies: service-token auth + app.state accessors."""

from __future__ import annotations

from dnd_common.auth import decode_token
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.broker import EventPublisher
from app.clients.campaigns import CampaignServiceClient
from app.clients.content import ContentServiceClient
from app.core.config import ServiceSettings, get_settings
from app.storage import ObjectStorage

_bearer = HTTPBearer(auto_error=False)


def get_publisher(request: Request) -> EventPublisher:
    return request.app.state.publisher


def get_storage(request: Request) -> ObjectStorage:
    return request.app.state.storage


def get_campaign_client(request: Request) -> CampaignServiceClient:
    return request.app.state.campaign_client


def get_content_client(request: Request) -> ContentServiceClient:
    return request.app.state.content_client


def require_service(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: ServiceSettings = Depends(get_settings),
) -> None:
    """Require a dnd-services client-credentials token (worker/service calls).

    The token is validated against the realm JWKS; the azp claim must name
    the service client (Keycloak sets azp to the client id for the
    client_credentials grant).
    """
    if creds is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    claims = decode_token(creds.credentials, settings)
    if claims.get("azp") != settings.keycloak_service_client_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Service token required")
