"""Voice samples from previous sessions' manual identifications (pure logic)."""

from app.history import (
    HistoryEntry,
    HistorySample,
    SampleWindow,
    history_key,
    manual_samples,
    sample_windows,
    segment_confidence,
)

LABEL = "SPEAKER_00"


def seg(start, end, speaker=LABEL, **kw):
    return {"start": start, "end": end, "speaker": speaker, "text": "x", **kw}


def windows(segments, **overrides):
    kwargs = {
        "min_sec": 3.0,
        "max_sec": 20.0,
        "min_confidence": 0.8,
        "max_windows": 3,
    }
    kwargs.update(overrides)
    return sample_windows(segments, LABEL, **kwargs)


# --------------------------------------------------------------- confidence


def test_segment_confidence_prefers_speaker_confidence():
    assert segment_confidence({"speaker_confidence": 0.9, "confidence": 0.1}) == 0.9
    assert segment_confidence({"confidence": 0.42}) == 0.42
    assert segment_confidence({}) is None
    # a bool is not a confidence, and neither is a string
    assert segment_confidence({"confidence": True}) is None
    assert segment_confidence({"confidence": "0.9"}) is None


# --------------------------------------------------------------- windows


def test_short_turns_are_not_samples():
    """A turn shorter than the minimum carries too little voice to identify."""
    assert windows([seg(0.0, 2.0), seg(20.0, 21.5)]) == []


def test_low_confidence_turns_are_dropped():
    """The diarizer was unsure about the label: the DM naming it does not make
    the *audio* a good sample."""
    assert windows([seg(0.0, 8.0, speaker_confidence=0.55)]) == []
    # ... but a confident turn next to it still counts
    kept = windows([seg(0.0, 8.0, speaker_confidence=0.55), seg(20.0, 26.0, confidence=0.95)])
    assert [(w.start, w.end) for w in kept] == [(20.0, 26.0)]


def test_turns_without_confidence_are_kept():
    """Some backends report no confidence; the DM's naming is the label then."""
    kept = windows([seg(0.0, 7.0)])
    assert [(w.start, w.end, w.confidence) for w in kept] == [(0.0, 7.0, None)]


def test_consecutive_segments_merge_into_one_turn():
    kept = windows(
        [
            seg(0.0, 2.0, confidence=1.0),
            seg(2.5, 5.0, confidence=0.9),  # small gap -> same turn
            seg(9.0, 12.0, confidence=0.8),  # > GAP_TOLERANCE_SEC -> new turn
        ]
    )
    assert [(w.start, w.end) for w in kept] == [(0.0, 5.0), (9.0, 12.0)]
    assert [w.confidence for w in kept] == [0.95, 0.8]


def test_other_labels_are_ignored():
    assert windows([seg(0.0, 9.0, speaker="SPEAKER_01"), seg(10.0, 12.0)]) == []


def test_invalid_spans_are_ignored():
    assert windows([seg(5.0, 5.0), seg(8.0, 4.0), {"speaker": LABEL}]) == []


def test_a_long_turn_is_trimmed_to_max_sec():
    kept = windows([seg(10.0, 200.0)], max_sec=20.0)
    assert [(w.start, w.end) for w in kept] == [(10.0, 30.0)]


def test_the_longest_turns_win_but_stay_in_time_order():
    """Sampling is capped: keep the most useful turns, index them by position so
    the stored sample ids do not move between runs."""
    segments = [
        seg(0.0, 5.0),  # 5 s
        seg(60.0, 70.0),  # 10 s  <- longest
        seg(120.0, 129.0),  # 9 s
        seg(200.0, 204.0),  # 4 s   <- dropped by max_windows
    ]
    kept = windows(segments, max_windows=3)
    assert [(w.start, w.end) for w in kept] == [(0.0, 5.0), (60.0, 70.0), (120.0, 129.0)]
    assert [w.index for w in kept] == [0, 1, 2]


def test_windows_are_stable_across_calls():
    segments = [seg(0.0, 6.0), seg(30.0, 40.0)]
    assert windows(segments) == windows(segments)


# --------------------------------------------------------------- plan


def entry(session, label=LABEL, user="user-1"):
    return HistoryEntry(session_id=session, label=label, user_id=user)


def plan(history, diarizations, **overrides):
    kwargs = {
        "already_enrolled": set(),
        "min_sec": 3.0,
        "max_sec": 20.0,
        "min_confidence": 0.8,
        "max_windows_per_label": 3,
        "max_windows_per_run": 24,
    }
    kwargs.update(overrides)
    return manual_samples(history, diarizations, **kwargs)


def test_plan_uses_the_labelled_audio_of_previous_sessions():
    samples = plan(
        [entry("s1"), entry("s2")],
        {"s1": [seg(1.0, 9.0)], "s2": [seg(2.0, 6.0)]},
    )
    assert [(s.session_id, s.label, s.user_id, s.window.start) for s in samples] == [
        ("s1", LABEL, "user-1", 1.0),
        ("s2", LABEL, "user-1", 2.0),
    ]
    assert samples[0].key == history_key("s1", LABEL, 0)


def test_plan_skips_sessions_without_a_readable_diarization():
    samples = plan([entry("s1"), entry("s2")], {"s2": [seg(2.0, 6.0)]})
    assert [s.session_id for s in samples] == ["s2"]


def test_plan_skips_samples_already_in_the_store():
    """Re-identifying a session must not re-enroll the same audio."""
    diarizations = {"s1": [seg(1.0, 9.0), seg(30.0, 40.0)]}
    already = {history_key("s1", LABEL, 0)}
    samples = plan([entry("s1")], diarizations, already_enrolled=already)
    assert [s.window.index for s in samples] == [1]


def test_plan_deduplicates_the_same_label_reported_twice():
    samples = plan([entry("s1"), entry("s1")], {"s1": [seg(1.0, 9.0)]})
    assert len(samples) == 1


def test_plan_caps_the_embeddings_of_one_run():
    history = [entry(f"s{i}") for i in range(5)]
    diarizations = {f"s{i}": [seg(0.0, 9.0)] for i in range(5)}
    samples = plan(history, diarizations, max_windows_per_run=2)
    assert [s.session_id for s in samples] == ["s0", "s1"]


def test_plan_caps_the_windows_of_one_label():
    diarizations = {"s1": [seg(0.0, 9.0), seg(30.0, 40.0), seg(60.0, 68.0), seg(90.0, 95.0)]}
    samples = plan([entry("s1")], diarizations, max_windows_per_label=2)
    assert len(samples) == 2


def test_history_sample_key_is_stable():
    sample = HistorySample(
        session_id="s1", label=LABEL, user_id="u", window=SampleWindow(1.0, 4.0, 0.9, 2)
    )
    assert sample.key == "s1#SPEAKER_00#2"
