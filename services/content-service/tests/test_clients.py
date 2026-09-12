"""Tests for the outbound service clients (session/wiki/user)."""

import httpx
import pytest

from app.clients import session_service as session_mod
from app.clients import user_service as user_mod
from app.clients import wiki_service as wiki_mod
from app.clients.session_service import (
    ConflictTransition,
    SessionServiceClient,
    SessionServiceError,
)
from app.clients.user_service import UserServiceClient, UserServiceError
from app.clients.wiki_service import WikiServiceClient, WikiServiceError
from app.core.config import ServiceSettings


class FakeAsyncClient:
    """Replaces httpx.AsyncClient: returns a canned response or raises."""

    def __init__(self, response=None, error=None, method="patch"):
        self.response = response
        self.error = error
        self.method = method
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def patch(self, url, json=None, headers=None):
        self.calls.append((url, json, headers))
        return self._respond()

    async def post(self, url, json=None, headers=None):
        self.calls.append((url, json, headers))
        return self._respond()

    async def get(self, url, params=None, headers=None):
        self.calls.append((url, params, headers))
        return self._respond()

    async def request(self, method, url, json=None, headers=None, params=None):
        # generic entry point used by SessionServiceClient._request
        self.calls.append((url, params if params is not None else json, headers))
        return self._respond()

    def _respond(self):
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture
def settings():
    return ServiceSettings(
        session_service_url="http://session.test",
        wiki_service_url="http://wiki.test",
        user_service_url="http://user.test",
    )


def _patch(monkeypatch, mod, fake):
    monkeypatch.setattr(mod, "service_token", lambda s: "tok")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)


# ------------------------------------------------------------- session


async def test_session_update_status_success(monkeypatch, settings):
    fake = FakeAsyncClient(response=httpx.Response(200, json={"status": "generating_wiki"}))
    _patch(monkeypatch, session_mod, fake)

    out = await SessionServiceClient(settings).update_status("s1", "generating_wiki")
    assert out["status"] == "generating_wiki"
    url, body, headers = fake.calls[0]
    assert url == "http://session.test/internal/sessions/s1/status"
    assert body == {"status": "generating_wiki", "error": None}
    assert headers["Authorization"] == "Bearer tok"


async def test_session_409_raises_conflict(monkeypatch, settings):
    fake = FakeAsyncClient(response=httpx.Response(409, text="Invalid transition"))
    _patch(monkeypatch, session_mod, fake)
    with pytest.raises(ConflictTransition):
        await SessionServiceClient(settings).update_status("s1", "generating_wiki")


async def test_session_5xx_raises_error(monkeypatch, settings):
    fake = FakeAsyncClient(response=httpx.Response(503, text="boom"))
    _patch(monkeypatch, session_mod, fake)
    with pytest.raises(SessionServiceError, match="503"):
        await SessionServiceClient(settings).update_status("s1", "generating_wiki")


async def test_session_network_error(monkeypatch, settings):
    fake = FakeAsyncClient(error=httpx.ConnectError("refused"))
    _patch(monkeypatch, session_mod, fake)
    with pytest.raises(SessionServiceError, match="refused"):
        await SessionServiceClient(settings).update_status("s1", "generating_wiki")


# ------------------------------------------------------------- wiki


async def test_wiki_apply_changes(monkeypatch, settings):
    """The confirmed change set is posted to the internal apply endpoint."""
    fake = FakeAsyncClient(
        response=httpx.Response(200, json={"created": [], "updated": []}), method="post"
    )
    _patch(monkeypatch, wiki_mod, fake)

    payload = {
        "campaign_id": "c1",
        "session_id": "s1",
        "confirmed_by": "u1",
        "changes": [{"change_id": "c1", "action": "create", "kind": "character",
                     "title": "Aragorn"}],
        "relations": [],
    }
    out = await WikiServiceClient(settings).apply_changes(payload)
    assert out == {"created": [], "updated": []}
    url, body, headers = fake.calls[0]
    assert url == "http://wiki.test/internal/wiki/changes/apply"
    assert body == payload
    assert headers["Authorization"] == "Bearer tok"


async def test_wiki_apply_changes_error(monkeypatch, settings):
    fake = FakeAsyncClient(
        response=httpx.Response(422, text="invalid attributes"), method="post"
    )
    _patch(monkeypatch, wiki_mod, fake)
    with pytest.raises(WikiServiceError, match="422"):
        await WikiServiceClient(settings).apply_changes({"changes": []})


async def test_wiki_list_campaign_pages(monkeypatch, settings):
    fake = FakeAsyncClient(
        response=httpx.Response(200, json=[{"id": "p1", "title": "Aragorn"}]), method="get"
    )
    _patch(monkeypatch, wiki_mod, fake)
    pages = await WikiServiceClient(settings).list_campaign_pages("c1")
    assert pages[0]["title"] == "Aragorn"
    url, body, _ = fake.calls[0]
    assert url == "http://wiki.test/internal/wiki/pages?campaign_id=c1"
    assert body is None


# ------------------------------------------------------------- user


async def test_user_display_names(monkeypatch, settings):
    fake = FakeAsyncClient(
        response=httpx.Response(
            200,
            json={"users": {"u1": {"display_name": "Aragorn"}, "u2": {"display_name": "Gimli"}}},
        ),
        method="get",
    )
    _patch(monkeypatch, user_mod, fake)

    names = await UserServiceClient(settings).display_names(["u1", "u2"])
    assert names == {"u1": "Aragorn", "u2": "Gimli"}
    url, params, headers = fake.calls[0]
    assert url == "http://user.test/internal/users"
    assert params == [("ids", "u1"), ("ids", "u2")]
    assert headers["Authorization"] == "Bearer tok"


async def test_user_display_names_empty(monkeypatch, settings):
    fake = FakeAsyncClient(method="get")
    _patch(monkeypatch, user_mod, fake)
    assert await UserServiceClient(settings).display_names([]) == {}
    assert fake.calls == []  # no HTTP call for no ids


async def test_user_display_names_error(monkeypatch, settings):
    fake = FakeAsyncClient(response=httpx.Response(500, text="boom"), method="get")
    _patch(monkeypatch, user_mod, fake)
    with pytest.raises(UserServiceError, match="500"):
        await UserServiceClient(settings).display_names(["u1"])
