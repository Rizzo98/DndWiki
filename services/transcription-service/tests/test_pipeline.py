"""Tests for the WhisperX pipeline: artifact builders + transcribe() wiring.

whisperx/torch are not installed locally, so every test that touches them
patches sys.modules / the loader functions (lazy imports make this possible).
"""

import json
import sys
from types import SimpleNamespace

import pytest

from app import pipeline
from app.core.config import ServiceSettings


def _result_with_segments() -> dict:
    return {
        "language": "en",
        "language_score": 0.98,
        "segments": [
            {
                "start": 0.0,
                "end": 4.2,
                "text": "Welcome back, heroes.",
                "speaker": "SPEAKER_00",
                "words": [
                    {
                        "word": "Welcome",
                        "start": 0.0,
                        "end": 0.5,
                        "score": 0.99,
                        "speaker": "SPEAKER_00",
                    },
                    {
                        "word": "heroes.",
                        "start": 3.9,
                        "end": 4.2,
                        "score": 0.95,
                        "speaker": "SPEAKER_00",
                    },
                ],
            },
            {
                "start": 4.5,
                "end": 8.0,
                "text": "And they were never seen again.",
                "speaker": None,  # silence/overlap after diarization
                "words": [],
            },
        ],
    }


def test_build_transcript_shape():
    """Matches docs/data-model.md transcript_uri JSON shape."""
    out = pipeline.build_transcript("sess-1", _result_with_segments(), "large-v3")
    assert out["session_id"] == "sess-1"
    assert out["language"] == "en"
    assert out["model"] == "large-v3"
    assert len(out["segments"]) == 2

    seg = out["segments"][0]
    assert seg["start"] == 0.0
    assert seg["end"] == 4.2
    assert seg["speaker"] == "SPEAKER_00"
    assert seg["text"] == "Welcome back, heroes."
    assert seg["words"] == [
        {"word": "Welcome", "start": 0.0, "end": 0.5},
        {"word": "heroes.", "start": 3.9, "end": 4.2},
    ]
    # second segment has no speaker -> null, no words
    assert out["segments"][1]["speaker"] is None
    assert out["segments"][1]["words"] == []


def test_build_diarization_is_compact():
    """Event-contract segments: start/end/speaker/text, no words."""
    out = pipeline.build_diarization("sess-1", _result_with_segments(), "large-v3")
    assert out["segments"] == [
        {
            "start": 0.0,
            "end": 4.2,
            "speaker": "SPEAKER_00",
            "text": "Welcome back, heroes.",
        },
        {
            "start": 4.5,
            "end": 8.0,
            "speaker": None,
            "text": "And they were never seen again.",
        },
    ]


def test_builders_round_numpy_scalars():
    """numpy float32/float64-like timings become plain JSON-friendly floats.

    numpy itself is a whisperx dependency (not installed in the dev venv), so
    the scalar types are faked with float subclasses — float() conversion is
    exactly what _fmt relies on.
    """

    class FakeFloat32(float):
        pass

    class FakeFloat64(float):
        pass

    result = {
        "language": "en",
        "segments": [
            {
                "start": FakeFloat32(1.23456),
                "end": FakeFloat64(2.34567),
                "text": "hi",
                "speaker": "SPEAKER_00",
                "words": [
                    {"word": "hi", "start": FakeFloat32(1.2), "end": FakeFloat32(1.8)}
                ],
            }
        ],
    }
    out = pipeline.build_transcript("sess", result, "small")
    assert out["segments"][0]["start"] == 1.235
    assert out["segments"][0]["end"] == 2.346
    assert out["segments"][0]["words"][0]["start"] == 1.2
    json.dumps(out)  # must not raise


def test_audio_duration():
    result = _result_with_segments()
    assert pipeline.audio_duration(result) == 8.0
    assert pipeline.audio_duration({"segments": []}) is None


def test_transcribe_wiring(monkeypatch):
    """transcribe() drives load_audio -> ASR -> align -> diarize -> assign."""
    calls: list[str] = []

    def fake_load_audio(path):
        calls.append("load_audio")
        return "<audio>"

    def fake_load_align_model(language_code, device):
        calls.append("load_align_model")
        return ("model_a", "metadata")

    def fake_align(segments, model_a, metadata, audio, device):
        calls.append("align")
        return {"segments": [{"start": 0.0, "end": 1.0, "text": "aligned"}]}

    def fake_assign(diarize_segments, result):
        calls.append("assign_word_speakers")
        return result

    fake_asr = SimpleNamespace(
        transcribe=lambda audio, batch_size, language=None: {
            "language": "en",
            "segments": [{"start": 0.0, "end": 1.0, "text": "raw"}],
        }
    )
    fake_diarizer = lambda audio: "<diarize>"

    fake_whisperx = SimpleNamespace(
        load_audio=fake_load_audio,
        load_align_model=fake_load_align_model,
        align=fake_align,
        assign_word_speakers=fake_assign,
    )
    monkeypatch.setitem(sys.modules, "whisperx", fake_whisperx)
    monkeypatch.setattr(pipeline, "get_device", lambda settings: "cpu")
    monkeypatch.setattr(pipeline, "get_transcriber", lambda settings: fake_asr)
    monkeypatch.setattr(pipeline, "get_diarizer", lambda settings: fake_diarizer)

    settings = ServiceSettings(work_dir="/tmp/x")
    result = pipeline.transcribe("/tmp/audio.m4a", settings)

    assert calls == [
        "load_audio",
        "load_align_model",
        "align",
        "assign_word_speakers",
    ]
    assert result["segments"][0]["text"] == "aligned"


