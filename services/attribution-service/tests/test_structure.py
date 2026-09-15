"""Split/merge tests: the engine saying 'this hypothesis is wrong'."""

import random

import pytest
from dnd_common.purity import SameVoiceBaseline

from app.structure import (
    ObservationRef,
    aba_pattern,
    anchor_conflict,
    capability_contradiction,
    confidence_collapse,
    evaluate_identity,
    evaluate_triggers,
    length_outlier,
    percentile,
    propose_merges,
    rediarize_span,
    split_boundary,
)


def unit_vector(seed, dim=12):
    r = random.Random(seed)
    v = [r.gauss(0.0, 1.0) for _ in range(dim)]
    norm = sum(x * x for x in v) ** 0.5
    return [x / norm for x in v]


def cloud(seed, count, jitter=0.02, dim=12):
    base = unit_vector(seed, dim)
    r = random.Random(seed * 17)
    return [[b + r.gauss(0.0, jitter) for b in base] for _ in range(count)]


def obs(key, start, end, **kw):
    return ObservationRef(key=key, start=start, end=end, **kw)


# --- triggers ---------------------------------------------------------------


def test_anchor_conflict_needs_two_different_confident_members():
    one = [obs("a", 0.0, 5.0, nearest=(("member:m1", 0.9),))]
    two = one + [obs("b", 5.0, 10.0, nearest=(("member:m2", 0.85),))]
    assert anchor_conflict(one) is None
    trigger = anchor_conflict(two)
    assert trigger is not None
    assert trigger.kind == "anchor_conflict"
    assert set(trigger.detail["candidates"]) == {"member:m1", "member:m2"}


def test_anchor_conflict_ignores_weak_matches():
    weak = [
        obs("a", 0.0, 5.0, nearest=(("member:m1", 0.9),)),
        obs("b", 5.0, 10.0, nearest=(("member:m2", 0.4),)),
    ]
    assert anchor_conflict(weak) is None


def test_length_outlier_fires_on_a_turn_longer_than_the_session_tail():
    # A realistic spread of turn lengths: the tail sits around 12 s.
    session = [1.0 + i * 0.11 for i in range(100)]
    trigger = length_outlier([obs("a", 0.0, 240.0)], session)
    assert trigger is not None
    assert trigger.detail["longest_sec"] == 240.0
    # the tail of a 1..12 s spread, not an exact number we should pin
    assert 10.0 < trigger.detail["session_p99_sec"] < 12.5
    assert length_outlier([obs("a", 0.0, 8.0)], session) is None


def test_aba_pattern_detects_a_label_that_changes_and_changes_back():
    observations = [
        obs("a", 0.0, 2.0, label="SPEAKER_00"),
        obs("b", 2.2, 4.0, label="SPEAKER_01"),
        obs("c", 4.1, 6.0, label="SPEAKER_00"),
    ]
    trigger = aba_pattern(observations)
    assert trigger is not None
    assert trigger.detail["labels"] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]


def test_aba_pattern_ignores_a_slow_alternation():
    observations = [
        obs("a", 0.0, 2.0, label="A"),
        obs("b", 2.2, 4.0, label="B"),
        obs("c", 30.0, 32.0, label="A"),
    ]
    assert aba_pattern(observations) is None


def test_capability_contradiction_needs_a_member_who_could_do_both():
    observations = [
        obs("a", 0.0, 2.0, capability_reqs=("spell:fireball",)),
        obs("b", 2.0, 4.0, capability_reqs=("feature:rage",)),
    ]
    impossible = {"m1": frozenset({"spell:fireball"}), "m2": frozenset({"feature:rage"})}
    possible = {
        "m1": frozenset({"spell:fireball"}),
        "m2": frozenset({"spell:fireball", "feature:rage"}),
    }
    trigger = capability_contradiction(observations, impossible)
    assert trigger is not None
    assert set(trigger.detail["requires"]) == {"spell:fireball", "feature:rage"}
    assert capability_contradiction(observations, possible) is None


def test_capability_contradiction_without_a_known_roster_is_silent():
    observations = [obs("a", 0.0, 2.0, capability_reqs=("spell:fireball",))]
    assert capability_contradiction(observations, {}) is None


def test_confidence_collapse_uses_the_session_median():
    observations = [
        obs("a", 0.0, 2.0, diar_confidence=0.95),
        obs("b", 2.0, 4.0, diar_confidence=0.30),
    ]
    trigger = confidence_collapse(observations, [0.95, 0.92, 0.94, 0.30])
    assert trigger is not None
    assert trigger.detail["flagged"] == 1
    assert confidence_collapse(observations, [0.5, 0.5]) is None


