"""Keycloak JWT validation and authorization dependencies for FastAPI.

Every service enforces authentication locally (no gateway single point of
failure): the access token is verified against the realm's JWKS, then role and
campaign-membership checks run as FastAPI dependencies.
"""

import logging
import threading
import time
from functools import lru_cache
from typing import Any

import httpx
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

#: How long a fetched JWKS stays valid before we re-fetch from Keycloak.
JWKS_TTL_SEC = 300
#: url -> (fetched_at, jwks). Only successful fetches are cached, so a
#: transient Keycloak outage never serves a poisoned (stale-empty) cache.
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks_lock = threading.Lock()

_bearer = HTTPBearer(auto_error=False)


@lru_cache(maxsize=8)
def _jwks_client(jwks_url: str) -> httpx.Client:
    """Cached HTTP client for the realm's JWKS endpoint (keyed by URL).

    Keyed on the URL string, not the Settings instance: pydantic models are
    unhashable, which made the previous @lru_cache on Settings raise
    "TypeError: unhashable type: 'Settings'" on the first authenticated call.
    """
    return httpx.Client(base_url=jwks_url, timeout=5.0)


def _get_jwks(jwks_url: str) -> dict[str, Any]:
    """JWKS for a realm, cached for JWKS_TTL_SEC (re-fetched on expiry)."""
    now = time.monotonic()
    with _jwks_lock:
        cached = _jwks_cache.get(jwks_url)
        if cached is not None and now - cached[0] < JWKS_TTL_SEC:
            return cached[1]
    resp = _jwks_client(jwks_url).get("")
    resp.raise_for_status()
    jwks = resp.json()
    with _jwks_lock:
        _jwks_cache[jwks_url] = (time.monotonic(), jwks)
    return jwks


def _fetch_jwks(settings: Settings) -> dict[str, Any]:
    return _get_jwks(settings.jwks_url)


def _resolve_signing_key(jwks: dict[str, Any], token: str) -> Any:
    """Pick the JWK matching the token's kid (first key if no kid) and prepare it.

    PyJWT's jwt.decode does not accept a raw JWKS dict — passing it raises
    "TypeError: Expecting a PEM-formatted key". The signing key must be
    resolved to a prepared key object (PyJWK.key) first.
    """
    header = jwt.get_unverified_header(token)
    keys = jwks.get("keys") or []
    if not keys:
        raise jwt.exceptions.DecodeError("JWKS contains no keys")
    kid = header.get("kid")
    if kid is not None:
        for jwk in keys:
            if jwk.get("kid") == kid:
                return jwt.PyJWK(jwk, algorithm=header.get("alg")).key
        raise jwt.exceptions.PyJWKClientError(
            f"Unable to find a signing key that matches: {kid}"
        )
    return jwt.PyJWK(keys[0], algorithm=header.get("alg")).key


def decode_token(token: str, settings: Settings | None = None) -> dict[str, Any]:
    """Validate a JWT against the Keycloak realm and return its claims."""
    settings = settings or get_settings()
    jwks = _fetch_jwks(settings)
    try:
        return jwt.decode(
            token,
            _resolve_signing_key(jwks, token),
            algorithms=["RS256"],
            audience="account",
            options={"verify_aud": False},
        )
    except jwt.PyJWTError as exc:  # invalid token, expired, bad signature
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        ) from exc


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """FastAPI dependency: authenticated user claims (raises 401 otherwise)."""
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token"
        )
    return decode_token(creds.credentials, settings)


def require_roles(*roles: str):
    """Dependency factory: require at least one Keycloak realm role."""

    def dependency(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        user_roles = set(user.get("realm_access", {}).get("roles", []))
        if not user_roles.intersection(roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of roles: {', '.join(roles)}",
            )
        return user

    return dependency


#: Convenience dependencies for the two platform roles.
require_dm = require_roles("dm")
require_player_or_dm = require_roles("player", "dm")

#: Realm role that unlocks developer/debug tooling (driving the summary
#: review of any campaign). Assigned manually in Keycloak.
DEV_ROLE = "dev"


def realm_roles(claims: dict[str, Any]) -> set[str]:
    """The token's realm roles ('' claims tolerated)."""
    roles = ((claims or {}).get("realm_access") or {}).get("roles")
    return {str(r) for r in roles} if isinstance(roles, list) else set()


def is_developer(claims: dict[str, Any]) -> bool:
    """True when the token carries the 'dev' realm role."""
    return DEV_ROLE in realm_roles(claims)


def service_token(settings: Settings | None = None) -> str:
    """Client-credentials token for service-to-service calls (dnd-services)."""
    settings = settings or get_settings()
    resp = httpx.post(
        f"{settings.keycloak_url}/realms/{settings.keycloak_realm}"
        "/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": settings.keycloak_service_client_id,
            "client_secret": settings.keycloak_service_client_secret,
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]
