"""FastAPI dependencies: service-token auth for the internal pipeline API."""

from __future__ import annotations

from dnd_common.auth import decode_token
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import ServiceSettings, get_settings

_bearer = HTTPBearer(auto_error=False)


def require_service(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: ServiceSettings = Depends(get_settings),
) -> None:
    """Require a dnd-services client-credentials token (service-to-service).

    The token is validated against the realm JWKS; the azp claim must name the
    service client (Keycloak sets azp to the client id for the client_credentials
    grant).
    """
    if creds is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    claims = decode_token(creds.credentials, settings)
    if claims.get("azp") != settings.keycloak_service_client_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Service token required")
