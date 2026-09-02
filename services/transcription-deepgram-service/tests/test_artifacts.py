"""Tests for the artifact builders (shapes must match the on-prem worker)."""

import json

from app.artifacts import (
    build_diarization,
    build_transcript,
    normalize_segments,
    normalize_speaker,
    offset_segments,
)

MODEL = "nova-3"


def _utterance_segments() -> list[dict]:
    """Deepgram utterances shape (transcript + words + int speaker)."""
    return [
        {
            "start": 0.0,
            "end": 3.42,
            "speaker": 0,
            "transcript": "Welcome back, heroes.",
            "words": [
                {"word": "Welcome", "start": 0.0, "end": 0.6, "speaker": 0, "speaker_confidence": 0.99},
                {"word": "back,", "start": 0.6, "end": 1.1, "speaker": 0, "speaker_confidence": 0.97},
                {"word": "heroes.", "start": 1.2, "end": 3.42, "speaker": 0, "speaker_confidence": 0.95},
            ],
        },
        {
            "start": 3.6,
            "end": 7.0,
            "speaker": 1,
            "transcript": "The goblins took the road north.",
            "words": [
                {"word": "The", "start": 3.6, "end": 3.9, "speaker": 1, "speaker_confidence": 0.88},
                {"word": "goblins", "start": 3.9, "end": 4.8, "speaker": 1, "speaker_confidence": 0.84},
            ],
        },
    ]


def test_normalize_speaker_maps_deepgram_numbers():
    assert normalize_speaker(0) == "SPEAKER_00"
    assert normalize_speaker(1) == "SPEAKER_01"
    assert normalize_speaker(12) == "SPEAKER_12"
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
            # mean of the word-level speaker_confidence values (0.99+0.97+0.95)/3
            "speaker_confidence": 0.97,
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
            "speaker_confidence": 0.86,
        },
    ]


def test_normalize_segments_app_shape_and_rounding():
    out = normalize_segments(
        [
            {"start": 0.0, "end": 1.234567, "speaker": 0, "text": "hi", "words": []},
            {"start": 0.0, "end": 1.0, "speaker": 0, "text": "direct", "speaker_confidence": 0.923456},
            {"start": None, "end": 5.0, "speaker": 0, "text": "no start"},
            {"start": 6.0, "end": None, "speaker": 0, "text": "no end"},
            {"start": 1.0, "end": 2.0, "text": "no speaker"},
        ]
    )
    assert out == [
        {"start": 0.0, "end": 1.235, "speaker": "SPEAKER_00", "text": "hi", "words": [], "speaker_confidence": None},
        {"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00", "text": "direct", "words": [], "speaker_confidence": 0.923},
        {"start": 1.0, "end": 2.0, "speaker": None, "text": "no speaker", "words": [], "speaker_confidence": None},
    ]


def test_normalize_segments_empty_and_none():
    assert normalize_segments(None) == []
    assert normalize_segments([]) == []


def test_offset_segments_shifts_timestamps_and_words():
    """Chunk results are spliced into the session timeline by chunk start."""
    segments = normalize_segments(
        [
            {
                "start": 0.5,
                "end": 1.5,
                "speaker": 1,
                "transcript": "due",
                "words": [{"word": "due", "start": 0.5, "end": 1.5, "speaker": 1, "speaker_confidence": 0.91}],
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
            "speaker_confidence": 0.91,
        }
    ]
    json.dumps(out)  # must be JSON-serializable


def test_offset_segments_rounds_to_3_decimals():
    segments = [{"start": 0.123456, "end": 1.987654, "speaker": "SPEAKER_00", "text": "x", "words": []}]
    out = offset_segments(segments, 10.0)
    assert out[0]["start"] == 10.123
    assert out[0]["end"] == 11.988


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
    # diarization confidence survives into the artifact
    assert seg["speaker_confidence"] == 0.97
    json.dumps(out)  # must be JSON-serializable


def test_build_diarization_is_compact():
    """Event-contract segments: start/end/speaker/text (+ confidence), no words."""
    segments = normalize_segments(_utterance_segments())
    out = build_diarization("sess-1", "it", MODEL, segments)
    assert out["segments"] == [
        {
            "start": 0.0, "end": 3.42, "speaker": "SPEAKER_00",
            "text": "Welcome back, heroes.", "speaker_confidence": 0.97,
        },
        {
            "start": 3.6, "end": 7.0, "speaker": "SPEAKER_01",
            "text": "The goblins took the road north.", "speaker_confidence": 0.86,
        },
    ]
