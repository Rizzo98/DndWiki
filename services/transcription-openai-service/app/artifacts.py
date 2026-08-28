"""Artifact builders for the OpenAI diarized transcription response.

The on-prem WhisperX worker emits transcript.json / diarization.json with
documented shapes (docs/data-model.md, docs/event-contracts.md). This module
produces the same shapes from the OpenAI `diarized_json` response, so the
downstream consumers (speaker-service, content-service, web UI) cannot tell
which backend produced them.

One difference: OpenAI's diarized output has no word-level timings, so
transcript.json segments carry an empty `words` list (the UI and the LLM
pipeline treat words as optional).
"""

from __future__ import annotations

from typing import Any


def _fmt(value: Any) -> float:
    """Convert numpy/scalar-like timings to plain JSON-friendly floats."""
    return round(float(value), 3)


def normalize_segments(segments: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize raw OpenAI diarized segments into the app's segment shape.

    Keeps start/end/text/speaker (JSON-friendly floats); segments missing
    usable timings are dropped.
    """
    out: list[dict[str, Any]] = []
    for seg in segments or []:
        start = seg.get("start")
        end = seg.get("end")
        if start is None or end is None:
            continue
        out.append(
            {
                "start": _fmt(start),
                "end": _fmt(end),
                "text": seg.get("text") or "",
                "speaker": seg.get("speaker"),
            }
        )
    return out


def offset_segments(
    segments: list[dict[str, Any]],
    offset_sec: float,
    chunk: int | None = None,
) -> list[dict[str, Any]]:
    """Shift every segment timestamp by offset_sec, in place.

    Chunk results carry timestamps relative to the chunk start; splicing them
    into the session timeline requires adding the chunk's start offset.
    Returns the segments list with plain (JSON-friendly) floats.

    ``chunk`` (0-based) tags every segment with its source chunk so
    speaker-service can treat chunk boundaries as hard turn boundaries when it
    re-clusters diarizer labels (raw labels restart per chunk).
    """
    offset = float(offset_sec)
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


def build_transcript(
    session_id: str,
    language: str | None,
    model_name: str,
    segments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Full transcript artifact: segments with speaker + empty word lists."""
    return {
        "session_id": session_id,
        "language": language,
        "model": model_name,
        "segments": [
            {
                "start": seg["start"],
                "end": seg["end"],
                "text": seg["text"],
                "speaker": seg["speaker"],
                **({"chunk": seg["chunk"]} if seg.get("chunk") is not None else {}),
                "words": [],
            }
            for seg in segments
        ],
    }


def build_diarization(
    session_id: str,
    language: str | None,
    model_name: str,
    segments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compact diarization artifact: speaker -> text segments (no words)."""
    return {
        "session_id": session_id,
        "language": language,
        "model": model_name,
        "segments": [
            {
                "start": seg["start"],
                "end": seg["end"],
                "speaker": seg["speaker"],
                "text": seg["text"],
                **({"chunk": seg["chunk"]} if seg.get("chunk") is not None else {}),
            }
            for seg in segments
        ],
    }
