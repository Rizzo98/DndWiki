"""Tests for the OpenAI transcriber client (no real network)."""

import httpx
import pytest

from app.clients import openai_transcriber as mod
from app.clients.openai_transcriber import (
    OpenAIAPIError,
    OpenAITranscriber,
    TranscriptionError,
)
from app.core.config import ServiceSettings


def _settings(**overrides) -> ServiceSettings:
    defaults = {
        "openai_api_key": "sk-test",
        "openai_api_base": "https://api.openai.com/v1",
        "openai_max_retries": 2,
        "openai_transcription_language": "",
    }
    defaults.update(overrides)
    return ServiceSettings(**defaults)


class FakeHTTP:
    """Replaces httpx.AsyncClient: returns canned responses/raises."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, data=None, files=None):
        self.calls.append((url, headers, data, files))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _patch_http(monkeypatch, responses):
    fake = FakeHTTP(responses)
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: fake)
    return fake


def _wav_file(tmp_path, name="chunk_0000.wav") -> str:
    p = tmp_path / name
    p.write_bytes(b"fake-wav-bytes")
    return str(p)


def _diarized_body() -> dict:
    return {
        "segments": [
            {"start": 0.0, "end": 1.5, "speaker": "Speaker 1", "text": "ciao"},
            {"start": 1.8, "end": 3.0, "speaker": "Speaker 2", "text": "salve"},
        ]
    }


async def test_transcribe_success(monkeypatch, tmp_path):
    fake = _patch_http(monkeypatch, [httpx.Response(200, json=_diarized_body())])
    settings = _settings()

    out = await OpenAITranscriber(settings).transcribe(_wav_file(tmp_path))

    assert len(out.segments) == 2
    assert out.segments[0] == {"start": 0.0, "end": 1.5, "speaker": "Speaker 1", "text": "ciao"}
    assert out.language is None

    url, headers, data, files = fake.calls[0]
    assert url == "https://api.openai.com/v1/audio/transcriptions"
    assert headers["Authorization"] == "Bearer sk-test"
    assert data == {
        "model": "gpt-4o-transcribe-diarize",
        "response_format": "diarized_json",
        "chunking_strategy": "auto",
    }
    assert files["file"][0] == "chunk_0000.wav"
    assert files["file"][2] == "audio/wav"


async def test_transcribe_passes_language_hint(monkeypatch, tmp_path):
    fake = _patch_http(monkeypatch, [httpx.Response(200, json=_diarized_body())])
    settings = _settings()

    out = await OpenAITranscriber(settings).transcribe(_wav_file(tmp_path), language="it")

    _url, _headers, data, _files = fake.calls[0]
    assert data["language"] == "it"
    # the response carries no language and no config hint is set, so the
    # parsed result stays None (the worker keeps the hint it passed itself)
    assert out.language is None


async def test_transcribe_retries_on_429_then_succeeds(monkeypatch, tmp_path):
    fake = _patch_http(
        monkeypatch,
        [
            httpx.Response(429, headers={"retry-after": "0"}, text="rate limited"),
            httpx.Response(200, json=_diarized_body()),
        ],
    )

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    out = await OpenAITranscriber(_settings()).transcribe(_wav_file(tmp_path))
    assert len(fake.calls) == 2
    assert len(out.segments) == 2


async def test_transcribe_retries_on_5xx_then_fails(monkeypatch, tmp_path):
    fake = _patch_http(
        monkeypatch,
        [
            httpx.Response(500, text="boom"),
            httpx.Response(500, text="boom"),
        ],
    )

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    with pytest.raises(TranscriptionError, match="500"):
        await OpenAITranscriber(_settings()).transcribe(_wav_file(tmp_path))
    assert len(fake.calls) == 2  # both attempts used


async def test_transcribe_retries_on_network_error(monkeypatch, tmp_path):
    fake = _patch_http(
        monkeypatch,
        [httpx.ConnectError("connection reset"), httpx.Response(200, json=_diarized_body())],
    )

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    out = await OpenAITranscriber(_settings()).transcribe(_wav_file(tmp_path))
    assert len(fake.calls) == 2
    assert len(out.segments) == 2


async def test_transcribe_permanent_4xx_fails_fast(monkeypatch, tmp_path):
    """401 (bad key) is permanent: exactly one request, no retries."""
    fake = _patch_http(monkeypatch, [httpx.Response(401, text="invalid api key")])

    with pytest.raises(OpenAIAPIError, match="401"):
        await OpenAITranscriber(_settings()).transcribe(_wav_file(tmp_path))
    assert len(fake.calls) == 1


async def test_transcribe_missing_api_key(monkeypatch, tmp_path):
    fake = _patch_http(monkeypatch, [httpx.Response(200, json=_diarized_body())])
    with pytest.raises(TranscriptionError, match="OPENAI_API_KEY"):
        await OpenAITranscriber(_settings(openai_api_key="")).transcribe(_wav_file(tmp_path))
    assert fake.calls == []  # no request is made without a key
