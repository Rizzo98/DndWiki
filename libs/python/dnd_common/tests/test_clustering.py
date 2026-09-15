"""Shared clustering primitive tests (pure, no numpy/torch/Qdrant needed)."""

import math

import pytest

from dnd_common.clustering import (
    adjusted_rand_index,
    centroid,
    cosine,
    mean_intra_cosine,
    pairwise_cosine_distance,
    spherical_kmeans,
    unit,
    upgma_cluster,
)


def axis(i, dim=4, scale=1.0):
    v = [0.0] * dim
    v[i] = scale
    return v


def near(i, jitter, dim=4):
    v = axis(i, dim)
    v[(i + 1) % dim] += jitter
    return v


# --- basics -----------------------------------------------------------------


def test_cosine_identical_and_orthogonal():
    assert cosine([1.0, 0.0], [2.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_zero_vector_is_zero():
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_unit_normalises_and_preserves_zero():
    assert math.sqrt(sum(x * x for x in unit([3.0, 4.0]))) == pytest.approx(1.0)
    assert unit([0.0, 0.0]) == [0.0, 0.0]


def test_centroid_is_unit_length_mean():
    c = centroid([[1.0, 0.0], [1.0, 0.0]])
    assert c == pytest.approx([1.0, 0.0])
    assert centroid([]) == []


def test_pairwise_distance_matrix_is_symmetric_with_zero_diagonal():
    d = pairwise_cosine_distance([axis(0), axis(1), axis(2)])
    assert d[0][0] == 0.0
    assert d[0][1] == pytest.approx(d[1][0])
    assert d[0][1] == pytest.approx(1.0)


# --- UPGMA ------------------------------------------------------------------


def test_upgma_requires_k_or_threshold():
    with pytest.raises(ValueError):
        upgma_cluster([axis(0), axis(1)])


def test_upgma_separates_two_well_spaced_groups():
    vectors = [axis(0), near(0, 0.05), axis(1), near(1, 0.05)]
    labels = upgma_cluster(vectors, k=2)
    assert labels[0] == labels[1]
    assert labels[2] == labels[3]
    assert labels[0] != labels[2]


def test_upgma_threshold_stops_before_merging_distant_clusters():
    labels = upgma_cluster([axis(0), near(0, 0.02), axis(1)], threshold=0.5)
    assert labels[0] == labels[1]
    assert labels[2] != labels[0]


def test_upgma_no_merge_when_threshold_is_tiny():
    labels = upgma_cluster([axis(0), axis(1), axis(2)], threshold=0.01)
    assert len(set(labels)) == 3


def test_upgma_edge_cases():
    assert upgma_cluster([]) == []
    assert upgma_cluster([axis(0)]) == [0]


def test_upgma_cluster_ids_are_first_appearance_ordered():
    labels = upgma_cluster([axis(1), axis(0), near(1, 0.01)], k=2)
    assert labels[0] == 0  # first appearance wins id 0


def test_upgma_accepts_precomputed_distance_matrix():
    vectors = [axis(0), near(0, 0.01), axis(1)]
    matrix = pairwise_cosine_distance(vectors)
    assert upgma_cluster(vectors, k=2, distance_matrix=matrix) == upgma_cluster(vectors, k=2)


def test_upgma_rejects_misaligned_matrix():
    with pytest.raises(ValueError):
        upgma_cluster([axis(0), axis(1)], k=2, distance_matrix=[[0.0]])


# --- mean intra cosine ------------------------------------------------------


def test_mean_intra_cosine_per_cluster():
    scores = mean_intra_cosine([axis(0), near(0, 0.0), axis(1)], [0, 0, 1])
    assert scores[0] == pytest.approx(1.0)
    assert 1 not in scores  # a singleton cluster has no pair to average


# --- spherical k-means ------------------------------------------------------


def test_spherical_kmeans_splits_two_obvious_groups_deterministically():
    vectors = [axis(0), near(0, 0.02), axis(1), near(1, 0.02)]
    first = spherical_kmeans(vectors, 2, restarts=5, seed=7)
    second = spherical_kmeans(vectors, 2, restarts=5, seed=7)
    assert first == second
    assert first[0] == first[1] != first[2] == first[3]


def test_spherical_kmeans_edge_cases():
    assert spherical_kmeans([], 2) == []
    assert spherical_kmeans([axis(0), axis(1)], 1) == [0, 0]


def test_spherical_kmeans_caps_k_at_point_count():
    labels = spherical_kmeans([axis(0), axis(1)], 5)
    assert len(labels) == 2
    assert set(labels) <= {0, 1}


# --- adjusted Rand index ----------------------------------------------------


def test_ari_identical_partitions_is_one():
    assert adjusted_rand_index([0, 0, 1, 1], [1, 1, 0, 0]) == pytest.approx(1.0)


def test_ari_unrelated_partition_is_near_zero():
    a = [0] * 20 + [1] * 20
    b = [i % 2 for i in range(40)]
    assert abs(adjusted_rand_index(a, b)) < 0.05


def test_ari_rejects_misaligned_and_handles_trivial():
    with pytest.raises(ValueError):
        adjusted_rand_index([0], [0, 0])
    assert adjusted_rand_index([0], [0]) == 1.0
