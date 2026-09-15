"""Purity and merge-decision tests on synthetic voice clouds (pure, no models)."""

import random

import pytest

from dnd_common.clustering import mean_intra_cosine, unit
from dnd_common.purity import (
    COLD_START_D50,
    SameVoiceBaseline,
    assess_merge,
    assess_purity,
    bootstrap_stability,
    split_shape,
)

DIM = 16


def rng_vector(seed):
    r = random.Random(seed)
    return unit([r.gauss(0.0, 1.0) for _ in range(DIM)])


def voice(seed, count, jitter=0.02):
    """A cloud of embeddings of one synthetic speaker."""
    base = rng_vector(seed)
    r = random.Random(seed * 7919)
    return [unit([b + r.gauss(0.0, jitter) for b in base]) for _ in range(count)]


def seconds(count, each=5.0):
    return [each] * count


# --- baseline ---------------------------------------------------------------


def test_cold_start_baseline_is_a_distance_yardstick():
    baseline = SameVoiceBaseline.cold_start()
    assert baseline.p50 == COLD_START_D50
    assert baseline.p95 > baseline.p50  # the tail is farther, not closer


def test_baseline_from_distances_uses_the_far_tail_for_p95():
    distances = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    baseline = SameVoiceBaseline.from_distances(distances)
    assert baseline.p50 == pytest.approx(0.55)
    assert baseline.p95 == pytest.approx(0.955)
    assert baseline.n_samples == 10


def test_baseline_from_empty_distances_falls_back_to_cold_start():
    assert SameVoiceBaseline.from_distances([]) == SameVoiceBaseline.cold_start()


# --- split geometry ---------------------------------------------------------


def test_split_shape_uses_the_worst_cluster_cohesion():
    tight = voice(1, 4, 0.01)
    mushy = voice(2, 4, 0.30)
    shape = split_shape(tight + mushy, [0] * 4 + [1] * 4)
    intra = mean_intra_cosine(tight + mushy, [0] * 4 + [1] * 4)
    # The mushy cluster's internal distance IS the reported 'within': one tight
    # cluster beside one mush is still a mush.
    assert shape.within == pytest.approx(1.0 - intra[1])
    assert shape.within > 1.0 - intra[0]
    assert shape.sep > 0.8
    assert shape.sizes == (4, 4)
    assert shape.margin == pytest.approx(shape.sep - shape.within)


def test_split_shape_of_a_degenerate_partition_is_not_evidence():
    shape = split_shape(voice(1, 4), [0, 0, 0, 0])
    assert shape.sep == 0.0 and shape.within == 1.0


# --- the purity verdict -----------------------------------------------------


def test_two_merged_voices_are_found_impure():
    vectors = voice(11, 8, 0.02) + voice(12, 8, 0.02)
    verdict = assess_purity(vectors, durations=seconds(16), resamples=10)
    assert verdict.impure is True
    assert verdict.reason == "impure"
    assert verdict.purity >= 0.8
    assert verdict.sep > verdict.within


def test_one_tight_voice_is_clean():
    verdict = assess_purity(voice(21, 16, 0.02), durations=seconds(16), resamples=10)
    assert verdict.impure is False
    assert verdict.reason == "no_margin"
    assert verdict.purity == 1.0


def test_too_few_observations_never_splits():
    verdict = assess_purity(voice(31, 4, 0.02), durations=seconds(4), resamples=10)
    assert verdict.impure is False
    assert verdict.reason == "too_few_observations"
    assert verdict.n_observations == 4


def test_a_single_outlier_is_a_lopsided_split_not_a_second_voice():
    vectors = voice(41, 6, 0.02) + voice(42, 1, 0.02)
    verdict = assess_purity(vectors, durations=seconds(7), resamples=10)
    assert verdict.impure is False
    assert verdict.reason == "lopsided_split"


