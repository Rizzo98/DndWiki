"""Tests for the Deepgram transcriber client (no real network)."""

import httpx
import pytest

from app.clients import deepgram_transcriber as mod
from app.clients.deepgram_transcriber import (
    DeepgramAPIError,
    DeepgramTranscriber,
    TranscriptionError,
)
from app.core.config import ServiceSettings


def _settings(**overrides) -> ServiceSettings:
    defaults = {
        "deepgram_api_key": "dg-test",
        "deepgram_api_base": "https://api.deepgram.com/v1",
        "deepgram_max_retries": 2,
        "deepgram_language": "",
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

    async def post(self, url, headers=None, params=None, content=None):
        self.calls.append((url, headers, params, content))
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


def _utterances_body() -> dict:
    """Deepgram listen response with utterances=true (speaker turns)."""
    return {
        "metadata": {"duration": 3.0},
        "results": {
            "utterances": [
                {
                    "start": 0.0,
                    "end": 1.5,
                    "confidence": 0.99,
                    "channel": 0,
                    "transcript": "ciao",
                    "speaker": 0,
                    "words": [
                        {"word": "ciao", "start": 0.0, "end": 1.5, "speaker": 0, "speaker_confidence": 0.99}
                    ],
                },
                {
                    "start": 1.8,
                    "end": 3.0,
                    "confidence": 0.98,
                    "channel": 0,
                    "transcript": "salve",
                    "speaker": 1,
                    "words": [
                        {"word": "salve", "start": 1.8, "end": 3.0, "speaker": 1, "speaker_confidence": 0.93}
                    ],
                },
            ]
        },
    }


async def test_transcribe_success(monkeypatch, tmp_path):
    fake = _patch_http(monkeypatch, [httpx.Response(200, json=_utterances_body())])
    settings = _settings()

    out = await DeepgramTranscriber(settings).transcribe(_wav_file(tmp_path))

    assert len(out.segments) == 2
    assert out.segments[0] == {
        "start": 0.0,
        "end": 1.5,
        "speaker": "SPEAKER_00",
        "text": "ciao",
        "words": [{"word": "ciao", "start": 0.0, "end": 1.5}],
        # mean of the word-level speaker_confidence (single word -> 0.99)
        "speaker_confidence": 0.99,
    }
    assert out.language is None  # no language field on utterances here

    url, headers, params, content = fake.calls[0]
    assert url == "https://api.deepgram.com/v1/listen"
    assert headers["Authorization"] == "Token dg-test"
    assert headers["Content-Type"] == "audio/wav"
    assert params == {
        "model": "nova-3",
        "diarize": "true",
        "utterances": "true",
        "smart_format": "true",
        "punctuate": "true",
        "language": "multi",  # empty hint -> Nova auto-detects
        "detect_language": "true",
    }
    assert content == b"fake-wav-bytes"


async def test_transcribe_passes_language_hint(monkeypatch, tmp_path):
    fake = _patch_http(monkeypatch, [httpx.Response(200, json=_utterances_body())])
    settings = _settings()

    await DeepgramTranscriber(settings).transcribe(_wav_file(tmp_path), language="it")

    _url, _headers, params, _content = fake.calls[0]
    assert params["language"] == "it"


async def test_transcribe_detects_language_from_utterances(monkeypatch, tmp_path):
    body = _utterances_body()
    body["results"]["utterances"][0]["language"] = "it"
    _patch_http(monkeypatch, [httpx.Response(200, json=body)])

    out = await DeepgramTranscriber(_settings()).transcribe(_wav_file(tmp_path))

    assert out.language == "it"


async def test_transcribe_falls_back_to_paragraphs(monkeypatch, tmp_path):
    """No utterances in the response -> paragraphs are used as segments."""
    body = {
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "paragraphs": {
                                "paragraphs": [
                                    {
                                        "start": 0.0,
                                        "end": 1.5,
                                        "speaker": 0,
                                        "sentences": [{"text": "ciao", "start": 0.0, "end": 1.5}],
                                    },
                                    {
                                        "start": 1.8,
                                        "end": 3.0,
                                        "speaker": 1,
                                        "sentences": [{"text": "salve", "start": 1.8, "end": 3.0}],
                                    },
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    }
    _patch_http(monkeypatch, [httpx.Response(200, json=body)])

    out = await DeepgramTranscriber(_settings()).transcribe(_wav_file(tmp_path))

    assert len(out.segments) == 2
    assert out.segments[0]["speaker"] == "SPEAKER_00"
    assert out.segments[0]["text"] == "ciao"
    assert out.segments[0]["words"] == []
    # paragraphs carry no confidence (no per-word data in that fallback path)
    assert out.segments[0]["speaker_confidence"] is None


async def test_transcribe_falls_back_to_word_runs(monkeypatch, tmp_path):
    """No utterances/paragraphs -> consecutive same-speaker words are grouped."""
    body = {
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "words": [
                                {"word": "ciao", "start": 0.0, "end": 0.4, "speaker": 0, "speaker_confidence": 0.9},
                                {"word": "mondo", "start": 0.5, "end": 1.0, "speaker": 0, "speaker_confidence": 0.8},
                                {"word": "salve", "start": 1.8, "end": 2.6, "speaker": 1, "speaker_confidence": 0.7},
                            ]
                        }
                    ]
                }
            ]
        }
    }
    _patch_http(monkeypatch, [httpx.Response(200, json=body)])

    out = await DeepgramTranscriber(_settings()).transcribe(_wav_file(tmp_path))

    assert [(s["start"], s["end"], s["text"], s["speaker"]) for s in out.segments] == [
        (0.0, 1.0, "ciao mondo", "SPEAKER_00"),
        (1.8, 2.6, "salve", "SPEAKER_01"),
    ]
    # word-run fallback: run confidence = mean of the run's word confidences
    assert [s["speaker_confidence"] for s in out.segments] == [0.85, 0.7]


