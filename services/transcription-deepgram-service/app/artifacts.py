"""Artifact builders for the Deepgram diarized transcription response.

The on-prem WhisperX worker emits transcript.json / diarization.json with
documented shapes (docs/data-model.md, docs/event-contracts.md). This module
produces the same shapes from Deepgram's listen response, so the downstream
consumers (speaker-service, refiner-service, content-service, web UI) cannot
tell which backend produced them.

Deepgram returns integer speaker numbers per word/paragraph/utterance
(0, 1, 2, ...). These are normalized to the canonical `SPEAKER_XX` label
format the rest of the pipeline expects (docs/data-model.md); labels restart
per chunk, exactly like the on-prem pyannote output, and speaker-service
re-clusters them into stable identities.

Unlike the OpenAI variant (whose diarized output has no word-level timings),
Deepgram returns word timings, so transcript.json segments carry real
`words` lists — matching the on-prem worker's shape.

Deepgram also reports a per-word `speaker_confidence` (0..1) for the
diarization. Every segment carries a `speaker_confidence` value (direct
field when present, otherwise the mean of its words), which the web UI uses
to flag labels that are likely mis-attributed and worth re-assigning. The
refiner and speaker-service preserve unknown segment fields, so the value
survives LLM refinement and voiceprint re-clustering untouched.
"""

from __future__ import annotations

import re
from typing import Any

# Deepgram speaker numbers are integers per channel; the pipeline's canonical
# label format is zero-padded SPEAKER_XX (docs/data-model.md).
_SPEAKER_INT_RE = re.compile(r"^\d+$")


def normalize_speaker(value: Any) -> str | None:
    """Map a Deepgram speaker number to a canonical `SPEAKER_XX` label.

    ints/floats and numeric strings (0, 1, ...) -> `SPEAKER_00`, `SPEAKER_01`;
    already-canonical labels are passed through; anything else (e.g. other
    providers' `Speaker 1`) is left untouched — labels are opaque to the
    downstream matching pipeline.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass; not a speaker
        return str(value)
    if isinstance(value, (int, float)):
        return f"SPEAKER_{int(value):02d}"
    text = str(value).strip()
    if _SPEAKER_INT_RE.match(text):
        return f"SPEAKER_{int(text):02d}"
    return text or None


def _word_meta(word: dict[str, Any]) -> dict[str, Any]:
    """Deepgram word -> {word, start, end} (the on-prem transcript shape)."""
    return {
        "word": word.get("word", ""),
        "start": _fmt(word["start"]) if word.get("start") is not None else None,
        "end": _fmt(word["end"]) if word.get("end") is not None else None,
    }


def _fmt(value: Any) -> float:
    """Convert numpy/scalar-like timings to plain JSON-friendly floats."""
    return round(float(value), 3)


def _speaker_confidence(seg: dict[str, Any]) -> float | None:
    """Best-effort segment-level diarization confidence (0..1).

    Prefers a direct `speaker_confidence` field when the provider carries one
    at segment level; otherwise averages the word-level `speaker_confidence`
    values Deepgram reports per word. Missing entirely -> None (no confidence
    information for this segment).
    """
    direct = seg.get("speaker_confidence")
    if isinstance(direct, (int, float)) and not isinstance(direct, bool):
        return float(direct)
    values = [
        v
        for v in (w.get("speaker_confidence") for w in (seg.get("words") or []))
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    ]
    if not values:
        return None
    return sum(values) / len(values)


def normalize_segments(segments: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize raw Deepgram segments (utterances/paragraphs/word-runs) into
    the app's segment shape: {start, end, text, speaker, words, speaker_confidence}.

    Accepts both utterance-shaped dicts (`transcript` field) and
    app-shaped ones (`text`); segments missing usable timings are dropped.
    `speaker_confidence` is the segment-level diarization confidence (direct
    field or word average); the UI uses it to flag labels worth re-checking.
    """
    out: list[dict[str, Any]] = []
    for seg in segments or []:
        start = seg.get("start")
        end = seg.get("end")
        if start is None or end is None:
            continue
        words = [
            _word_meta(w)
            for w in (seg.get("words") or [])
            if w.get("start") is not None and w.get("end") is not None
        ]
        confidence = _speaker_confidence(seg)
        out.append(
            {
                "start": _fmt(start),
                "end": _fmt(end),
                "text": seg.get("text") or seg.get("transcript") or "",
                "speaker": normalize_speaker(seg.get("speaker")),
                "words": words,
                "speaker_confidence": _fmt(confidence) if confidence is not None else None,
            }
        )
    return out


def offset_segments(
    segments: list[dict[str, Any]],
    offset_sec: float,
    chunk: int | None = None,
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
    for seg in segments:
        seg["start"] = _fmt(seg["start"] + offset)
        seg["end"] = _fmt(seg["end"] + offset)
        if chunk is not None:
            seg["chunk"] = chunk
        for word in seg.get("words", []):
            if word.get("start") is not None:
                word["start"] = _fmt(word["start"] + offset)
            if word.get("end") is not None:
                word["end"] = _fmt(word["end"] + offset)
    return segments


def build_transcript(
    session_id: str,
    language: str | None,
    model_name: str,
    segments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Full transcript artifact: segments with speaker + word-level timings."""
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
                "words": seg.get("words", []),
                "speaker_confidence": seg.get("speaker_confidence"),
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
                "speaker_confidence": seg.get("speaker_confidence"),
            }
            for seg in segments
        ],
    }