def test_triggers_are_ordered_by_weight():
    observations = [
        obs("a", 0.0, 2.0, label="A", diar_confidence=0.2, nearest=(("member:m1", 0.9),)),
        obs("b", 2.1, 4.0, label="B", diar_confidence=0.2),
        obs("c", 4.1, 6.0, label="A", diar_confidence=0.2, nearest=(("member:m2", 0.9),)),
    ]
    triggers = evaluate_triggers(observations, session_confidences=[0.9] * 5)
    weights = [t.weight for t in triggers]
    assert weights == sorted(weights, reverse=True)
    assert triggers[0].kind == "anchor_conflict"


def test_percentile_interpolates():
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.5)
    assert percentile([5.0], 0.99) == 5.0
    assert percentile([], 0.99) is None


# --- the verdict ------------------------------------------------------------


def test_a_clean_identity_is_not_split():
    vectors = cloud(1, 12, jitter=0.02)
    verdict = evaluate_identity(
        "V1",
        [f"o{i}" for i in range(12)],
        vectors=vectors,
        durations=[5.0] * 12,
        observations=[obs(f"o{i}", i * 5.0, i * 5.0 + 5.0) for i in range(12)],
    )
    assert verdict.impure is False
    assert verdict.purity == 1.0
    assert verdict.split is None
    assert verdict.needs_local_rediarization is False


def test_two_merged_voices_are_split_with_a_boundary():
    first = cloud(11, 8, jitter=0.02)
    second = cloud(12, 8, jitter=0.02)
    observations = [obs(f"a{i}", i * 6.0, i * 6.0 + 5.0) for i in range(8)] + [
        obs(f"b{i}", 100.0 + i * 6.0, 100.0 + i * 6.0 + 5.0) for i in range(8)
    ]
    verdict = evaluate_identity(
        "V2",
        [o.key for o in observations],
        vectors=first + second,
        durations=[5.0] * 16,
        observations=observations,
        baseline=SameVoiceBaseline(),
    )
    assert verdict.impure is True
    assert verdict.needs_local_rediarization is True
    left, right = verdict.split
    assert len(left) == 8 and len(right) == 8
    boundary = split_boundary(observations, verdict.split)
    assert boundary is not None
    assert 40.0 < boundary < 110.0


def test_the_verdict_payload_is_json_friendly():
    verdict = evaluate_identity(
        "V3", ["a", "b"], vectors=cloud(3, 2), durations=[5.0, 5.0]
    )
    payload = verdict.as_payload()
    assert set(payload) == {
        "handle",
        "purity",
        "impure",
        "reason",
        "triggers",
        "speech_sec",
        "n_observations",
    }


def test_rediarize_span_pads_the_identity_span():
    observations = [obs("a", 10.0, 20.0), obs("b", 30.0, 40.0)]
    assert rediarize_span(observations, padding=2.0) == (8.0, 42.0)
    assert rediarize_span([], padding=2.0) is None


def test_rediarize_span_never_goes_negative():
    assert rediarize_span([obs("a", 1.0, 20.0)], padding=5.0) == (0.0, 25.0)


def test_split_boundary_of_a_degenerate_split_is_none():
    assert split_boundary([obs("a", 0.0, 1.0)], ([0], [])) is None


# --- merges -----------------------------------------------------------------


def identity(handle, seed, **kw):
    return {"handle": handle, "centroid": unit_vector(seed), **kw}


def test_two_identities_of_one_voice_are_proposed_for_merging():
    proposals = propose_merges([identity("V1", 5), identity("V2", 5)])
    assert len(proposals) == 1
    assert proposals[0].reason == "merge"
    assert proposals[0].similarity > 0.99


def test_different_voices_are_not_merged():
    assert propose_merges([identity("V1", 5), identity("V2", 6)]) == []


def test_identities_speaking_at_once_are_not_merged():
    proposals = propose_merges(
        [
            identity("V1", 5, span=(0.0, 10.0)),
            identity("V2", 5, span=(5.0, 15.0)),
        ]
    )
    assert proposals == []


def test_the_dm_may_be_in_two_places_at_once():
    proposals = propose_merges(
        [
            identity("V1", 5, span=(0.0, 10.0), is_dm=True),
            identity("V2", 5, span=(5.0, 15.0)),
        ]
    )
    assert len(proposals) == 1


def test_contradicting_anchor_evidence_blocks_the_merge():
    proposals = propose_merges(
        [identity("V1", 5, anchor_conflict=True), identity("V2", 5)]
    )
    assert proposals == []


def test_propagation_can_veto_a_merge():
    proposals = propose_merges(
        [identity("V1", 5), identity("V2", 5)],
        llr_gain={("V1", "V2"): -0.3},
    )
    assert proposals == []
    allowed = propose_merges(
        [identity("V1", 5), identity("V2", 5)],
        llr_gain={("V1", "V2"): 0.4},
    )
    assert len(allowed) == 1


def test_merge_proposals_are_sorted_by_similarity():
    identities = [identity("V1", 5), identity("V2", 5), identity("V3", 5, centroid=None)]
    proposals = propose_merges(identities)
    assert [p.similarity for p in proposals] == sorted(
        [p.similarity for p in proposals], reverse=True
    )
