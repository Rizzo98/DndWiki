"""Calibration tests: cosine -> log LR, and the campaign's own yardstick."""

import random

import pytest

from app.calibration import (
    COLD_START_MIDPOINT,
    COLD_START_SLOPE,
    LLR_CLIP,
    MAX_CHANNEL_WEIGHT,
    MIN_CHANNEL_WEIGHT,
    AnswerExample,
    Calibration,
    CalibrationParams,
    fit_channel_weights,
    fit_score_to_llr,
    silverman_bandwidth,
)

A, B = "member:a", "member:b"


# --- cold start -------------------------------------------------------------


def test_cold_start_is_a_logistic_ramp_through_the_midpoint():
    params = CalibrationParams.cold_start()
    assert params.log_lr(COLD_START_MIDPOINT) == pytest.approx(0.0)
    assert params.log_lr(0.9) > 0
    assert params.log_lr(0.3) < 0
    assert params.fitted is False


def test_cold_start_ramp_slope_is_the_configured_one():
    params = CalibrationParams.cold_start()
    assert params.raw_log_lr(COLD_START_MIDPOINT + 0.1) == pytest.approx(
        COLD_START_SLOPE * 0.1
    )


def test_the_log_lr_is_clipped_so_one_channel_cannot_dominate():
    params = CalibrationParams.cold_start()
    # the cold-start ramp spans -6.6..5.4, so the clip binds only at the bottom
    assert params.log_lr(0.0) == pytest.approx(-LLR_CLIP)
    assert params.log_lr(1.0) == pytest.approx(COLD_START_SLOPE * 0.45)
    # a FITTED curve happily returns +40 for a point in the far tail, which is
    # exactly what the clip is there to stop
    fitted = CalibrationParams(
        kind="kde", same=(0.99, 0.98), different=(0.1, 0.2), bandwidth=0.02
    )
    assert fitted.raw_log_lr(0.999) > LLR_CLIP
    assert fitted.log_lr(0.999) == pytest.approx(LLR_CLIP)


def test_quality_scales_the_voice_evidence_down_never_up():
    params = CalibrationParams.cold_start()
    full = params.log_lr(0.9, quality=1.0)
    faint = params.log_lr(0.9, quality=0.4)
    assert faint == pytest.approx(full * 0.4)
    assert params.log_lr(0.9, quality=5.0) == pytest.approx(full)


def test_the_old_constant_is_now_one_point_on_the_curve():
    """0.75 used to be a policy threshold. It is now just a point where the
    curve is positive - nothing special happens there."""
    params = CalibrationParams.cold_start()
    just_below = params.log_lr(0.74)
    just_above = params.log_lr(0.76)
    assert 0 < just_below < 3.0
    assert 0 < just_above < 3.0
    assert just_above > just_below


# --- fitting ----------------------------------------------------------------


def two_voices(seed=3, same_n=60, diff_n=60):
    r = random.Random(seed)
    same = [min(1.0, 0.85 + r.gauss(0, 0.04)) for _ in range(same_n)]
    different = [max(-1.0, 0.25 + r.gauss(0, 0.10)) for _ in range(diff_n)]
    return same, different


def test_silverman_bandwidth_scales_with_spread():
    tight = silverman_bandwidth([0.9, 0.901, 0.899, 0.9] * 10)
    loose = silverman_bandwidth([0.1, 0.5, 0.9, 0.3] * 10)
    assert tight < loose
    assert silverman_bandwidth([0.9]) > 0  # floored, never zero


def test_fitting_needs_enough_labelled_pairs():
    few = fit_score_to_llr([0.9] * 5, [0.2] * 5, min_samples=30)
    assert few.fitted is False
    enough = fit_score_to_llr(*two_voices(), min_samples=30)
    assert enough.fitted is True


def test_a_fitted_curve_separates_the_two_populations():
    params = fit_score_to_llr(*two_voices(), min_samples=30)
    assert params.log_lr(0.85) > 0
    assert params.log_lr(0.25) < 0
    # monotone in the region that matters (on the raw curve: the clip flattens
    # the far tail by design)
    assert params.raw_log_lr(0.9) > params.raw_log_lr(0.8) > params.raw_log_lr(0.5)


