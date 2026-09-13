"""Regression tests for the real ContentServiceClient (no fakes).

The delete endpoint fakes the client in the API tests, which hid a missing
setting: the real client reads settings.service_timeout_sec, so constructing it
against ServiceSettings and issuing a request must work. These tests cover that
plus the URL/headers and the 409 -> SessionNotDeletable / network -> error
mapping session-service relies on.
"""

import uuid

import httpx
import pytest

from app.clients.content import ContentServiceClient, ContentServiceError, SessionNotDeletable
from app.core.config import ServiceSettings


class _FakeResponse:
    def __init__(self, status_code: int = 200, body: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._body = body or {}
        self.text = text

    def json(self) -> dict:
        return self._body


class _FakeAsyncClient:
    """Async-context-manager stand-in for httpx.AsyncClient."""

    def __init__(self, response=None, exc: Exception | None = None) -> None:
        self._response = response
        self._exc = exc
        self.requests: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def delete(self, url, headers=None):
        if self._exc is not None:
            raise self._exc
        self.requests.append((url, headers or {}))
        return self._response


def _make_client(monkeypatch, response=None, exc=None):
    fake = _FakeAsyncClient(response=response, exc=exc)
    monkeypatch.setattr("app.clients.content.httpx.AsyncClient", lambda **kw: fake)
    monkeypatch.setattr("app.clients.content.service_token", lambda settings: "tok-123")
    # A real ServiceSettings: a setting the client needs but the model does not
    # define raises AttributeError right here.
    client = ContentServiceClient("http://content.test", ServiceSettings())
    return client, fake


async def test_delete_session_data_calls_the_internal_endpoint(monkeypatch):
    session = uuid.uuid4()
    client, fake = _make_client(
        monkeypatch,
        response=_FakeResponse(200, {"session_id": str(session), "deleted": True, "jobs": 2}),
    )

    body = await client.delete_session_data(str(session))

    assert body["deleted"] is True
    url, headers = fake.requests[0]
    assert url == f"http://content.test/internal/content/sessions/{session}"
    assert headers["Authorization"] == "Bearer tok-123"


async def test_delete_session_data_maps_409_to_not_deletable(monkeypatch):
    client, _ = _make_client(
        monkeypatch,
        response=_FakeResponse(409, {"detail": "this session already wrote 2 page(s)"}),
    )

    with pytest.raises(SessionNotDeletable) as exc:
        await client.delete_session_data(str(uuid.uuid4()))
    assert "already wrote" in str(exc.value)


async def test_delete_session_data_maps_transport_errors(monkeypatch):
    client, _ = _make_client(monkeypatch, exc=httpx.ConnectError("nope"))

    with pytest.raises(ContentServiceError):
        await client.delete_session_data(str(uuid.uuid4()))


async def test_delete_session_data_maps_5xx(monkeypatch):
    client, _ = _make_client(monkeypatch, response=_FakeResponse(500, text="boom"))

    with pytest.raises(ContentServiceError):
        await client.delete_session_data(str(uuid.uuid4()))
