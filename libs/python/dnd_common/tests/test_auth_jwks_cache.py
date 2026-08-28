"""JWKS caching tests (dnd_common.auth).

Verifies the Keycloak JWKS is fetched once per TTL window instead of on every
decode_token call, and that failed fetches are never cached.
"""

import time

import httpx
import pytest

from dnd_common import auth
from dnd_common.config import Settings


class _FakeResponse:
    def __init__(self, body: dict) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._body


class _FakeJwksClient:
    def __init__(self) -> None:
        self.calls = 0
        self.fail_first = 0

    def get(self, path: str) -> _FakeResponse:
        self.calls += 1
        if self.calls <= self.fail_first:
            raise httpx.ConnectError("keycloak unreachable")
        return _FakeResponse({"keys": [{"kid": "k1", "kty": "RSA"}]})


def _install_fake(monkeypatch, fake: _FakeJwksClient) -> None:
    monkeypatch.setattr(auth, "_jwks_client", lambda url: fake)
    monkeypatch.setattr(auth, "_jwks_cache", {})


def test_jwks_fetched_once_within_ttl(monkeypatch):
    fake = _FakeJwksClient()
    _install_fake(monkeypatch, fake)
    settings = Settings()

    first = auth._fetch_jwks(settings)
    second = auth._fetch_jwks(settings)

    assert first == second
    assert fake.calls == 1  # cached; no second Keycloak round-trip


def test_jwks_refetched_after_ttl(monkeypatch):
    fake = _FakeJwksClient()
    _install_fake(monkeypatch, fake)
    settings = Settings()

    auth._fetch_jwks(settings)
    assert fake.calls == 1

    # expire the cached entry, forcing a re-fetch
    auth._jwks_cache[settings.jwks_url] = (time.monotonic() - auth.JWKS_TTL_SEC - 1, {})
    auth._fetch_jwks(settings)

    assert fake.calls == 2


def test_failed_fetch_is_not_cached(monkeypatch):
    fake = _FakeJwksClient()
    fake.fail_first = 1  # first call fails, subsequent succeed
    _install_fake(monkeypatch, fake)
    settings = Settings()

    with pytest.raises(httpx.ConnectError):
        auth._fetch_jwks(settings)
    assert auth._jwks_cache == {}  # nothing poisoned

    auth._fetch_jwks(settings)  # succeeds
    auth._fetch_jwks(settings)  # served from cache
    assert fake.calls == 2