def test_a_fitted_curve_is_a_better_yardstick_than_the_global_constant():
    """The point of fitting: two campaigns with different microphone setups get
    different curves, instead of sharing relabel_merge_threshold = 0.5."""
    quiet = fit_score_to_llr(
        [0.72 + random.Random(1).gauss(0, 0.02) for _ in range(60)],
        [0.20 + random.Random(2).gauss(0, 0.05) for _ in range(60)],
    )
    loud = fit_score_to_llr(*two_voices(seed=9))
    assert quiet.log_lr(0.70) != pytest.approx(loud.log_lr(0.70), abs=1e-6)


def test_params_round_trip_through_json():
    params = fit_score_to_llr(*two_voices(), min_samples=30)
    restored = CalibrationParams.from_params(params.as_params())
    assert restored.kind == params.kind
    # as_params() rounds for storage, so compare at storage precision
    assert restored.bandwidth == pytest.approx(params.bandwidth, abs=1e-6)
    assert restored.log_lr(0.8) == pytest.approx(params.log_lr(0.8))


def test_unknown_params_fall_back_to_cold_start():
    restored = CalibrationParams.from_params({})
    assert restored.fitted is False
    assert restored.slope == pytest.approx(COLD_START_SLOPE)


# --- channel weights --------------------------------------------------------


def example(channel_lrs, chosen, prior=None):
    return AnswerExample(log_lr=channel_lrs, chosen=chosen, prior=prior or {A: 0.0, B: 0.0})


def test_channel_weights_are_fitted_upward_for_a_useful_channel():
    """Voice separates the answers perfectly; narration is noise. The fit should
    keep voice high and pull narration toward zero."""
    rng = random.Random(4)
    examples = []
    for _ in range(40):
        chosen = A if rng.random() < 0.5 else B
        other = B if chosen == A else A
        voice = {chosen: 2.5 + rng.gauss(0, 0.3), other: -2.5}
        noise = {chosen: rng.gauss(0, 1.0), other: rng.gauss(0, 1.0)}
        examples.append(example({"voice": voice, "narration": noise}, chosen))
    weights = fit_channel_weights(examples, learning_rate=0.5, steps=200)
    assert weights["voice"] > weights["narration"]
    assert weights["voice"] > 1.0


def test_weights_are_clamped_into_a_sane_range():
    examples = [
        example({"voice": {A: 50.0, B: -50.0}}, A) for _ in range(10)
    ]
    weights = fit_channel_weights(examples, learning_rate=1.0, steps=500)
    assert MIN_CHANNEL_WEIGHT <= weights["voice"] <= MAX_CHANNEL_WEIGHT


def test_no_examples_keeps_the_defaults():
    weights = fit_channel_weights([])
    assert all(value == 1.0 for value in weights.values())


# --- the bundle -------------------------------------------------------------


def test_calibration_delegates_to_its_params():
    calibration = Calibration()
    assert calibration.fitted is False
    assert calibration.log_lr(0.9) > 0
    assert calibration.channel_weight("narration") == 1.0


def test_calibration_round_trips_through_db_params():
    calibration = Calibration(
        voice=CalibrationParams.cold_start(),
        weights={"narration": 0.7, "voice": 1.4},
        n_labeled=120,
        false_confident_rate=0.01,
    )
    restored = Calibration.from_params(calibration.as_params())
    assert restored.weights == {"narration": 0.7, "voice": 1.4}
    assert restored.n_labeled == 120
    assert restored.false_confident_rate == pytest.approx(0.01)
    assert restored.log_lr(0.8) == pytest.approx(calibration.log_lr(0.8))


def test_tightening_shrinks_the_confidence_the_same_cosine_buys():
    calibration = Calibration(weights={"voice": 1.0})
    strict = calibration.tightened(factor=0.5)
    assert strict.log_lr(0.9) == pytest.approx(calibration.log_lr(0.9) * 0.5)
    assert strict.weights["voice"] == pytest.approx(0.5)
    assert strict.false_confident_rate == calibration.false_confident_rate


def test_a_fitted_calibration_reports_itself_as_fitted():
    assert Calibration(voice=fit_score_to_llr(*two_voices())).fitted is True
    assert Calibration(weights={"voice": 1.2}).fitted is True
