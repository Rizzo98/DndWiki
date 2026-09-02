"""Tests for the campaign-service client (no real network)."""

import httpx
import pytest

from app.clients import campaign_service as mod
from app.clients.campaign_service import (
    CampaignNotFound,
    CampaignServiceClient,
    CampaignServiceError,
)
from app.core.config import ServiceSettings


def _settings(**overrides) -> ServiceSettings:
    defaults = {
        "campaign_service_url": "http://campaign-service:8000",
    }
    defaults.update(overrides)
    return ServiceSettings(**defaults)


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.calls.append((url, headers))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _patch_http(monkeypatch, responses):
    fake = FakeHTTP(responses)
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: fake)
    return fake


CAMPAIGN_ID = "22222222-2222-2222-2222-222222222222"


async def test_get_campaign(monkeypatch):
    body = {"id": CAMPAIGN_ID, "name": "The Fellowship", "description": "Quest", "language": "it", "dm_user_id": "u1"}
    fake = _patch_http(monkeypatch, [httpx.Response(200, json=body)])

    out = await CampaignServiceClient(_settings()).get_campaign(CAMPAIGN_ID)

    assert out == body
    url, headers = fake.calls[0]
    assert url == f"http://campaign-service:8000/internal/campaigns/{CAMPAIGN_ID}"
    assert headers["Authorization"].startswith("Bearer ")


async def test_list_members(monkeypatch):
    body = [{"id": "m1", "role": "dm", "character_name": "Narrator"}, {"id": "m2", "role": "player", "character_name": "Aragorn"}]
    fake = _patch_http(monkeypatch, [httpx.Response(200, json=body)])

    out = await CampaignServiceClient(_settings()).list_members(CAMPAIGN_ID)

    assert out == body
    url, _headers = fake.calls[0]
    assert url == f"http://campaign-service:8000/internal/campaigns/{CAMPAIGN_ID}/members"


async def test_get_campaign_not_found(monkeypatch):
    _patch_http(monkeypatch, [httpx.Response(404, text="not found")])
    with pytest.raises(CampaignNotFound):
        await CampaignServiceClient(_settings()).get_campaign(CAMPAIGN_ID)


async def test_get_campaign_unreachable(monkeypatch):
    _patch_http(monkeypatch, [httpx.ConnectError("connection refused")])
    with pytest.raises(CampaignServiceError):
        await CampaignServiceClient(_settings()).get_campaign(CAMPAIGN_ID)
