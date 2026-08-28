"""Regression tests for the shared JWT auth layer (dnd_common.auth).

The bug: _jwks_client was @lru_cache'd on a pydantic Settings instance, which
is unhashable -> "TypeError: unhashable type: 'Settings'" on the first
authenticated request (GET /api/campaigns 500). These tests pin the fixed
behavior without needing a live Keycloak.
"""

import asyncio

import pytest
from dnd_common import auth as dnd_auth
from fastapi import HTTPException

from app.core.config import get_settings


def test_jwks_client_keyed_on_url_not_settings():
    settings = get_settings()
    client = dnd_auth._jwks_client(settings.jwks_url)
    try:
        assert str(client.base_url).rstrip("/") == settings.jwks_url
        # lru_cache works with the hashable string key: same instance back
        assert dnd_auth._jwks_client(settings.jwks_url) is client
    finally:
        client.close()


def test_fetch_jwks_accepts_settings_instance(monkeypatch):
    """Previously-crashing path: _fetch_jwks(settings) -> _jwks_client(settings)."""
    settings = get_settings()

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"keys": [{"kid": "k1"}]}

    class FakeClient:
        def get(self, path):
            return FakeResponse()

    monkeypatch.setattr(dnd_auth, "_jwks_client", lambda url: FakeClient())
    assert dnd_auth._fetch_jwks(settings) == {"keys": [{"kid": "k1"}]}


def test_decode_token_invalid_raises_401(monkeypatch):
    """decode_token keeps its contract: unverifiable token -> HTTP 401 (no network)."""
    settings = get_settings()
    monkeypatch.setattr(dnd_auth, "_fetch_jwks", lambda s: {"keys": []})
    with pytest.raises(HTTPException) as exc:
        dnd_auth.decode_token("garbage-token", settings)
    assert exc.value.status_code == 401


def test_current_user_missing_token_401():
    """current_user dependency: no bearer header -> 401 (never reaches JWKS)."""

    from dnd_common.auth import current_user

    async def _run():
        return await current_user(creds=None, settings=get_settings())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_run())
    assert exc.value.status_code == 401


def _rs256_jwks():
    """Generate an RSA keypair and return (private_key, jwks_dict)."""
    import json

    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = "k1"
    return private_key, {"keys": [jwk]}


def test_decode_token_rs256_roundtrip(monkeypatch):
    """A real RS256-signed token verifies end-to-end (no Keycloak needed).

    Regression for the frontend's "Invalid token: Algorithm not supported":
    PyJWT only registers RS256 when the 'cryptography' package is installed;
    without it, decode_token fails before any signature check. This test
    fails at import time if cryptography is missing.
    """
    import jwt as pyjwt

    private_key, jwks = _rs256_jwks()
    token = pyjwt.encode(
        {"sub": "user-1", "realm_access": {"roles": ["player"]}},
        private_key,
        algorithm="RS256",
        headers={"kid": "k1"},
    )
    monkeypatch.setattr(dnd_auth, "_fetch_jwks", lambda settings: jwks)

    claims = dnd_auth.decode_token(token, get_settings())
    assert claims["sub"] == "user-1"
    assert claims["realm_access"]["roles"] == ["player"]


def test_decode_token_rs256_tampered_raises_401(monkeypatch):
    """A valid RS256 key but tampered payload must fail verification (401)."""
    import jwt as pyjwt

    private_key, jwks = _rs256_jwks()
    token = pyjwt.encode({"sub": "user-1"}, private_key, algorithm="RS256", headers={"kid": "k1"})
    # flip characters inside the payload section of the token
    header, payload, signature = token.split(".")
    tampered = f"{header}.{payload[:-4]}XXXX{payload[-4:]}.{signature}"
    monkeypatch.setattr(dnd_auth, "_fetch_jwks", lambda settings: jwks)

    with pytest.raises(HTTPException) as exc:
        dnd_auth.decode_token(tampered, get_settings())
    assert exc.value.status_code == 401


def test_decode_token_rs256_wrong_kid_raises_401(monkeypatch):
    """A token signed by an unknown kid must fail cleanly (401)."""
    import jwt as pyjwt

    private_key, jwks = _rs256_jwks()  # JWKS only contains kid "k1"
    token = pyjwt.encode({"sub": "user-1"}, private_key, algorithm="RS256", headers={"kid": "k9"})
    monkeypatch.setattr(dnd_auth, "_fetch_jwks", lambda settings: jwks)

    with pytest.raises(HTTPException) as exc:
        dnd_auth.decode_token(token, get_settings())
    assert exc.value.status_code == 401