async def test_transcribe_retries_on_429_then_succeeds(monkeypatch, tmp_path):
    fake = _patch_http(
        monkeypatch,
        [
            httpx.Response(429, headers={"retry-after": "0"}, text="rate limited"),
            httpx.Response(200, json=_utterances_body()),
        ],
    )

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    out = await DeepgramTranscriber(_settings()).transcribe(_wav_file(tmp_path))
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
        await DeepgramTranscriber(_settings()).transcribe(_wav_file(tmp_path))
    assert len(fake.calls) == 2  # both attempts used


async def test_transcribe_retries_on_network_error(monkeypatch, tmp_path):
    fake = _patch_http(
        monkeypatch,
        [httpx.ConnectError("connection reset"), httpx.Response(200, json=_utterances_body())],
    )

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    out = await DeepgramTranscriber(_settings()).transcribe(_wav_file(tmp_path))
    assert len(fake.calls) == 2
    assert len(out.segments) == 2


async def test_transcribe_permanent_4xx_fails_fast(monkeypatch, tmp_path):
    """401 (bad key) is permanent: exactly one request, no retries."""
    fake = _patch_http(monkeypatch, [httpx.Response(401, text="invalid api key")])

    with pytest.raises(DeepgramAPIError, match="401"):
        await DeepgramTranscriber(_settings()).transcribe(_wav_file(tmp_path))
    assert len(fake.calls) == 1


async def test_transcribe_missing_api_key(monkeypatch, tmp_path):
    fake = _patch_http(monkeypatch, [httpx.Response(200, json=_utterances_body())])
    with pytest.raises(TranscriptionError, match="DEEPGRAM_API_KEY"):
        await DeepgramTranscriber(_settings(deepgram_api_key="")).transcribe(_wav_file(tmp_path))
    assert fake.calls == []  # no request is made without a key
