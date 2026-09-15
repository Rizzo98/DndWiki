"""Vectorised clustering for the attribution engine.

`dnd_common.clustering` is the REFERENCE implementation: pure Python, no
dependencies, and the one whose merge semantics are pinned by tests. It is what
speaker-service uses and what the small cases here use too.

This module adds the fast path the engine needs. A four-hour session produces
hundreds to thousands of observations, and the reference UPGMA is O(n^3) in pure
Python: fine for 50 turns, hopeless for 2000. The fast path computes the same
thing with NumPy and is pinned against the reference by an equivalence test
(tests/test_clustering.py), so the two cannot drift apart - the merge rule lives
in exactly one place conceptually, and the optimised version has to prove it
agrees.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from dnd_common.clustering import upgma_cluster as reference_upgma

Vector = Sequence[float]

#: Above this many points the NumPy path is used unconditionally.
DEFAULT_FAST_THRESHOLD = 24


def as_matrix(vectors: Sequence[Vector]) -> np.ndarray:
    """Unit-normalised (n, d) float64 matrix (zero rows are left as zeros)."""
    matrix = np.asarray(vectors, dtype=np.float64)
    if matrix.ndim != 2:
        raise ValueError("vectors must be a 2-D sequence")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    np.divide(matrix, norms, out=matrix, where=norms > 0)
    return matrix


def cosine_similarity_matrix(vectors: Sequence[Vector]) -> np.ndarray:
    """Full symmetric cosine SIMILARITY matrix (diagonal = 1 for unit rows)."""
    matrix = as_matrix(vectors)
    return matrix @ matrix.T


def distance_matrix(vectors: Sequence[Vector]) -> np.ndarray:
    """Full symmetric cosine DISTANCE matrix, clipped to [0, 2]."""
    similarity = np.clip(cosine_similarity_matrix(vectors), -1.0, 1.0)
    distances = 1.0 - similarity
    np.fill_diagonal(distances, 0.0)
    return distances


def upgma_cluster(
    vectors: Sequence[Vector],
    *,
    k: int | None = None,
    threshold: float | None = None,
    fast_threshold: int = DEFAULT_FAST_THRESHOLD,
) -> list[int]:
    """Average-linkage agglomerative clustering on cosine distance.

    Same contract as `dnd_common.clustering.upgma_cluster` (k or threshold,
    ids in first-appearance order) and, above 'fast_threshold' points, the same
    result by a vectorised route.
    """
    n = len(vectors)
    if n <= fast_threshold:
        return reference_upgma(vectors, k=k, threshold=threshold)
    if k is None and threshold is None:
        raise ValueError("upgma_cluster requires at least one of k or threshold")

    dist = distance_matrix(vectors)
    # The diagonal must never win the argmin. distance_matrix() reports a true
    # 0.0 self-distance (useful to callers), and leaving it in would make every
    # cluster "closest to itself" and merge it with itself n times - which is
    # exactly the bug the equivalence test against the reference exists to
    # catch.
    np.fill_diagonal(dist, np.inf)
    sizes = np.ones(n, dtype=np.float64)
    members: list[list[int]] = [[i] for i in range(n)]
    active = np.ones(n, dtype=bool)

    while int(active.sum()) > 1:
        if k is not None and int(active.sum()) <= k:
            break
        masked = np.where(active[:, None] & active[None, :], dist, np.inf)
        flat = int(np.argmin(masked))
        i, j = divmod(flat, n)
        best = float(masked[i, j])
        if not np.isfinite(best):
            break
        if threshold is not None and best > threshold:
            break
        size_i, size_j = sizes[i], sizes[j]
        merged = (size_i * dist[i] + size_j * dist[j]) / (size_i + size_j)
        dist[i] = merged
        dist[:, i] = merged
        dist[i, i] = np.inf
        sizes[i] = size_i + size_j
        members[i].extend(members[j])
        active[j] = False
        dist[j, :] = np.inf
        dist[:, j] = np.inf

    labels = [0] * n
    order = sorted(
        (index for index in range(n) if active[index]),
        key=lambda index: min(members[index]),
    )
    for cluster_id, index in enumerate(order):
        for member in members[index]:
            labels[member] = cluster_id
    return labels


def nearest_pairs(
    vectors: Sequence[Vector], k: int, *, limit_pairs: int | None = None
) -> list[tuple[int, int, float]]:
    """The k nearest neighbours of every point, as (i, j, similarity) triples.

    Used to build the `same_voice` edges of the belief-propagation graph: the
    design connects each utterance to its k nearest neighbours in embedding
    space rather than to all pairs, which is what keeps inference linear-ish on
    a real session (docs/attribution-model.md S4.4).
    """
    n = len(vectors)
    if n < 2 or k <= 0:
        return []
    similarity = cosine_similarity_matrix(vectors)
    np.fill_diagonal(similarity, -np.inf)
    k = min(k, n - 1)
    neighbours = np.argpartition(-similarity, k - 1, axis=1)[:, :k]
    pairs: dict[tuple[int, int], float] = {}
    for i in range(n):
        for j in neighbours[i]:
            j = int(j)
            if i == j:
                continue
            key = (min(i, j), max(i, j))
            score = float(similarity[i, j])
            if key not in pairs or score > pairs[key]:
                pairs[key] = score
    ordered = sorted(pairs.items(), key=lambda item: (-item[1], item[0]))
    if limit_pairs is not None:
        ordered = ordered[:limit_pairs]
    return [(i, j, score) for (i, j), score in ordered]


def cross_identity_pairs(
    vectors: Sequence[Vector],
    identity_of: Sequence[int | None],
    *,
    min_similarity: float,
    limit_pairs: int | None = None,
) -> list[tuple[int, int, float]]:
    """Pairs of observations in DIFFERENT identities that look like one voice.

    These edges are how a MERGE is discovered (S4.3): two clusters the diarizer
    kept apart whose observations are mutually more similar than two samples of
    one speaker normally are.
    """
    n = len(vectors)
    if n < 2:
        return []
    similarity = cosine_similarity_matrix(vectors)
    pairs: list[tuple[int, int, float]] = []
    for i in range(n):
        for j in range(i + 1, n):
            if identity_of[i] is None or identity_of[j] is None:
                continue
            if identity_of[i] == identity_of[j]:
                continue
            score = float(similarity[i, j])
            if score >= min_similarity:
                pairs.append((i, j, score))
    pairs.sort(key=lambda item: (-item[2], item[0], item[1]))
    if limit_pairs is not None:
        pairs = pairs[:limit_pairs]
    return pairs
