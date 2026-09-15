"""Status rule tests: the safety property of the whole design."""

import pytest

from app.statuses import (
    ATTRIBUTION_STATUSES,
    CONFIDENT_STATUSES,
    StatusThresholds,
    at_least,
    classify,
    coverage,
    is_confident,
    rank,
    unresolved_stakes,
)


def verdict(**kw):
    base = {"p_max": 0.95, "margin": 0.7, "entropy": 0.2}
    base.update(kw)
    return classify(**base)


def test_status_order_is_the_monotonicity_rule():
    assert ATTRIBUTION_STATUSES == (
        "unresolved",
        "auto_low",
        "auto_high",
        "propagated",
        "user_confirmed",
    )
    assert rank("auto_high") > rank("auto_low") > rank("unresolved")
    assert rank("nonsense") == -1
    assert at_least("propagated", "auto_high")
    assert not at_least("auto_low", "auto_high")


def test_only_three_statuses_may_reach_a_character_page():
    assert CONFIDENT_STATUSES == {"user_confirmed", "auto_high", "propagated"}
    assert not is_confident("auto_low")
    assert not is_confident("unresolved")


def test_an_answer_is_always_user_confirmed():
    assert verdict(answered=True, p_max=0.4, margin=0.05).status == "user_confirmed"


def test_a_lone_voice_channel_can_never_be_high_confidence():
    """This is the rule that keeps silent errors out of the wiki: today a bare
    cosine of 0.76 auto-assigns a name."""
    weak_voice = verdict(voice_lr=2.9)
    assert weak_voice.status == "auto_low"
    strong_voice = verdict(voice_lr=3.0)
    assert strong_voice.status == "auto_high"
    assert strong_voice.corroborated_by == "voice"


def test_a_second_channel_corroborates_at_a_much_lower_bar():
    corroborated = verdict(voice_lr=0.0, corroborating_channel="self_name")
    assert corroborated.status == "auto_high"
    assert corroborated.corroborated_by == "self_name"


def test_high_probability_without_a_wide_margin_stays_low():
    assert verdict(p_max=0.92, margin=0.30, voice_lr=5.0).status == "auto_low"


def test_a_narrow_margin_without_high_probability_stays_low():
    assert verdict(p_max=0.70, margin=0.60, voice_lr=5.0).status == "auto_low"


def test_below_the_low_floor_the_utterance_is_unresolved():
    assert verdict(p_max=0.45, margin=0.10, voice_lr=0.0).status == "unresolved"


def test_propagated_demands_stricter_numbers_than_auto_high():
    """A propagated attribution generalises from an answer instead of observing
    the moment, so it must clear a higher bar - and if it is wrong, the blast
    radius is the rule, not the DM."""
    as_observed = verdict(p_max=0.96, margin=0.60, voice_lr=4.0)
    as_propagated = verdict(p_max=0.96, margin=0.60, voice_lr=4.0, propagated=True)
    assert as_observed.status == "auto_high"
    # ... and it does NOT fall back to auto_high: auto_high claims the moment
    # was OBSERVED, this one was inferred
    assert as_propagated.status == "auto_low"

    strong = verdict(p_max=0.97, margin=0.80, propagated=True)
    assert strong.status == "propagated"


def test_a_weak_propagation_can_fall_all_the_way_to_unresolved():
    assert verdict(p_max=0.4, margin=0.1, propagated=True).status == "unresolved"


def test_propagated_loses_to_an_answer():
    assert verdict(answered=True, propagated=True, p_max=0.99, margin=0.9).status == (
        "user_confirmed"
    )


def test_thresholds_are_configurable_and_used():
    strict = StatusThresholds(auto_high_pmin=0.99)
    assert classify(
        p_max=0.95, margin=0.7, entropy=0.1, voice_lr=5.0, thresholds=strict
    ).status == "auto_low"


def test_verdict_reports_the_decision_provenance():
    v = verdict(answered=True, decided_by="question:abc")
    assert v.decided_by == "question:abc"
    assert v.confident is True


# --- coverage and the stopping input ---------------------------------------


def test_coverage_is_stakes_weighted_speech_not_label_counts():
    rows = [
        {"status": "auto_high", "stakes": 1.0, "speech_sec": 100.0},
        {"status": "unresolved", "stakes": 1.0, "speech_sec": 100.0},
    ]
    assert coverage(rows) == pytest.approx(0.5)


def test_coverage_weights_a_high_stakes_minute_over_a_low_stakes_hour():
    rows = [
        {"status": "auto_high", "stakes": 1.0, "speech_sec": 6.0},
        {"status": "unresolved", "stakes": 0.1, "speech_sec": 60.0},
    ]
    assert coverage(rows) > 0.45


def test_coverage_of_an_empty_session_is_one():
    assert coverage([]) == 1.0


def test_auto_low_does_not_count_as_covered():
    rows = [{"status": "auto_low", "stakes": 1.0, "speech_sec": 10.0}]
    assert coverage(rows) == 0.0


def test_unresolved_stakes_measures_the_open_mass():
    rows = [
        {"status": "unresolved", "stakes": 0.9},
        {"status": "auto_high", "stakes": 0.1},
    ]
    assert unresolved_stakes(rows) == pytest.approx(0.9)
    assert unresolved_stakes([]) == 0.0


def test_a_moment_the_engine_only_leans_towards_is_still_open():
    """auto_low is NOT confident: the wiki gate refuses it, so it is as
    unattributed as 'unresolved' is.

    Counting only the lowest status let a session where everything was auto_low
    report zero open stakes - the review then declared itself finished and asked
    the DM nothing, with coverage saying three quarters of it was unattributed.
    """
    rows = [
        {"status": "auto_low", "stakes": 0.8},
        {"status": "auto_high", "stakes": 0.2},
    ]
    assert unresolved_stakes(rows) == pytest.approx(0.8)


def test_unresolved_stakes_is_the_complement_of_coverage_when_seconds_are_equal():
    rows = [
        {"status": "auto_high", "stakes": 0.5, "speech_sec": 1.0},
        {"status": "auto_low", "stakes": 0.5, "speech_sec": 1.0},
    ]
    assert coverage(rows) == pytest.approx(0.5)
    assert unresolved_stakes(rows) == pytest.approx(0.5)
