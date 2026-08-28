"""Match-policy tests (match_label)."""

from types import SimpleNamespace

from app.core.config import ServiceSettings
from app.identify import match_label

SETTINGS = ServiceSettings(speaker_match_threshold=0.75, voice_sample_min_sec=2.0)


def hit(score, user_id="u-1"):
    return SimpleNamespace(score=score, payload={"user_id": user_id})


def test_no_embedding_pending():
    out = match_label(
        "SPEAKER_00", settings=SETTINGS, embedding=None, duration_sec=0.0, hits=[]
    )
    assert out == {
        "speaker_label": "SPEAKER_00",
        "user_id": None,
        "confidence": None,
        "status": "pending",
    }


def test_too_short_pending():
    out = match_label(
        "SPEAKER_00", settings=SETTINGS, embedding=[0.1] * 192, duration_sec=1.0, hits=[]
    )
    assert out["status"] == "pending"
    assert out["user_id"] is None


def test_auto_match_above_threshold():
    out = match_label(
        "SPEAKER_00",
        settings=SETTINGS,
        embedding=[0.1] * 192,
        duration_sec=5.0,
        hits=[hit(0.91, user_id="alice")],
    )
    assert out == {
        "speaker_label": "SPEAKER_00",
        "user_id": "alice",
        "confidence": 0.91,
        "status": "auto",
    }


def test_below_threshold_pending_keeps_confidence():
    out = match_label(
        "SPEAKER_00",
        settings=SETTINGS,
        embedding=[0.1] * 192,
        duration_sec=5.0,
        hits=[hit(0.42, user_id="bob")],
    )
    assert out["status"] == "pending"
    assert out["user_id"] is None
    assert out["confidence"] == 0.42


def test_no_hits_pending_no_confidence():
    out = match_label(
        "SPEAKER_00",
        settings=SETTINGS,
        embedding=[0.1] * 192,
        duration_sec=5.0,
        hits=[],
    )
    assert out["status"] == "pending"
    assert out["confidence"] is None
