"""Tests for the AssemblyAI transcriber client (no real network)."""

import httpx
import pytest

from app.clients import assemblyai_transcriber as mod
from app.clients.assemblyai_transcriber import (
    AssemblyAIAPIError,
    AssemblyAITranscriber,
    TranscriptionError,
)
from app.core.config import ServiceSettings


def _settings(**overrides) -> ServiceSettings:
    defaults = {
        "assemblyai_api_key": "aai-test",
        "assemblyai_api_base": "https://api.assemblyai.com",
        "assemblyai_max_retries": 2,
        "assemblyai_poll_interval_sec": 0.01,
        "assemblyai_poll_timeout_sec": 5.0,
    }
    defaults.update(overrides)
    return ServiceSettings(**defaults)


class FakeHTTP:
    """Replaces httpx.AsyncClient: canned responses per call (post/get).

    get() repeats its last response once the list is exhausted, so poll loops
    that outlive the canned responses keep getting a stable status (used by
    the poll-timeout test).
    """

    def __init__(self, posts=None, gets=None):
        self.posts = list(posts or [])
        self.gets = list(gets or [])
        self.post_calls = []
        self.get_calls = []
        self._last_get = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, params=None, content=None, json=None):
        self.post_calls.append((url, headers, params, content, json))
        item = self.posts.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def get(self, url, headers=None):
        self.get_calls.append((url, headers))
        if not self.gets:
            return self._last_get
        item = self.gets.pop(0)
        if isinstance(item, BaseException):
            self._last_get = item
            raise item
        self._last_get = item
        return item


def _patch_http(monkeypatch, posts=None, gets=None):
    fake = FakeHTTP(posts, gets)
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: fake)
    return fake


def _wav_file(tmp_path, name="chunk_0000.wav") -> str:
    p = tmp_path / name
    p.write_bytes(b"fake-wav-bytes")
    return str(p)


def _upload_response() -> dict:
    return {"upload_url": "https://cdn.assemblyai.com/upload/abc123"}


def _submit_response() -> dict:
    return {"id": "transcript-123", "status": "queued"}


def _completed_body() -> dict:
    """AssemblyAI transcript response with diarized utterances (ms timings)."""
    return {
        "id": "transcript-123",
        "status": "completed",
        "text": "Ciao. Salve.",
        "language_code": "it",
        "audio_duration": 3.0,
        "confidence": 0.95,
        "utterances": [
            {
                "speaker": "A",
                "text": "Ciao",
                "start": 0,
                "end": 1500,
                "confidence": 0.99,
                "words": [
                    {"text": "Ciao", "speaker": "A", "start": 0, "end": 1500, "confidence": 0.99},
                ],
            },
            {
                "speaker": "B",
                "text": "Salve",
                "start": 1800,
                "end": 3000,
                "confidence": 0.98,
                "words": [
                    {"text": "Salve", "speaker": "B", "start": 1800, "end": 3000, "confidence": 0.98},
                ],
            },
        ],
    }


def _queued_body() -> dict:
    return {"id": "transcript-123", "status": "processing", "text": None}


async def test_transcribe_success(monkeypatch, tmp_path):
    fake = _patch_http(
        monkeypatch,
        posts=[httpx.Response(200, json=_upload_response()), httpx.Response(200, json=_submit_response())],
        gets=[httpx.Response(200, json=_queued_body()), httpx.Response(200, json=_completed_body())],
    )
    settings = _settings()

    out = await AssemblyAITranscriber(settings).transcribe(
        _wav_file(tmp_path),
        language="it",
        speakers_expected=5,
        prompt="D&D session",
        keyterms=["Gandalf", "Frodo"],
    )

    assert len(out.segments) == 2
    assert out.segments[0] == {
        "start": 0.0,
        "end": 1.5,
        "speaker": "SPEAKER_00",
        "text": "Ciao",
        "words": [{"word": "Ciao", "start": 0.0, "end": 1.5}],
        "speaker_confidence": 0.99,
        "confidence": 0.99,
    }
    assert out.language == "it"

    # upload: POST /v2/upload with the raw WAV bytes
    upload_url, upload_headers, _p, upload_content, _j = fake.post_calls[0]
    assert upload_url == "https://api.assemblyai.com/v2/upload"
    assert upload_headers["authorization"] == "aai-test"
    assert upload_headers["Content-Type"] == "application/octet-stream"
    assert upload_content == b"fake-wav-bytes"

    # submit: POST /v2/transcript with the full parameter set
    submit_url, submit_headers, _p, _c, submit_json = fake.post_calls[1]
    assert submit_url == "https://api.assemblyai.com/v2/transcript"
    assert submit_headers["Content-Type"] == "application/json"
    assert submit_json == {
        "audio_url": "https://cdn.assemblyai.com/upload/abc123",
        "speech_models": ["universal-3-5-pro"],
        "speaker_labels": True,
        "language_code": "it",
        "speakers_expected": 5,
        "prompt": "D&D session",
        "keyterms_prompt": ["Gandalf", "Frodo"],
    }

    # poll: GET /v2/transcript/{id} until completed
    assert fake.get_calls[0][0] == "https://api.assemblyai.com/v2/transcript/transcript-123"
    assert fake.get_calls[1][0] == "https://api.assemblyai.com/v2/transcript/transcript-123"


