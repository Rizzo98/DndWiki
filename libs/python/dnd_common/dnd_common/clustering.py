"""Pure clustering primitives shared by the speaker and attribution services.

This module is deliberately dependency-free (standard library only) so the
clustering policy is unit-testable in the dev venv without torch, numpy or
Qdrant. It is the *reference implementation*: the attribution engine adds a
vectorised fast path for large inputs and pins it against these functions with
an equivalence test.

Everything here operates on plain lists of floats. Vectors coming from
ECAPA-TDNN are 192-d; nothing in this module assumes a dimension.

Moved out of speaker-service app/relabel.py so both services share one
implementation of the merge semantics (see docs/attribution-model.md S12.5).
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence

Vector = Sequence[float]

__all__ = [
    "adjusted_rand_index",
    "centroid",
    "cosine",
    "mean_intra_cosine",
    "pairwise_cosine_distance",
    "spherical_kmeans",
    "unit",
    "upgma_cluster",
]


def cosine(a: Vector, b: Vector) -> float:
    """Cosine similarity of two (ideally unit) vectors."""
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def unit(v: Vector) -> list[float]:
    """Scale a vector to unit L2 norm (a zero vector is returned unchanged)."""
    n = math.sqrt(sum(x * x for x in v))
    if n == 0.0:
        return list(v)
    return [x / n for x in v]


def centroid(vectors: Sequence[Vector]) -> list[float]:
    """Unit-norm mean of a set of embeddings (empty input -> empty list)."""
    if not vectors:
        return []
    d = len(vectors[0])
    s = [0.0] * d
    for v in vectors:
        for i in range(d):
            s[i] += v[i]
    return unit([x / len(vectors) for x in s])


def pairwise_cosine_distance(vectors: Sequence[Vector]) -> list[list[float]]:
    """Full symmetric matrix of cosine distances (1 - cosine, clipped to [0, 2])."""
    n = len(vectors)
    dist = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = 1.0 - max(-1.0, min(1.0, cosine(vectors[i], vectors[j])))
            dist[i][j] = dist[j][i] = d
    return dist


def upgma_cluster(
    vectors: Sequence[Vector],
    *,
    k: int | None = None,
    threshold: float | None = None,
    distance_matrix: list[list[float]] | None = None,
) -> list[int]:
    """Average-linkage agglomerative clustering (UPGMA) on cosine distance.

    'k': stop once this many clusters remain (the diarizer's speaker count).
    'threshold': cosine distance; stop once the closest pair is farther than
    this (robust when K is unknown). Exactly one of the two must be given.

    'distance_matrix' lets a caller supply a precomputed (or vectorised) matrix;
    it must be square, symmetric and aligned to 'vectors'.

    Returns a list of cluster ids (0..m-1) aligned to 'vectors', numbered in
    first-appearance order.
    """
    n = len(vectors)
    if n == 0:
        return []
    if n == 1:
        return [0]
    if k is None and threshold is None:
        raise ValueError("upgma_cluster requires at least one of k or threshold")

    dist = distance_matrix if distance_matrix is not None else pairwise_cosine_distance(vectors)
    if len(dist) != n or any(len(row) != n for row in dist):
        raise ValueError("distance_matrix must be square and aligned to vectors")

    sizes = [1] * n
    members = [[i] for i in range(n)]
    active = set(range(n))

    while len(active) > 1:
        if k is not None and len(active) <= k:
            break
        # find the closest active pair
        best: float | None = None
        pair: tuple[int, int] | None = None
        ordered = sorted(active)
        for x in range(len(ordered)):
            for y in range(x + 1, len(ordered)):
                i, j = ordered[x], ordered[y]
                d = dist[i][j]
                if best is None or d < best:
                    best = d
                    pair = (i, j)
        if threshold is not None and best is not None and best > threshold:
            break
        assert pair is not None
        i, j = pair
        new_size = sizes[i] + sizes[j]
        # UPGMA (average linkage) Lance-Williams update: merge j into i
        for m in active:
            if m == i or m == j:
                continue
            dist[i][m] = dist[m][i] = (
                sizes[i] * dist[i][m] + sizes[j] * dist[j][m]
            ) / new_size
        members[i].extend(members[j])
        sizes[i] = new_size
        active.remove(j)

    labels = [0] * n
    for cid, c in enumerate(sorted(active, key=lambda idx: min(members[idx]))):
        for m in members[c]:
            labels[m] = cid
    return labels


def mean_intra_cosine(vectors: Sequence[Vector], labels: Sequence[int]) -> dict[int, float]:
    """Mean pairwise cosine similarity *inside* each cluster (per cluster id).

    A cluster with fewer than two members has no pair to average and is omitted:
    the caller (the purity test) treats a missing cluster as undefined rather
    than as zero.
    """
    groups: dict[int, list[int]] = {}
    for index, label in enumerate(labels):
        groups.setdefault(label, []).append(index)
    out: dict[int, float] = {}
    for label, members in groups.items():
        if len(members) < 2:
            continue
        total = 0.0
        pairs = 0
        for a in range(len(members)):
            for b in range(a + 1, len(members)):
                total += cosine(vectors[members[a]], vectors[members[b]])
                pairs += 1
        out[label] = total / pairs
    return out


def spherical_kmeans(
    vectors: Sequence[Vector],
    k: int,
    *,
    restarts: int = 10,
    max_iter: int = 100,
    seed: int = 0,
) -> list[int]:
    """Spherical k-means (cosine k-means) with deterministic restarts.

    Every vector is normalised internally, centroids are re-normalised after
    each update, and assignment maximises cosine similarity. The best restart by
    total within-cluster cosine is kept. Ties are broken by the seed order, so
    the result is fully deterministic for a given input.

    Returns cluster ids aligned to 'vectors' (0..k-1). Fewer than k distinct
    points yields fewer, possibly empty, clusters; empty clusters keep their id
    and simply have no members.
    """
    n = len(vectors)
    if n == 0:
        return []
    if k <= 1:
        return [0] * n
    points = [unit(v) for v in vectors]
    k = min(k, n)
    rng = random.Random(seed)

    best_labels: list[int] | None = None
    best_score = float("-inf")
    for restart in range(max(1, restarts)):
        if restart == 0:
            # deterministic first restart: spread seeds over the input order
            starts = [points[(i * n) // k] for i in range(k)]
        else:
            starts = [points[rng.randrange(n)] for _ in range(k)]
        centroids_now = [list(p) for p in starts]
        labels = [0] * n
        for _ in range(max_iter):
            changed = False
            for i, p in enumerate(points):
                best_c = 0
                best_s = float("-inf")
                for c, cent in enumerate(centroids_now):
                    s = cosine(p, cent)
                    if s > best_s:
                        best_s = s
                        best_c = c
                if labels[i] != best_c:
                    labels[i] = best_c
                    changed = True
            sums = [[0.0] * len(points[0]) for _ in range(k)]
            counts = [0] * k
            for i, p in enumerate(points):
                c = labels[i]
                counts[c] += 1
                for d in range(len(p)):
                    sums[c][d] += p[d]
            for c in range(k):
                if counts[c]:
                    centroids_now[c] = unit(sums[c])
            if not changed:
                break
        score = 0.0
        for i, p in enumerate(points):
            score += cosine(p, centroids_now[labels[i]])
        if score > best_score:
            best_score = score
            best_labels = list(labels)
    return best_labels or [0] * n


def adjusted_rand_index(a: Sequence[int], b: Sequence[int]) -> float:
    """Adjusted Rand Index of two partitions (1.0 = identical, ~0 = chance).

    Used by the purity bootstrap: a split is only trusted when the same
    partition recurs across resamples.
    """
    if len(a) != len(b):
        raise ValueError("partitions must have the same length")
    n = len(a)
    if n < 2:
        return 1.0
    cont: dict[tuple[int, int], int] = {}
    row: dict[int, int] = {}
    col: dict[int, int] = {}
    for x, y in zip(a, b):
        cont[(x, y)] = cont.get((x, y), 0) + 1
        row[x] = row.get(x, 0) + 1
        col[y] = col.get(y, 0) + 1

    def comb2(v: int) -> float:
        return v * (v - 1) / 2.0

    sum_ij = sum(comb2(v) for v in cont.values())
    sum_i = sum(comb2(v) for v in row.values())
    sum_j = sum(comb2(v) for v in col.values())
    total = comb2(n)
    if total == 0.0:
        return 1.0
    expected = sum_i * sum_j / total
    maximum = (sum_i + sum_j) / 2.0
    if maximum == expected:
        return 1.0
    return (sum_ij - expected) / (maximum - expected)
