"""Tests for the session-service internal API client."""

import httpx
import pytest

from app.clients import session_service as client_mod
from app.clients.session_service import (
    ConflictTransition,
    SessionServiceClient,
    SessionServiceError,
)
from app.core.config import ServiceSettings


class FakeAsyncClient:
    """Replaces httpx.AsyncClient: returns a canned response or raises."""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def patch(self, url, json=None, headers=None):
        self.calls.append((url, json, headers))
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture
def settings():
    return ServiceSettings(session_service_url="http://session.test")


def _patch_httpx(monkeypatch, fake):
    monkeypatch.setattr(client_mod, "service_token", lambda s: "tok")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)


async def test_update_status_success(monkeypatch, settings):
    fake = FakeAsyncClient(
        response=httpx.Response(200, json={"id": "s1", "status": "transcribing"})
    )
    _patch_httpx(monkeypatch, fake)

    out = await SessionServiceClient(settings).update_status("s1", "transcribing")
    assert out["status"] == "transcribing"
    url, body, headers = fake.calls[0]
    assert url == "http://session.test/internal/sessions/s1/status"
    assert body == {"status": "transcribing", "error": None}
    assert headers["Authorization"] == "Bearer tok"


async def test_update_artifacts_success(monkeypatch, settings):
    fake = FakeAsyncClient(response=httpx.Response(200, json={"id": "s1"}))
    _patch_httpx(monkeypatch, fake)

    await SessionServiceClient(settings).update_artifacts(
        "s1",
        transcript_uri="transcripts/s1/transcript.json",
        diarization_uri="transcripts/s1/diarization.json",
        duration_sec=3724.5,
    )
    url, body, _ = fake.calls[0]
    assert url == "http://session.test/internal/sessions/s1/artifacts"
    assert body == {
        "transcript_uri": "transcripts/s1/transcript.json",
        "diarization_uri": "transcripts/s1/diarization.json",
        "duration_sec": 3724.5,
    }


async def test_409_raises_conflict(monkeypatch, settings):
    fake = FakeAsyncClient(
        response=httpx.Response(409, text="Invalid transition: recorded -> transcribed")
    )
    _patch_httpx(monkeypatch, fake)

    with pytest.raises(ConflictTransition):
        await SessionServiceClient(settings).update_status("s1", "transcribing")


async def test_5xx_raises_service_error(monkeypatch, settings):
    fake = FakeAsyncClient(response=httpx.Response(503, text="boom"))
    _patch_httpx(monkeypatch, fake)

    with pytest.raises(SessionServiceError, match="503"):
        await SessionServiceClient(settings).update_status("s1", "transcribing")


async def test_network_error_raises_service_error(monkeypatch, settings):
    fake = FakeAsyncClient(error=httpx.ConnectError("connection refused"))
    _patch_httpx(monkeypatch, fake)

    with pytest.raises(SessionServiceError, match="connection refused"):
        await SessionServiceClient(settings).update_status("s1", "transcribing")


async def test_auth_failure_propagates(monkeypatch, settings):
    """Token acquisition failure surfaces to the caller (retry policy applies)."""

    def _boom(settings):
        raise RuntimeError("keycloak down")

    monkeypatch.setattr(client_mod, "service_token", _boom)
    with pytest.raises(RuntimeError, match="keycloak down"):
        await SessionServiceClient(settings).update_status("s1", "transcribing")