def test_transcribe_skips_alignment_without_language(monkeypatch):
    """No language detected -> align is skipped, diarization still runs."""
    calls: list[str] = []

    fake_whisperx = SimpleNamespace(
        load_audio=lambda path: "<audio>",
        load_align_model=lambda language_code, device: None,
        align=lambda *a, **k: calls.append("align"),
        assign_word_speakers=lambda d, r: r,
    )
    fake_asr = SimpleNamespace(
        transcribe=lambda audio, batch_size, language=None: {
            "language": None,
            "segments": [{"start": 0.0, "end": 1.0, "text": "x"}],
        }
    )
    fake_diarizer = lambda audio: None

    monkeypatch.setitem(sys.modules, "whisperx", fake_whisperx)
    monkeypatch.setattr(pipeline, "get_device", lambda settings: "cpu")
    monkeypatch.setattr(pipeline, "get_transcriber", lambda settings: fake_asr)
    monkeypatch.setattr(pipeline, "get_diarizer", lambda settings: fake_diarizer)

    result = pipeline.transcribe("/tmp/audio.m4a", ServiceSettings(work_dir="/tmp/x"))
    assert calls == []  # align must not have been called
    assert result["segments"][0]["text"] == "x"


def test_loaders_are_singletons(monkeypatch):
    """Model loading happens exactly once per (kind, model, device)."""
    loaded: list[str] = []

    fake_whisperx = SimpleNamespace(
        load_model=lambda *a, **k: "asr-model",
    )
    monkeypatch.setitem(sys.modules, "whisperx", fake_whisperx)
    # whisperx >= 3.8 imports DiarizationPipeline from whisperx.diarize
    monkeypatch.setitem(
        sys.modules,
        "whisperx.diarize",
        SimpleNamespace(DiarizationPipeline=lambda **k: "diarize-model"),
    )
    monkeypatch.setattr(pipeline, "get_device", lambda settings: "cpu")

    orig_load_model = fake_whisperx.load_model

    def wrapped_load(*a, **k):
        loaded.append("load_model")
        return orig_load_model(*a, **k)

    fake_whisperx.load_model = wrapped_load

    pipeline._models.clear()
    settings = ServiceSettings(work_dir="/tmp/x")
    try:
        assert pipeline.get_transcriber(settings) == "asr-model"
        assert pipeline.get_transcriber(settings) == "asr-model"  # cached
        assert pipeline.get_diarizer(settings) == "diarize-model"
        assert pipeline.get_diarizer(settings) == "diarize-model"  # cached
        assert loaded == ["load_model"]
    finally:
        pipeline._models.clear()


def test_split_audio_fixed_chunks():
    audio = [0.0] * (pipeline.SAMPLE_RATE * 12)  # 12 s at 16 kHz
    chunks = pipeline.split_audio(audio, 5)
    assert [(round(s, 3), round(e, 3), len(c)) for s, e, c in chunks] == [
        (0.0, 5.0, pipeline.SAMPLE_RATE * 5),
        (5.0, 10.0, pipeline.SAMPLE_RATE * 5),
        (10.0, 12.0, pipeline.SAMPLE_RATE * 2),
    ]


def test_split_audio_exact_multiple_no_trailing_empty():
    audio = [0.0] * (pipeline.SAMPLE_RATE * 10)
    chunks = pipeline.split_audio(audio, 5)
    assert [(s, e) for s, e, _ in chunks] == [(0.0, 5.0), (5.0, 10.0)]


def test_split_audio_empty_returns_no_chunks():
    assert pipeline.split_audio([], 5) == []


def test_offset_segments_shifts_timestamps():
    class FakeFloat32(float):
        pass

    result = {
        "segments": [
            {
                "start": FakeFloat32(1.0),
                "end": FakeFloat32(2.0),
                "text": "hi",
                "speaker": "SPEAKER_00",
                "words": [{"word": "hi", "start": FakeFloat32(1.0), "end": FakeFloat32(2.0)}],
            }
        ]
    }
    segments = pipeline.offset_segments(result, 300)
    assert segments is result["segments"]
    assert segments[0]["start"] == 301.0
    assert segments[0]["end"] == 302.0
    assert segments[0]["words"][0]["start"] == 301.0
    assert type(segments[0]["start"]) is float
    import json

    json.dumps(segments)  # must not raise (plain floats)