def test_a_second_voice_must_carry_real_speech_seconds():
    vectors = voice(51, 8, 0.02) + voice(52, 8, 0.02)
    verdict = assess_purity(vectors, durations=seconds(16, each=1.0), resamples=10)
    assert verdict.impure is False
    assert verdict.reason == "not_enough_speech"
    assert verdict.speech_sec == pytest.approx(16.0)


def test_a_generous_baseline_suppresses_the_split():
    vectors = voice(61, 8, 0.02) + voice(62, 8, 0.02)
    permissive = SameVoiceBaseline(p50=0.1, p95=0.999)
    verdict = assess_purity(
        vectors, durations=seconds(16), baseline=permissive, resamples=10
    )
    assert verdict.impure is False
    assert verdict.reason == "within_same_voice_range"


def test_the_longest_observations_win_when_the_sample_is_capped():
    vectors = voice(71, 40, 0.02)
    durations = [0.5] * 20 + [30.0] * 20
    verdict = assess_purity(
        vectors, durations=durations, max_observations=20, resamples=5
    )
    assert verdict.n_observations == 20  # the cap, not the input size
    assert verdict.speech_sec == pytest.approx(600.0)  # only the long ones


def test_verdict_payload_is_json_friendly():
    verdict = assess_purity(voice(81, 8, 0.02), durations=seconds(8), resamples=5)
    payload = verdict.as_payload()
    assert set(payload) == {
        "purity",
        "impure",
        "sep",
        "within",
        "sizes",
        "stability",
        "reason",
        "n_observations",
    }
    assert isinstance(payload["sizes"], list)


# --- bootstrap --------------------------------------------------------------


def test_bootstrap_of_a_real_split_is_stable():
    vectors = voice(91, 10, 0.02) + voice(92, 10, 0.02)
    reference = [0] * 10 + [1] * 10
    assert bootstrap_stability(vectors, reference, resamples=10) == 1.0


def test_bootstrap_of_one_cloud_is_unstable():
    vectors = voice(93, 20, 0.02)
    reference = [0] * 10 + [1] * 10
    assert bootstrap_stability(vectors, reference, resamples=10) < 0.5


def test_bootstrap_of_a_tiny_cluster_is_trivially_stable():
    assert bootstrap_stability(voice(94, 2), [0, 1], resamples=10) == 1.0


# --- merge decisions --------------------------------------------------------


def test_merge_proposed_for_two_clusters_of_one_voice():
    verdict = assess_merge(rng_vector(101), rng_vector(101))
    assert verdict.merge is True
    assert verdict.distance == pytest.approx(0.0, abs=1e-6)


def test_merge_refused_for_two_different_voices():
    verdict = assess_merge(rng_vector(111), rng_vector(112))
    assert verdict.merge is False
    assert verdict.reason == "not_similar_enough"


def test_merge_refused_when_a_person_speaks_in_two_places_at_once():
    base = rng_vector(121)
    verdict = assess_merge(base, base, a_span=(0.0, 10.0), b_span=(5.0, 15.0))
    assert verdict.merge is False
    assert verdict.reason == "speaks_at_the_same_time"


def test_the_dm_may_be_in_two_places_at_once():
    base = rng_vector(131)
    verdict = assess_merge(
        base, base, a_is_dm=True, a_span=(0.0, 10.0), b_span=(5.0, 15.0)
    )
    assert verdict.merge is True


def test_merge_refused_on_contradicting_anchor_evidence():
    base = rng_vector(141)
    verdict = assess_merge(base, base, anchor_conflict=True)
    assert verdict.merge is False
    assert verdict.reason == "anchor_conflict"


def test_merge_refused_when_propagation_disagrees():
    base = rng_vector(151)
    verdict = assess_merge(base, base, llr_gain=-0.2)
    assert verdict.merge is False
    assert verdict.reason == "propagation_disagrees"
    assert assess_merge(base, base, llr_gain=0.2).merge is True


def test_merge_verdict_detail_is_auditable():
    verdict = assess_merge(rng_vector(161), rng_vector(162))
    assert set(verdict.detail) == {
        "distance",
        "d_same_p50",
        "overlaps",
        "anchor_conflict",
    }
