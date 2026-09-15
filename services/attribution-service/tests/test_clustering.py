"""Fast clustering must agree with the pure-Python reference implementation."""

import random

import pytest
from dnd_common.clustering import upgma_cluster as reference

from app.clustering import (
    as_matrix,
    cross_identity_pairs,
    distance_matrix,
    nearest_pairs,
    upgma_cluster,
)


def unit_vector(seed, dim=16):
    r = random.Random(seed)
    v = [r.gauss(0.0, 1.0) for _ in range(dim)]
    norm = sum(x * x for x in v) ** 0.5
    return [x / norm for x in v]


def cloud(seed, count, jitter=0.05, dim=16):
    base = unit_vector(seed, dim)
    r = random.Random(seed * 31)
    return [
        [b + r.gauss(0.0, jitter) for b in base] for _ in range(count)
    ]


def mixed_cloud(groups=5, per_group=12, seed=7):
    vectors = []
    for g in range(groups):
        vectors.extend(cloud(seed + g, per_group))
    return vectors


# --- matrices ---------------------------------------------------------------


def test_as_matrix_normalises_rows():
    matrix = as_matrix([[3.0, 4.0], [0.0, 0.0]])
    assert matrix[0].tolist() == pytest.approx([0.6, 0.8])
    assert matrix[1].tolist() == [0.0, 0.0]  # a zero row stays zero


def test_distance_matrix_is_symmetric_with_a_zero_diagonal():
    d = distance_matrix(mixed_cloud(groups=2, per_group=3))
    assert (d == d.T).all()
    assert (d.diagonal() == 0.0).all()
    assert (d >= 0.0).all() and (d <= 2.0).all()


def test_as_matrix_rejects_a_flat_sequence():
    with pytest.raises(ValueError):
        as_matrix([1.0, 2.0])


# --- equivalence with the reference ----------------------------------------


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_fast_upgma_matches_the_reference_on_k(seed):
    vectors = mixed_cloud(groups=4, per_group=9, seed=seed)
    for k in (1, 2, 3, 6, 20):
        # force the fast path so the comparison is actually made
        assert upgma_cluster(vectors, k=k, fast_threshold=0) == reference(vectors, k=k)


@pytest.mark.parametrize("threshold", [0.05, 0.2, 0.5, 0.9])
def test_fast_upgma_matches_the_reference_on_threshold(threshold):
    vectors = mixed_cloud(groups=4, per_group=8, seed=11)
    assert upgma_cluster(vectors, threshold=threshold, fast_threshold=0) == reference(
        vectors, threshold=threshold
    )


def test_small_inputs_use_the_reference_directly():
    vectors = mixed_cloud(groups=2, per_group=4)
    assert upgma_cluster(vectors, k=2) == reference(vectors, k=2)


def test_upgma_requires_a_stopping_rule():
    with pytest.raises(ValueError):
        upgma_cluster(mixed_cloud(groups=2, per_group=20), fast_threshold=0)


def test_upgma_edge_cases():
    assert upgma_cluster([]) == []
    assert upgma_cluster([[1.0, 0.0]]) == [0]


def test_cluster_ids_follow_first_appearance():
    vectors = cloud(1, 5) + cloud(2, 5)
    labels = upgma_cluster(vectors, k=2, fast_threshold=0)
    assert labels[0] == 0


# --- neighbour pairs --------------------------------------------------------


def test_nearest_pairs_connect_each_point_to_its_k_closest():
    vectors = cloud(5, 10, jitter=0.02)
    pairs = nearest_pairs(vectors, k=3)
    assert pairs
    for i, j, score in pairs:
        assert i < j  # canonical order, no duplicates
        assert score > 0.9  # all one voice


def test_nearest_pairs_of_a_single_point_is_empty():
    assert nearest_pairs([[1.0, 0.0]], k=3) == []
    assert nearest_pairs([], k=3) == []


def test_nearest_pairs_respects_the_pair_limit():
    pairs = nearest_pairs(cloud(6, 12, jitter=0.05), k=4, limit_pairs=5)
    assert len(pairs) == 5


def test_nearest_pairs_prefers_the_strongest_similarity_for_a_duplicate_pair():
    vectors = [[1.0, 0.0], [0.999, 0.001], [0.0, 1.0]]
    pairs = nearest_pairs(vectors, k=2)
    keys = [(i, j) for i, j, _ in pairs]
    assert len(keys) == len(set(keys))


# --- cross-identity pairs (merge discovery) --------------------------------


def test_cross_identity_pairs_only_cross_identities():
    vectors = cloud(1, 3) + cloud(1, 3)  # same voice, two identities
    identity_of = [0, 0, 0, 1, 1, 1]
    pairs = cross_identity_pairs(vectors, identity_of, min_similarity=0.9)
    assert pairs
    for i, j, score in pairs:
        assert identity_of[i] != identity_of[j]
        assert score >= 0.9


def test_cross_identity_pairs_ignores_unassigned_observations():
    vectors = cloud(1, 2)
    assert cross_identity_pairs(vectors, [0, None], min_similarity=0.0) == []
