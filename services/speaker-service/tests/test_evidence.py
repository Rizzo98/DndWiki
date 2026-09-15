"""Voice evidence shaping tests: multi-centroid scoring, no verdict leakage."""

from types import SimpleNamespace

import pytest

from app.evidence import (
    CentroidHit,
    aggregate_candidate_scores,
    evidence_payload,
    hits_from_points,
    observation_evidence,
    select_nearest,
)


def hit(member=None, user=None, cos=0.8, centroid="c1", **kw):
    return CentroidHit(
        centroid_id=centroid, cosine=cos, member_id=member, user_id=user, **kw
    )


def point(pid, score, **payload):
    return SimpleNamespace(id=pid, score=score, payload=payload)


# --- candidate keys ---------------------------------------------------------


def test_member_key_wins_over_user_key():
    assert hit(member="m1", user="u1").candidate_key == "member:m1"


def test_legacy_print_with_only_a_user_is_still_a_candidate():
    assert hit(user="u1").candidate_key == "user:u1"


def test_a_print_that_identifies_nobody_has_no_key():
    assert hit().candidate_key is None


# --- Qdrant point conversion ------------------------------------------------


def test_hits_from_points_reads_old_and_new_payloads():
    hits = hits_from_points(
        [
            point("a", 0.9, user_id="u1", source="enrollment"),
            point("b", 0.7, member_id="m2", user_id="u2", source="session", quality=0.8),
        ],
        centroid_prefix="voiceprint",
    )
    assert hits[0].candidate_key == "user:u1"
    assert hits[1].candidate_key == "member:m2"
    assert hits[1].quality == 0.8
    assert hits[0].source == "enrollment"
    assert hits[0].centroid_id == "voiceprint:a"


def test_hits_from_points_skips_anonymous_hits():
    assert hits_from_points([point("a", 0.99)], centroid_prefix="x") == []


def test_hits_from_points_tolerates_missing_score():
    assert hits_from_points(
        [SimpleNamespace(id="a", score=None, payload={"user_id": "u"})], centroid_prefix="x"
    ) == []


# --- selection --------------------------------------------------------------


def test_select_nearest_orders_floors_and_caps():
    hits = [hit(member=f"m{i}", cos=c) for i, c in enumerate([0.4, 0.9, 0.6, 0.1])]
    kept = select_nearest(hits, limit=2, floor=0.3)
    assert [h.cosine for h in kept] == [0.9, 0.6]


def test_select_nearest_always_keeps_the_best_even_below_the_floor():
    kept = select_nearest([hit(member="m1", cos=0.05)], limit=8, floor=0.5)
    assert len(kept) == 1 and kept[0].cosine == 0.05


def test_select_nearest_of_nothing_is_empty():
    assert select_nearest([]) == []


# --- aggregation ------------------------------------------------------------


def test_topk_mean_scores_a_multi_voice_member_by_their_best_centroids():
    hits = [
        hit(member="m1", cos=0.9, centroid="a"),
        hit(member="m1", cos=0.8, centroid="b"),
        hit(member="m1", cos=0.1, centroid="c"),  # a bad print must not dominate
        hit(member="m2", cos=0.5, centroid="d"),
    ]
    scores = aggregate_candidate_scores(hits, top_k=2)
    assert scores["member:m1"] == pytest.approx(0.85)
    assert scores["member:m2"] == pytest.approx(0.5)


def test_top1_mode_takes_the_single_best_centroid():
    hits = [hit(member="m1", cos=0.9, centroid="a"), hit(member="m1", cos=0.2, centroid="b")]
    assert aggregate_candidate_scores(hits, mode="top1")["member:m1"] == pytest.approx(0.9)


def test_aggregation_handles_fewer_centroids_than_top_k():
    scores = aggregate_candidate_scores([hit(member="m1", cos=0.7)], top_k=3)
    assert scores["member:m1"] == pytest.approx(0.7)


def test_aggregation_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        aggregate_candidate_scores([hit(member="m1")], mode="median")


def test_aggregation_ignores_keyless_hits():
    assert aggregate_candidate_scores([hit(cos=0.99)]) == {}


def test_legacy_and_member_keys_are_scored_separately():
    scores = aggregate_candidate_scores(
        [hit(member="m1", cos=0.9, centroid="a"), hit(user="u1", cos=0.8, centroid="b")]
    )
    assert set(scores) == {"member:m1", "user:u1"}


# --- records ----------------------------------------------------------------


def test_observation_evidence_shape_and_rounding():
    record = observation_evidence(
        observation_id="SPEAKER_00#0",
        label="SPEAKER_00",
        index=0,
        start=1.23456,
        end=9.87654,
        quality=0.81234,
        hits=[hit(member="m1", cos=0.81234, quality=0.9)],
    )
    assert record["id"] == "SPEAKER_00#0"
    assert record["start"] == 1.235 and record["end"] == 9.877
    assert record["quality"] == 0.8123
    assert record["nearest_centroids"][0] == {
        "centroid_id": "c1",
        "cosine": 0.8123,
        "member_id": "m1",
        "quality": 0.9,
    }


def test_observation_evidence_with_no_hits_is_an_empty_list():
    record = observation_evidence(
        observation_id="v#0", label="V", index=0, start=0.0, end=1.0, quality=None, hits=[]
    )
    assert record["nearest_centroids"] == []
    assert record["quality"] is None


def test_evidence_payload_carries_the_model_version():
    payload = evidence_payload([{"id": "a"}], model_version="ecapa-1", embedding_version=2)
    assert payload == {
        "model_version": "ecapa-1",
        "embedding_version": 2,
        "observations": [{"id": "a"}],
    }