async def test_transcribe_speech_models_fallback_list(monkeypatch, tmp_path):
    """A comma-separated model setting maps to a fallback speech_models array."""
    fake = _patch_http(
        monkeypatch,
        posts=[httpx.Response(200, json=_upload_response()), httpx.Response(200, json=_submit_response())],
        gets=[httpx.Response(200, json=_completed_body())],
    )
    settings = _settings(assemblyai_transcription_model="universal-3-5-pro, universal-2")

    await AssemblyAITranscriber(settings).transcribe(_wav_file(tmp_path))

    _u, _h, _p, _c, submit_json = fake.post_calls[1]
    assert submit_json["speech_models"] == ["universal-3-5-pro", "universal-2"]


async def test_transcribe_auto_detects_without_language(monkeypatch, tmp_path):
    fake = _patch_http(
        monkeypatch,
        posts=[httpx.Response(200, json=_upload_response()), httpx.Response(200, json=_submit_response())],
        gets=[httpx.Response(200, json=_completed_body())],
    )

    out = await AssemblyAITranscriber(_settings()).transcribe(
        _wav_file(tmp_path),
        language=None,
        speakers_expected=None,
        prompt=None,
        keyterms=None,
    )

    _u, _h, _p, _c, submit_json = fake.post_calls[1]
    assert submit_json["language_detection"] is True
    assert "language_code" not in submit_json
    assert "speakers_expected" not in submit_json
    assert "prompt" not in submit_json
    assert "keyterms_prompt" not in submit_json
    # parsed language falls back to the detected one from the response
    assert out.language == "it"


async def test_transcribe_retries_upload_429_then_succeeds(monkeypatch, tmp_path):
    fake = _patch_http(
        monkeypatch,
        posts=[
            httpx.Response(429, headers={"retry-after": "0"}, text="rate limited"),
            httpx.Response(200, json=_upload_response()),
            httpx.Response(200, json=_submit_response()),
        ],
        gets=[httpx.Response(200, json=_completed_body())],
    )

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    out = await AssemblyAITranscriber(_settings()).transcribe(_wav_file(tmp_path))
    # 429 attempt + successful upload + submit
    assert len(fake.post_calls) == 3
    assert len(out.segments) == 2


async def test_transcribe_retries_poll_5xx_then_succeeds(monkeypatch, tmp_path):
    fake = _patch_http(
        monkeypatch,
        posts=[httpx.Response(200, json=_upload_response()), httpx.Response(200, json=_submit_response())],
        gets=[
            httpx.Response(500, text="boom"),
            httpx.Response(200, json=_completed_body()),
        ],
    )

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    out = await AssemblyAITranscriber(_settings()).transcribe(_wav_file(tmp_path))
    assert len(fake.get_calls) == 2
    assert len(out.segments) == 2


async def test_transcribe_permanent_4xx_fails_fast(monkeypatch, tmp_path):
    """401 (bad key) on submit is permanent: exactly one submit, no retries."""
    fake = _patch_http(
        monkeypatch,
        posts=[
            httpx.Response(200, json=_upload_response()),
            httpx.Response(401, text="invalid api key"),
        ],
        gets=[],
    )

    with pytest.raises(AssemblyAIAPIError, match="401"):
        await AssemblyAITranscriber(_settings()).transcribe(_wav_file(tmp_path))
    assert len(fake.post_calls) == 2
    assert fake.get_calls == []


async def test_transcribe_transcript_error_status(monkeypatch, tmp_path):
    """A transcript that reaches status "error" raises TranscriptionError."""
    _patch_http(
        monkeypatch,
        posts=[httpx.Response(200, json=_upload_response()), httpx.Response(200, json=_submit_response())],
        gets=[httpx.Response(200, json={"id": "transcript-123", "status": "error", "error": "audio too short"})],
    )

    with pytest.raises(TranscriptionError, match="audio too short"):
        await AssemblyAITranscriber(_settings()).transcribe(_wav_file(tmp_path))


async def test_transcribe_poll_timeout(monkeypatch, tmp_path):
    """A job stuck in processing for longer than the poll timeout raises."""
    fake = _patch_http(
        monkeypatch,
        posts=[httpx.Response(200, json=_upload_response()), httpx.Response(200, json=_submit_response())],
        gets=[httpx.Response(200, json=_queued_body())],  # repeated forever
    )

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    settings = _settings(assemblyai_poll_timeout_sec=0.05)
    with pytest.raises(TranscriptionError, match="did not complete"):
        await AssemblyAITranscriber(settings).transcribe(_wav_file(tmp_path))
    # the loop kept polling until the deadline
    assert len(fake.get_calls) > 2


async def test_transcribe_missing_api_key(monkeypatch, tmp_path):
    fake = _patch_http(monkeypatch, posts=[], gets=[])
    with pytest.raises(TranscriptionError, match="ASSEMBLYAI_API_KEY"):
        await AssemblyAITranscriber(_settings(assemblyai_api_key="")).transcribe(_wav_file(tmp_path))
    assert fake.post_calls == []
    assert fake.get_calls == []
