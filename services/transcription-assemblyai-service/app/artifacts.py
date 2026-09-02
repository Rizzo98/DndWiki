"""Artifact builders for the AssemblyAI diarized transcription response.

The on-prem WhisperX worker emits transcript.json / diarization.json with
documented shapes (docs/data-model.md, docs/event-contracts.md). This module
produces the same shapes from AssemblyAI's transcript response (utterances),
so the downstream consumers (speaker-service, refiner-service, content-service,
web UI) cannot tell which backend produced them.

AssemblyAI quirks handled here:

- Timestamps are **milliseconds**; they are converted to seconds and rounded
  to 3 decimals, matching every other backend.
- Speaker labels are sequential **letters** (A, B, C, ...). They are mapped
  to the canonical `SPEAKER_XX` format the rest of the pipeline expects
  (docs/data-model.md); labels restart per chunk, exactly like the on-prem
  pyannote output, and speaker-service re-clusters them into stable
  identities.
- AssemblyAI reports per-utterance and per-word `confidence` (0..1) for the
  transcribed text (it has no dedicated diarization-confidence field). The
  segment's `speaker_confidence` is filled from that value so the web UI's
  low-confidence flagging keeps working; it is a text-confidence signal, not a
  label-attribution one. The same value is also exposed under the segment's
  plain `confidence` key so the refiner's LLM pass can take the ASR
  probability per sentence into account (other backends simply omit it).
- Word-level timings are available (like Deepgram), so transcript.json
  segments carry real `words` lists — matching the on-prem worker's shape.
"""

from __future__ import annotations

import re
from typing import Any

# AssemblyAI speaker labels are letters ("A", "B", ..., "Z", "AA", ...).
_SPEAKER_LETTER_RE = re.compile(r"^[A-Za-z]{1,3}$")
# Deepgram-style integer speaker numbers (ints or numeric strings).
_SPEAKER_INT_RE = re.compile(r"^\d+$")


def _letter_to_index(label: str) -> int:
    """Spreadsheet-style column number for a letter label (A=0, B=1, ..., AA=26)."""
    index = 0
    for char in label.upper():
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def normalize_speaker(value: Any) -> str | None:
    """Map an AssemblyAI/Deepgram speaker label to a canonical `SPEAKER_XX`.

    Letter labels ("A", "B", "AA") -> `SPEAKER_00`, `SPEAKER_01`, ...;
    ints/floats and numeric strings (0, 1, ...) -> `SPEAKER_00`,
    `SPEAKER_01`; already-canonical labels pass through; anything else is
    left untouched — labels are opaque to the downstream matching pipeline.
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
    if _SPEAKER_LETTER_RE.match(text):
        return f"SPEAKER_{_letter_to_index(text):02d}"
    return text or None


def _fmt_sec(value: Any) -> float:
    """Convert a millisecond timestamp to seconds, rounded to 3 decimals.

    AssemblyAI reports start/end in milliseconds; the rest of the pipeline
    (and the on-prem worker) uses seconds.
    """
    return round(float(value) / 1000.0, 3)


def _word_meta(word: dict[str, Any]) -> dict[str, Any]:
    """AssemblyAI word -> {word, start, end} (the on-prem transcript shape)."""
    return {
        "word": word.get("text", ""),
        "start": _fmt_sec(word["start"]) if word.get("start") is not None else None,
        "end": _fmt_sec(word["end"]) if word.get("end") is not None else None,
    }


def _fmt_confidence(value: Any) -> float:
    """Round a 0..1 confidence to 3 decimals (no unit conversion)."""
    return round(float(value), 3)


def _speaker_confidence(seg: dict[str, Any]) -> float | None:
    """Best-effort segment-level confidence (0..1).

    AssemblyAI has no diarization-confidence field; the utterance `confidence`
    (transcription confidence) is surfaced so the web UI's low-confidence
    flagging keeps working. Prefers a direct `speaker_confidence` field when
    the provider carries one at segment level; missing entirely -> None.
    """
    direct = seg.get("speaker_confidence")
    if isinstance(direct, (int, float)) and not isinstance(direct, bool):
        return float(direct)
    conf = seg.get("confidence")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool):
        return float(conf)
    return None


def normalize_segments(segments: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize raw AssemblyAI utterances into the app's segment shape:
    {start, end, text, speaker, words, speaker_confidence, confidence} (seconds).

    Accepts both utterance-shaped dicts (`text` + ms timings) and
    app-shaped ones; segments missing usable timings are dropped. The
    plain `confidence` key carries the utterance ASR probability for the
    refiner (optional; see module docstring).
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
                "start": _fmt_sec(start),
                "end": _fmt_sec(end),
                "text": seg.get("text") or seg.get("transcript") or "",
                "speaker": normalize_speaker(seg.get("speaker")),
                "words": words,
                "speaker_confidence": _fmt_confidence(confidence) if confidence is not None else None,
                # Plain-text ASR probability for this utterance (0..1); the
                # refiner reads it to decide whether context can raise it.
                "confidence": _fmt_confidence(confidence) if confidence is not None else None,
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
        seg["start"] = round(seg["start"] + offset, 3)
        seg["end"] = round(seg["end"] + offset, 3)
        if chunk is not None:
            seg["chunk"] = chunk
        for word in seg.get("words", []):
            if word.get("start") is not None:
                word["start"] = round(word["start"] + offset, 3)
            if word.get("end") is not None:
                word["end"] = round(word["end"] + offset, 3)
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
                "confidence": seg.get("confidence"),
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
                "confidence": seg.get("confidence"),
            }
            for seg in segments
        ],
    }