def test_get_aligner_cached_per_language(monkeypatch):
    calls: list[str] = []

    fake_whisperx = SimpleNamespace(
        load_align_model=lambda language_code, device: calls.append(language_code)
        or ("model_a", "metadata"),
    )
    monkeypatch.setitem(sys.modules, "whisperx", fake_whisperx)
    monkeypatch.setattr(pipeline, "get_device", lambda settings: "cpu")
    pipeline._models.clear()
    try:
        settings = ServiceSettings(work_dir="/tmp/x")
        assert pipeline.get_aligner("it", settings) == ("model_a", "metadata")
        assert pipeline.get_aligner("it", settings) == ("model_a", "metadata")  # cached
        assert pipeline.get_aligner("en", settings) == ("model_a", "metadata")
        assert calls == ["it", "en"]
    finally:
        pipeline._models.clear()


def test_transcribe_audio_passes_language_to_asr(monkeypatch):
    calls: dict[str, object] = {}

    fake_whisperx = SimpleNamespace(
        load_align_model=lambda language_code, device: ("model_a", "metadata"),
        align=lambda segments, model_a, metadata, audio, device: {"segments": segments},
        assign_word_speakers=lambda d, r: r,
    )
    monkeypatch.setitem(sys.modules, "whisperx", fake_whisperx)
    monkeypatch.setattr(pipeline, "get_device", lambda settings: "cpu")

    def fake_asr_transcribe(audio, batch_size, language=None):
        calls["language"] = language
        return {"language": "it", "segments": [{"start": 0.0, "end": 1.0, "text": "x"}]}

    fake_asr = SimpleNamespace(transcribe=fake_asr_transcribe)
    monkeypatch.setattr(pipeline, "get_transcriber", lambda settings: fake_asr)
    monkeypatch.setattr(pipeline, "get_diarizer", lambda settings: lambda audio: None)

    pipeline._models.clear()
    try:
        result = pipeline.transcribe_audio("<audio>", ServiceSettings(work_dir="/tmp/x"), language="it")
        assert calls["language"] == "it"
        assert result["language"] == "it"
    finally:
        pipeline._models.clear()


# ---------------------------------------------------------------------------
# CPU-only support
# ---------------------------------------------------------------------------


def test_effective_compute_type_cpu_fallback():
    """float16 needs CUDA; on CPU the worker falls back to int8 (CTranslate2),
    which is memory-safe for large models — float32 on CPU OOMs."""
    settings = ServiceSettings(whisper_compute_type="float16")
    assert pipeline._effective_compute_type(settings, "cpu") == "int8"
    assert pipeline._effective_compute_type(settings, "cuda") == "float16"
    # explicit float32 on CPU is honored (user choice, more memory)
    settings32 = ServiceSettings(whisper_compute_type="float32")
    assert pipeline._effective_compute_type(settings32, "cpu") == "float32"
    # explicit int8 on GPU is honored too
    settings_i8 = ServiceSettings(whisper_compute_type="int8")
    assert pipeline._effective_compute_type(settings_i8, "cuda") == "int8"


def test_effective_compute_type_cpu_override():
    """WHISPER_CPU_COMPUTE_TYPE overrides the CPU fallback."""
    settings = ServiceSettings(
        whisper_compute_type="float16", whisper_cpu_compute_type="int8_float16"
    )
    assert pipeline._effective_compute_type(settings, "cpu") == "int8_float16"


def test_get_device_forced_cpu_skips_torch(monkeypatch):
    """WHISPER_DEVICE=cpu must not require torch to be importable."""
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    settings = ServiceSettings(whisper_device="cpu")
    assert pipeline.get_device(settings) == "cpu"
    settings_cuda = ServiceSettings(whisper_device="cuda")
    assert pipeline.get_device(settings_cuda) == "cuda"


def test_get_device_auto_detects_cuda(monkeypatch):
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: True)
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    assert pipeline.get_device(ServiceSettings(whisper_device="")) == "cuda"


def test_get_device_auto_detects_cpu(monkeypatch):
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: False)
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    assert pipeline.get_device(ServiceSettings(whisper_device="")) == "cpu"


def test_with_retry_succeeds_on_transient_error(monkeypatch):
    """Network-ish failures (OSError subclasses) are retried; the call
    eventually succeeds."""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionResetError("flaky network")
        return "ok"

    # make the sleep instant
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    assert pipeline._with_retry(flaky, attempts=3) == "ok"
    assert calls["n"] == 3


def test_with_retry_gives_up_after_attempts(monkeypatch):
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)

    def always_fails():
        raise ConnectionError("still flaky")

    with pytest.raises(ConnectionError):
        pipeline._with_retry(always_fails, attempts=2)


def test_with_retry_does_not_retry_non_network_errors():
    def buggy():
        raise ValueError("real bug")

    with pytest.raises(ValueError, match="real bug"):
        pipeline._with_retry(buggy, attempts=3)
