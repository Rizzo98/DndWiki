"""Tests for the alignment prewarm module (no whisperx/torch installed).

prewarm_align imports whisperx lazily, so tests patch sys.modules and the
pipeline helpers exactly like test_pipeline.py does.
"""

import sys
from types import SimpleNamespace

from app import prewarm
from app.core.config import ServiceSettings


def _settings(**overrides):
    base = {"prewarm_languages": "it"}
    base.update(overrides)
    return ServiceSettings(**base)


def test_main_prewarms_configured_languages(monkeypatch):
    calls = []

    class FakeWhisperx:
        @staticmethod
        def load_align_model(language_code, device):
            calls.append((language_code, device))
            return SimpleNamespace(), {"language": language_code, "type": "torchaudio"}

    monkeypatch.setitem(sys.modules, "whisperx", FakeWhisperx())
    monkeypatch.setattr(prewarm, "get_device", lambda settings: "cpu")
    monkeypatch.setattr(prewarm, "get_settings", lambda: _settings(prewarm_languages="it,en"))

    prewarm.main()
    assert calls == [("it", "cpu"), ("en", "cpu")]


def test_main_skips_when_no_languages(monkeypatch):
    calls = []

    class FakeWhisperx:
        @staticmethod
        def load_align_model(language_code, device):
            calls.append(language_code)
            return SimpleNamespace(), {}

    monkeypatch.setitem(sys.modules, "whisperx", FakeWhisperx())
    monkeypatch.setattr(prewarm, "get_settings", lambda: _settings(prewarm_languages=""))

    prewarm.main()
    assert calls == []


def test_prewarm_align_continues_on_failure(monkeypatch):
    calls = []

    def flaky_load_align_model(language_code, device):
        calls.append(language_code)
        if language_code == "en":
            raise OSError("connection reset")
        return SimpleNamespace(), {"language": language_code}

    monkeypatch.setattr(prewarm, "_with_retry", lambda fn: fn())
    monkeypatch.setattr(prewarm, "get_device", lambda settings: "cpu")
    monkeypatch.setitem(
        sys.modules,
        "whisperx",
        SimpleNamespace(load_align_model=flaky_load_align_model),
    )

    # The failure is logged, not raised: startup must not die on a flaky CDN.
    prewarm.prewarm_align(["it", "en"], _settings())
    assert calls == ["it", "en"]
