"""Window-picking tests (pure logic; no torch needed)."""

from app.audio import GAP_TOLERANCE_SEC, speaker_windows


def seg(start, end, speaker="SPEAKER_00"):
    return {"start": start, "end": end, "speaker": speaker}


def test_single_speaker_single_segment():
    windows = speaker_windows([seg(0.0, 10.0)])
    assert windows == {"SPEAKER_00": (0.0, 10.0)}


def test_two_speakers():
    windows = speaker_windows(
        [
            seg(0.0, 5.0, "SPEAKER_00"),
            seg(5.5, 9.0, "SPEAKER_01"),
            seg(6.0, 12.0, "SPEAKER_00"),  # 1.0s gap -> same run as (0,5)
        ]
    )
    assert windows["SPEAKER_00"] == (0.0, 12.0)  # merged across the short gap
    assert windows["SPEAKER_01"] == (5.5, 9.0)


def test_runs_split_on_long_gap():
    # gap > tolerance -> two runs; the longer run wins
    windows = speaker_windows(
        [seg(0.0, 2.0), seg(2.5, 3.0), seg(20.0, 30.0)]
    )
    assert windows["SPEAKER_00"] == (20.0, 30.0)


def test_window_capped_at_pool_sec():
    windows = speaker_windows([seg(0.0, 100.0)], pool_sec=20.0)
    assert windows["SPEAKER_00"] == (0.0, 20.0)


def test_skips_segments_without_speaker_or_duration():
    windows = speaker_windows(
        [
            {"start": 0.0, "end": 5.0, "speaker": None},
            {"start": 0.0, "end": 0.0, "speaker": "SPEAKER_00"},
            {"start": 3.0, "end": 1.0, "speaker": "SPEAKER_00"},  # end <= start
        ]
    )
    assert windows == {}


def test_empty_input():
    assert speaker_windows([]) == {}


def test_single_segment_window():
    assert speaker_windows([seg(0.0, 1.0)])["SPEAKER_00"] == (0.0, 1.0)


def test_gap_tolerance_constant_sane():
    assert GAP_TOLERANCE_SEC > 0
