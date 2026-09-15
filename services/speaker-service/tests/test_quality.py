"""Per-observation quality tests: pure policy + synthetic feature extraction."""

import pytest

from app.quality import (
    DEFAULT_MIN_SEC,
    NEUTRAL_SNR_SCORE,
    USABLE_FLOOR,
    assess,
    combine_scores,
    estimate_overlap_ratio,
    estimate_snr_db,
    length_score,
    overlap_score,
    snr_score,
)

np = pytest.importorskip("numpy")


# --- length -----------------------------------------------------------------


def test_length_score_is_zero_below_the_minimum():
    assert length_score(0.5, min_sec=1.0, full_sec=6.0) == 0.0
    assert length_score(DEFAULT_MIN_SEC - 0.01) == 0.0


def test_length_score_ramps_and_saturates():
    assert length_score(1.0, min_sec=1.0, full_sec=6.0) == 0.0
    assert length_score(3.5, min_sec=1.0, full_sec=6.0) == pytest.approx(0.5)
    assert length_score(6.0, min_sec=1.0, full_sec=6.0) == 1.0
    assert length_score(600.0, min_sec=1.0, full_sec=6.0) == 1.0


def test_length_score_degrades_gracefully_on_degenerate_bounds():
    assert length_score(2.0, min_sec=5.0, full_sec=5.0) == 0.0
    assert length_score(5.0, min_sec=5.0, full_sec=5.0) == 1.0


# --- SNR --------------------------------------------------------------------


def test_unknown_snr_scores_neutral_not_zero():
    assert snr_score(None) == NEUTRAL_SNR_SCORE


def test_snr_score_ramps_between_floor_and_full():
    assert snr_score(5.0, floor_db=5.0, full_db=20.0) == 0.0
    assert snr_score(12.5, floor_db=5.0, full_db=20.0) == pytest.approx(0.5)
    assert snr_score(40.0, floor_db=5.0, full_db=20.0) == 1.0
    assert snr_score(-3.0, floor_db=5.0, full_db=20.0) == 0.0


# --- overlap ----------------------------------------------------------------


def test_overlap_score_penalises_and_floors_at_zero():
    assert overlap_score(0.0) == 1.0
    assert overlap_score(0.25) == pytest.approx(0.5)
    assert overlap_score(0.5) == 0.0
    assert overlap_score(0.9) == 0.0
    assert overlap_score(-1.0) == 1.0


# --- combination ------------------------------------------------------------


def test_combine_scores_is_a_weighted_geometric_mean():
    assert combine_scores(1.0, 1.0, 1.0) == pytest.approx(1.0)
    assert combine_scores(0.0, 1.0, 1.0) == pytest.approx(0.0, abs=1e-6)
    mid = combine_scores(0.5, 0.5, 0.5)
    assert mid == pytest.approx(0.5, abs=1e-6)


def test_a_hard_zero_in_any_component_dominates():
    # A long, clean window with no length is still unusable; so is a window with
    # zero SNR, however long it is.
    assert combine_scores(0.0, 1.0, 1.0) == 0.0
    assert combine_scores(1.0, 0.0, 1.0) == 0.0
    assert combine_scores(1.0, 1.0, 0.0) == 0.0


def test_assess_marks_short_and_noisy_windows_unusable():
    short = assess(0.4, snr_db=30.0, overlap_ratio=0.0)
    assert short.usable is False
    assert short.length_score == 0.0

    clean = assess(8.0, snr_db=25.0, overlap_ratio=0.0)
    assert clean.usable is True
    assert clean.score > 0.9


def test_assess_penalises_overlap_without_zeroing_quality():
    clean = assess(8.0, snr_db=25.0, overlap_ratio=0.0)
    overlapped = assess(8.0, snr_db=25.0, overlap_ratio=0.4)
    assert overlapped.score < clean.score
    assert overlapped.score >= USABLE_FLOOR  # down-weighted, not discarded


def test_assess_payload_is_json_friendly():
    payload = assess(8.0, snr_db=None, overlap_ratio=0.123456).as_payload()
    assert set(payload) == {"quality", "snr_db", "overlap_ratio"}
    assert payload["snr_db"] is None
    assert payload["overlap_ratio"] == 0.1235


# --- feature extraction -----------------------------------------------------

SR = 16000


def tone(seconds, freq=200.0, amplitude=0.3):
    t = np.arange(int(seconds * SR)) / SR
    return amplitude * np.sin(2 * np.pi * freq * t)


def test_snr_is_high_for_a_loud_tone_over_a_quiet_floor():
    # A full second of quiet floor is needed for the lowest-20% estimate to see
    # noise and not a mix of noise and speech.
    signal = np.concatenate([tone(1.0, amplitude=0.0001), tone(1.0, amplitude=0.3)])
    snr = estimate_snr_db(signal, SR)
    assert snr is not None and snr > 30.0


def test_snr_drops_when_the_floor_rises():
    quiet = np.concatenate([tone(1.0, amplitude=0.0001), tone(1.0, amplitude=0.3)])
    noisy = np.concatenate([tone(1.0, amplitude=0.05), tone(1.0, amplitude=0.3)])
    assert estimate_snr_db(noisy, SR) < estimate_snr_db(quiet, SR)


def test_snr_is_none_for_silence_and_for_too_short_windows():
    assert estimate_snr_db(np.zeros(SR), SR) is None
    assert estimate_snr_db(np.zeros(10), SR) is None


def test_overlap_ratio_is_zero_for_a_clean_periodic_tone():
    assert estimate_overlap_ratio(tone(2.0), SR) == 0.0


def test_overlap_ratio_rises_when_a_bright_noisy_burst_lands_on_the_tone():
    signal = tone(2.0).copy()
    # loud high-frequency noise for a third of the window: the other talker
    rng = np.random.default_rng(0)
    start = int(0.7 * SR)
    stop = int(1.4 * SR)
    signal[start:stop] += rng.normal(0.0, 1.0, stop - start)
    assert estimate_overlap_ratio(signal, SR) > 0.1


def test_overlap_ratio_of_nothing_is_zero():
    assert estimate_overlap_ratio(np.zeros(10), SR) == 0.0
    assert estimate_overlap_ratio(np.zeros(SR), SR) == 0.0
