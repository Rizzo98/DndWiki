"""Regression tests for the real CampaignServiceClient (no fakes).

Covers the service-token cache (the _token attribute/method collision bug),
the Authorization header, and HTTP-error mapping.
"""

import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.clients.campaigns import CampaignServiceClient, MembershipUnavailable
from app.core.config import ServiceSettings


class _FakeResponse:
    def __init__(self, status_code: int = 200, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom", request=httpx.Request("GET", "http://campaign.test/internal/membership"),
                response=httpx.Response(self.status_code),
            )

    def json(self) -> dict:
        return self._body


class _FakeAsyncClient:
    """Async-context-manager stand-in for httpx.AsyncClient."""

    def __init__(self, response=None, exc: Exception | None = None) -> None:
        self._response = response
        self._exc = exc
        self.requests: list[tuple[tuple, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, *args, **kwargs):
        if self._exc is not None:
            raise self._exc
        self.requests.append((args, kwargs))
        return self._response


def _make_client(monkeypatch, response=None, exc=None):
    fake = _FakeAsyncClient(response=response, exc=exc)
    monkeypatch.setattr("app.clients.campaigns.httpx.AsyncClient", lambda **kw: fake)
    client = CampaignServiceClient("http://campaign.test", ServiceSettings())
    return client, fake


def _patch_token(monkeypatch, value: str = "tok-123"):
    calls = []

    def fake_service_token(settings):
        calls.append(settings)
        return value

    monkeypatch.setattr("app.clients.campaigns.service_token", fake_service_token)
    return calls


async def test_token_is_cached_until_expiry(monkeypatch):
    calls = _patch_token(monkeypatch, "tok-1")
    client, _ = _make_client(monkeypatch)

    assert client._token() == "tok-1"
    assert client._token() == "tok-1"
    assert len(calls) == 1  # cached; no second Keycloak round-trip

    # simulate token expiry -> refetches
    client._token_expires = datetime.now(UTC) - timedelta(minutes=1)
    assert client._token() == "tok-1"
    assert len(calls) == 2


async def test_check_membership_sends_bearer_header_and_returns_role(monkeypatch):
    _patch_token(monkeypatch, "tok-abc")
    client, fake = _make_client(monkeypatch, response=_FakeResponse(200, {"role": "dm"}))
    campaign_id, user_id = uuid.uuid4(), uuid.uuid4()

    role = await client.check_membership(campaign_id, user_id)

    assert role == "dm"
    args, kwargs = fake.requests[0]
    url = args[0] if args else kwargs.get("url")
    assert str(url) == "http://campaign.test/internal/membership"
    assert kwargs["params"] == {"campaign_id": str(campaign_id), "user_id": str(user_id)}
    assert kwargs["headers"]["Authorization"] == "Bearer tok-abc"


async def test_check_membership_404_returns_none(monkeypatch):
    _patch_token(monkeypatch)
    client, _ = _make_client(monkeypatch, response=_FakeResponse(404))
    assert await client.check_membership(uuid.uuid4(), uuid.uuid4()) is None


async def test_check_membership_connection_error_raises_unavailable(monkeypatch):
    _patch_token(monkeypatch)
    client, _ = _make_client(
        monkeypatch, exc=httpx.ConnectError("campaign-service refused connection")
    )
    with pytest.raises(MembershipUnavailable):
        await client.check_membership(uuid.uuid4(), uuid.uuid4())


async def test_assert_member_raises_for_non_member(monkeypatch):
    _patch_token(monkeypatch)
    client, _ = _make_client(monkeypatch, response=_FakeResponse(404))
    with pytest.raises(ValueError):
        await client.assert_member(uuid.uuid4(), uuid.uuid4())


async def test_assert_dm_raises_for_player(monkeypatch):
    _patch_token(monkeypatch)
    client, _ = _make_client(monkeypatch, response=_FakeResponse(200, {"role": "player"}))
    with pytest.raises(PermissionError):
        await client.assert_dm(uuid.uuid4(), uuid.uuid4())


async def test_membership_cached_within_ttl(monkeypatch):
    _patch_token(monkeypatch)
    client, fake = _make_client(monkeypatch, response=_FakeResponse(200, {"role": "dm"}))
    campaign_id, user_id = uuid.uuid4(), uuid.uuid4()

    assert await client.check_membership(campaign_id, user_id) == "dm"
    assert await client.check_membership(campaign_id, user_id) == "dm"
    assert len(fake.requests) == 1  # second call served from cache


async def test_membership_refetched_after_ttl(monkeypatch):
    _patch_token(monkeypatch)
    client, fake = _make_client(monkeypatch, response=_FakeResponse(200, {"role": "player"}))
    campaign_id, user_id = uuid.uuid4(), uuid.uuid4()

    await client.check_membership(campaign_id, user_id)
    assert len(fake.requests) == 1

    # force expiry -> refetch
    client._membership_cache[(campaign_id, user_id)] = (
        time.monotonic() - client._membership_ttl - 1,
        "player",
    )
    await client.check_membership(campaign_id, user_id)
    assert len(fake.requests) == 2


async def test_membership_error_not_cached(monkeypatch):
    _patch_token(monkeypatch)
    client, _ = _make_client(monkeypatch, exc=httpx.ConnectError("campaign down"))
    campaign_id, user_id = uuid.uuid4(), uuid.uuid4()

    with pytest.raises(MembershipUnavailable):
        await client.check_membership(campaign_id, user_id)
    with pytest.raises(MembershipUnavailable):
        await client.check_membership(campaign_id, user_id)
    assert client._membership_cache == {}  # transient errors never cached
