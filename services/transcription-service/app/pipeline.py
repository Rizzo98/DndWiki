"""WhisperX transcription pipeline: VAD -> ASR -> word alignment -> diarization.

Targets whisperx >= 3.8, whose ASR backend is faster-whisper (CTranslate2)
and whose diarization pipeline lives in whisperx.diarize. Heavy imports
(torch, whisperx) happen lazily inside the functions so the module can be
imported and unit-tested without a GPU or a whisperx install, and models are
loaded exactly once per process (module-level singletons, kept warm between
jobs).

The worker decodes the recording and runs transcribe_audio() per fixed-length
chunk in worker threads (asyncio.to_thread) so the asyncio event loop stays
responsive (RabbitMQ heartbeats, health API) while the CPU/GPU work blocks;
models are loaded once per process and reused across chunks and jobs.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)

# (kind, model_name, device) -> loaded object; kept warm between jobs
_models: dict[tuple[str, str, str], Any] = {}
_lock = threading.Lock()

# whisperx decodes every audio file to 16 kHz mono float32
SAMPLE_RATE = 16_000


def _is_retryable(exc: BaseException) -> bool:
    """Network/IO errors worth retrying; anything else is a real bug."""
    if isinstance(exc, OSError):  # socket errors, connection resets, ...
        return True
    try:
        import requests
        from huggingface_hub.errors import HFHubError
    except ImportError:  # optional deps missing in the dev/test env
        return False
    return isinstance(exc, (requests.RequestException, HFHubError))


def _with_retry(fn: Callable[[], Any], attempts: int = 3, base_delay: float = 2.0) -> Any:
    """Run fn, retrying transient network/IO failures with backoff.

    Model downloads can fail on flaky connections (HF serves large files via
    a CDN); the local cache makes retries cheap and idempotent. Non-retryable
    exceptions propagate immediately.
    """
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last_error = exc
            if not _is_retryable(exc):
                raise
            if attempt < attempts - 1:
                logger.warning(
                    "Transient error loading model (attempt %d/%d): %s",
                    attempt + 1,
                    attempts,
                    exc,
                )
                time.sleep(base_delay * (2**attempt))
    assert last_error is not None
    raise last_error


def get_device(settings: ServiceSettings) -> str:
    """Resolve the device to run whisperx on.

    Honors an explicit WHISPER_DEVICE (cuda/cpu); otherwise auto-detects:
    CUDA when available, else CPU.
    """
    if settings.whisper_device:
        return settings.whisper_device
    import torch

    return "cuda" if torch.cuda.is_available() else "cpu"


def _effective_compute_type(settings: ServiceSettings, device: str) -> str:
    """Pick a compute type that actually runs on the detected device.

    The ASR backend is faster-whisper/CTranslate2: float16 is GPU-only, and
    float32 on CPU can OOM with large models (large-v3 float32 is ~6 GB of
    RAM). On CPU we default to int8, which is memory-safe and near-lossless.
    """
    if device == "cpu" and settings.whisper_compute_type == "float16":
        return settings.whisper_cpu_compute_type
    return settings.whisper_compute_type


def get_transcriber(settings: ServiceSettings) -> Any:
    """Lazily load and cache the faster-whisper ASR model (large-v3 default)."""
    device = get_device(settings)
    key = ("asr", settings.whisper_model, device)
    with _lock:
        if key not in _models:
            import whisperx

            logger.info(
                "Loading WhisperX ASR model %s on %s (compute=%s)",
                settings.whisper_model,
                device,
                _effective_compute_type(settings, device),
            )
            _models[key] = _with_retry(
                lambda: whisperx.load_model(
                    settings.whisper_model,
                    device=device,
                    compute_type=_effective_compute_type(settings, device),
                    use_auth_token=settings.hf_token or None,
                )
            )
    return _models[key]


def get_diarizer(settings: ServiceSettings) -> Any:
    """Lazily load and cache the pyannote diarization pipeline (gated model)."""
    device = get_device(settings)
    key = ("diarize", settings.diarization_model, device)
    with _lock:
        if key not in _models:
            from whisperx.diarize import DiarizationPipeline

            logger.info(
                "Loading diarization model %s on %s",
                settings.diarization_model,
                device,
            )
            _models[key] = _with_retry(
                lambda: DiarizationPipeline(
                    model_name=settings.diarization_model,
                    token=settings.hf_token or None,
                    device=device,
                )
            )
    return _models[key]


def get_aligner(language: str, settings: ServiceSettings) -> Any:
    """Lazily load and cache the per-language wav2vec2 alignment model.

    Cached per (language, device): whisperx.load_align_model re-downloads and
    re-instantiates on every call, which chunked transcription must not do per
    chunk. Returns the (model, metadata) tuple whisperx.align expects.
    """
    device = get_device(settings)
    key = ("align", language, device)
    with _lock:
        if key not in _models:
            import whisperx

            logger.info("Loading alignment model for %s on %s", language, device)
            _models[key] = _with_retry(
                lambda: whisperx.load_align_model(language_code=language, device=device)
            )
    return _models[key]


def load_audio(audio_path: str) -> Any:
    """Decode an audio file to a 16 kHz mono float32 array (whisperx format)."""
    import whisperx

    return whisperx.load_audio(audio_path)


def split_audio(audio: Any, chunk_seconds: float) -> list[tuple[float, float, Any]]:
    """Slice a loaded audio array into fixed-length pieces.

    Returns (start_sec, end_sec, chunk_array) tuples so per-chunk results
    can be spliced back into the session-wide timeline; timestamps are
    relative to the start of the whole file. The final chunk may be shorter.
    """
    total = len(audio)  # numpy arrays report the sample count on axis 0
    if total <= 0:
        return []
    chunk = int(float(chunk_seconds) * SAMPLE_RATE)
    chunks: list[tuple[float, float, Any]] = []
    start = 0
    while start < total:
        end = min(start + chunk, total)
        chunks.append((start / SAMPLE_RATE, end / SAMPLE_RATE, audio[start:end]))
        start = end
    return chunks


def transcribe_audio(
    audio: Any, settings: ServiceSettings, language: str | None = None
) -> dict[str, Any]:
    """Run VAD -> ASR -> word alignment -> diarization on one audio array.

    Works on a full file or a single chunk. ``language`` may be passed in to
    skip re-detection on subsequent chunks of the same recording; when None
    it is auto-detected and returned in the result. Segments carry speaker
    labels + word timings after alignment and assign_word_speakers.
    """
    import whisperx

    device = get_device(settings)
    asr = get_transcriber(settings)
    result = asr.transcribe(
        audio, batch_size=settings.whisper_batch_size, language=language
    )

    language = result.get("language") or language
    if language:
        model_a, metadata = get_aligner(language, settings)
        aligned = whisperx.align(result["segments"], model_a, metadata, audio, device)
        # whisperx >= 3.8 returns a dict; older versions returned the list itself
        result["segments"] = aligned["segments"] if isinstance(aligned, dict) else aligned

    diarizer = get_diarizer(settings)
    diarize_segments = diarizer(audio)  # pandas DataFrame
    result = whisperx.assign_word_speakers(diarize_segments, result)
    return result


def transcribe(audio_path: str, settings: ServiceSettings) -> dict[str, Any]:
    """Run the full pipeline on a local audio file (whole-file entry point)."""
    return transcribe_audio(load_audio(audio_path), settings)


# ---------------------------------------------------------------------------
# Artifact builders (documented shapes in docs/data-model.md)
# ---------------------------------------------------------------------------


def _fmt(value: Any) -> float:
    """Convert numpy scalars/floats to plain JSON-friendly floats."""
    return round(float(value), 3)


def offset_segments(
    result: dict[str, Any], offset_sec: float, chunk: int | None = None
) -> list[dict[str, Any]]:
    """Shift every segment (and word) timestamp by offset_sec, in place.

    Chunk results carry timestamps relative to the chunk start; splicing them
    into the session timeline requires adding the chunk's start offset.
    Returns the segments list with plain (JSON-friendly) floats.

    ``chunk`` (0-based) tags every segment with its source chunk so
    speaker-service can treat chunk boundaries as hard turn boundaries when it
    re-clusters diarizer labels (raw labels restart per chunk).
    """
    offset = float(offset_sec)
    segments = result.get("segments") or []
    for seg in segments:
        seg["start"] = _fmt(seg["start"] + offset)
        seg["end"] = _fmt(seg["end"] + offset)
        if chunk is not None:
            seg["chunk"] = chunk
        for word in seg.get("words", []):
            if "start" in word:
                word["start"] = _fmt(word["start"] + offset)
            if "end" in word:
                word["end"] = _fmt(word["end"] + offset)
    return segments


def build_transcript(session_id: str, result: dict[str, Any], model_name: str) -> dict[str, Any]:
    """Full transcript artifact: segments with speaker + word-level timings."""
    return {
        "session_id": session_id,
        "language": result.get("language"),
        "model": model_name,
        "segments": [
            {
                "start": _fmt(seg["start"]),
                "end": _fmt(seg["end"]),
                "text": seg.get("text", ""),
                "speaker": seg.get("speaker"),
                **({"chunk": seg["chunk"]} if seg.get("chunk") is not None else {}),
                "words": [
                    {
                        "word": word.get("word", ""),
                        "start": _fmt(word["start"]),
                        "end": _fmt(word["end"]),
                    }
                    for word in seg.get("words", [])
                    if "start" in word and "end" in word
                ],
            }
            for seg in result.get("segments", [])
        ],
    }


def build_diarization(session_id: str, result: dict[str, Any], model_name: str) -> dict[str, Any]:
    """Compact diarization artifact: speaker -> text segments (no words)."""
    return {
        "session_id": session_id,
        "language": result.get("language"),
        "model": model_name,
        "segments": [
            {
                "start": _fmt(seg["start"]),
                "end": _fmt(seg["end"]),
                "speaker": seg.get("speaker"),
                "text": seg.get("text", ""),
                **({"chunk": seg["chunk"]} if seg.get("chunk") is not None else {}),
            }
            for seg in result.get("segments", [])
        ],
    }


def audio_duration(result: dict[str, Any]) -> float | None:
    """Best-effort audio duration from the last segment end."""
    segments = result.get("segments") or []
    if not segments:
        return None
    return float(segments[-1]["end"])
