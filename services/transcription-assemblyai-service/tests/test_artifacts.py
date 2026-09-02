"""Tests for the artifact builders (shapes must match the on-prem worker)."""

import json

from app.artifacts import (
    build_diarization,
    build_transcript,
    normalize_segments,
    normalize_speaker,
    offset_segments,
)

MODEL = "universal-3-5-pro"


def _utterance_segments() -> list[dict]:
    """AssemblyAI utterances shape (ms timings + letter speakers)."""
    return [
        {
            "speaker": "A",
            "text": "Welcome back, heroes.",
            "confidence": 0.97,
            "start": 0,
            "end": 3420,
            "words": [
                {"text": "Welcome", "start": 0, "end": 600, "confidence": 0.99, "speaker": "A"},
                {"text": "back,", "start": 600, "end": 1100, "confidence": 0.97, "speaker": "A"},
                {"text": "heroes.", "start": 1200, "end": 3420, "confidence": 0.95, "speaker": "A"},
            ],
        },
        {
            "speaker": "B",
            "text": "The goblins took the road north.",
            "confidence": 0.88,
            "start": 3600,
            "end": 7000,
            "words": [
                {"text": "The", "start": 3600, "end": 3900, "confidence": 0.9, "speaker": "B"},
                {"text": "goblins", "start": 3900, "end": 4800, "confidence": 0.84, "speaker": "B"},
            ],
        },
    ]


def test_normalize_speaker_maps_letters_and_numbers():
    assert normalize_speaker("A") == "SPEAKER_00"
    assert normalize_speaker("B") == "SPEAKER_01"
    assert normalize_speaker("C") == "SPEAKER_02"
    assert normalize_speaker("AA") == "SPEAKER_26"
    assert normalize_speaker("ab") == "SPEAKER_27"
    assert normalize_speaker(0) == "SPEAKER_00"
    assert normalize_speaker("3") == "SPEAKER_03"
    assert normalize_speaker("SPEAKER_05") == "SPEAKER_05"
    assert normalize_speaker(None) is None
    assert normalize_speaker("Speaker 1") == "Speaker 1"  # opaque labels pass through


def test_normalize_segments_utterance_shape():
    out = normalize_segments(_utterance_segments())
    assert out == [
        {
            "start": 0.0,
            "end": 3.42,
            "speaker": "SPEAKER_00",
            "text": "Welcome back, heroes.",
            "words": [
                {"word": "Welcome", "start": 0.0, "end": 0.6},
                {"word": "back,", "start": 0.6, "end": 1.1},
                {"word": "heroes.", "start": 1.2, "end": 3.42},
            ],
            # AssemblyAI has no diarization confidence; the utterance
            # transcription confidence is surfaced as speaker_confidence
            # (and as the plain confidence key for the refiner).
            "speaker_confidence": 0.97,
            "confidence": 0.97,
        },
        {
            "start": 3.6,
            "end": 7.0,
            "speaker": "SPEAKER_01",
            "text": "The goblins took the road north.",
            "words": [
                {"word": "The", "start": 3.6, "end": 3.9},
                {"word": "goblins", "start": 3.9, "end": 4.8},
            ],
            "speaker_confidence": 0.88,
            "confidence": 0.88,
        },
    ]


def test_normalize_segments_app_shape_and_rounding():
    out = normalize_segments(
        [
            {"start": 0, "end": 1234.5, "speaker": "A", "text": "hi", "words": []},
            {"start": 0, "end": 1000, "speaker": "A", "text": "direct", "confidence": 0.923456},
            {"start": None, "end": 5000, "speaker": "A", "text": "no start"},
            {"start": 6000, "end": None, "speaker": "A", "text": "no end"},
            {"start": 1000, "end": 2000, "text": "no speaker"},
        ]
    )
    assert out == [
        {"start": 0.0, "end": 1.234, "speaker": "SPEAKER_00", "text": "hi", "words": [], "speaker_confidence": None, "confidence": None},
        {"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00", "text": "direct", "words": [], "speaker_confidence": 0.923, "confidence": 0.923},
        {"start": 1.0, "end": 2.0, "speaker": None, "text": "no speaker", "words": [], "speaker_confidence": None, "confidence": None},
    ]


def test_normalize_segments_empty_and_none():
    assert normalize_segments(None) == []
    assert normalize_segments([]) == []


def test_offset_segments_shifts_timestamps_and_words():
    """Chunk results are spliced into the session timeline by chunk start."""
    segments = normalize_segments(
        [
            {
                "speaker": "B",
                "text": "due",
                "start": 500,
                "end": 1500,
                "words": [{"text": "due", "start": 500, "end": 1500, "speaker": "B"}],
            }
        ]
    )
    out = offset_segments(segments, 5.0)
    assert out == [
        {
            "start": 5.5,
            "end": 6.5,
            "speaker": "SPEAKER_01",
            "text": "due",
            "words": [{"word": "due", "start": 5.5, "end": 6.5}],
            "speaker_confidence": None,
            "confidence": None,
        }
    ]
    json.dumps(out)  # must be JSON-serializable


def test_build_transcript_shape():
    """transcript_uri JSON: segments carry speaker + real word timings."""
    segments = normalize_segments(_utterance_segments())
    out = build_transcript("sess-1", "it", MODEL, segments)
    assert out["session_id"] == "sess-1"
    assert out["language"] == "it"
    assert out["model"] == MODEL
    assert len(out["segments"]) == 2
    seg = out["segments"][0]
    assert seg["start"] == 0.0
    assert seg["end"] == 3.42
    assert seg["speaker"] == "SPEAKER_00"
    assert seg["text"] == "Welcome back, heroes."
    assert seg["words"][0] == {"word": "Welcome", "start": 0.0, "end": 0.6}
    assert seg["speaker_confidence"] == 0.97
    json.dumps(out)  # must be JSON-serializable


def test_build_diarization_is_compact():
    """Event-contract segments: start/end/speaker/text (+ confidence), no words."""
    segments = normalize_segments(_utterance_segments())
    out = build_diarization("sess-1", "it", MODEL, segments)
    assert out["segments"] == [
        {
            "start": 0.0, "end": 3.42, "speaker": "SPEAKER_00",
            "text": "Welcome back, heroes.", "speaker_confidence": 0.97, "confidence": 0.97,
        },
        {
            "start": 3.6, "end": 7.0, "speaker": "SPEAKER_01",
            "text": "The goblins took the road north.", "speaker_confidence": 0.88, "confidence": 0.88,
        },
    ]
