"""Tests for the artifact builders (shapes must match the on-prem worker)."""

import json

from app.artifacts import (
    build_diarization,
    build_transcript,
    normalize_segments,
    offset_segments,
)

MODEL = "gpt-4o-transcribe-diarize"


def _raw_segments() -> list[dict]:
    return [
        {
            "start": 0.0,
            "end": 3.42,
            "speaker": "Speaker 1",
            "text": "Welcome back, heroes.",
        },
        {
            "start": 3.6,
            "end": 7.0,
            "speaker": "Speaker 2",
            "text": "The goblins took the road north.",
        },
    ]


def test_normalize_segments_shape():
    out = normalize_segments(_raw_segments())
    assert out == [
        {"start": 0.0, "end": 3.42, "speaker": "Speaker 1", "text": "Welcome back, heroes."},
        {"start": 3.6, "end": 7.0, "speaker": "Speaker 2", "text": "The goblins took the road north."},
    ]


def test_normalize_segments_drops_bad_timings_and_rounds():
    out = normalize_segments(
        [
            {"start": 0.0, "end": 1.234567, "speaker": "Speaker 1", "text": "hi"},
            {"start": None, "end": 5.0, "speaker": "Speaker 1", "text": "no start"},
            {"start": 6.0, "end": None, "speaker": "Speaker 1", "text": "no end"},
            {"start": 1.0, "end": 2.0, "text": "no speaker"},
        ]
    )
    assert out == [
        {"start": 0.0, "end": 1.235, "speaker": "Speaker 1", "text": "hi"},
        {"start": 1.0, "end": 2.0, "speaker": None, "text": "no speaker"},
    ]


def test_normalize_segments_empty_and_none():
    assert normalize_segments(None) == []
    assert normalize_segments([]) == []


def test_offset_segments_shifts_timestamps():
    """Chunk results are spliced into the session timeline by chunk start."""
    segments = normalize_segments(
        [{"start": 0.5, "end": 1.5, "speaker": "Speaker 1", "text": "due"}]
    )
    out = offset_segments(segments, 5.0)
    assert out == [
        {"start": 5.5, "end": 6.5, "speaker": "Speaker 1", "text": "due"}
    ]
    json.dumps(out)  # must be JSON-serializable


def test_offset_segments_rounds_to_3_decimals():
    segments = [{"start": 0.123456, "end": 1.987654, "speaker": "S1", "text": "x"}]
    out = offset_segments(segments, 10.0)
    assert out[0]["start"] == 10.123
    assert out[0]["end"] == 11.988


def test_build_transcript_shape():
    """transcript_uri JSON: segments carry empty words lists (no word timings)."""
    segments = normalize_segments(_raw_segments())
    out = build_transcript("sess-1", "it", MODEL, segments)
    assert out["session_id"] == "sess-1"
    assert out["language"] == "it"
    assert out["model"] == MODEL
    assert len(out["segments"]) == 2
    seg = out["segments"][0]
    assert seg["start"] == 0.0
    assert seg["end"] == 3.42
    assert seg["speaker"] == "Speaker 1"
    assert seg["text"] == "Welcome back, heroes."
    assert seg["words"] == []
    json.dumps(out)  # must be JSON-serializable


def test_build_diarization_is_compact():
    """Event-contract segments: start/end/speaker/text, no words."""
    segments = normalize_segments(_raw_segments())
    out = build_diarization("sess-1", "it", MODEL, segments)
    assert out["segments"] == [
        {"start": 0.0, "end": 3.42, "speaker": "Speaker 1", "text": "Welcome back, heroes."},
        {"start": 3.6, "end": 7.0, "speaker": "Speaker 2", "text": "The goblins took the road north."},
    ]
