"""Pure speaker re-clustering logic (no ML/IO deps) - testable without torch.

The raw diarizer labels are not trusted (OpenAI's per-chunk labels restart at
"Speaker A" on every chunk; pyannote's per-chunk labels restart at SPEAKER_00).
This module overrides them with labels derived from *our own* embeddings:

1. 'speaker_turns' groups consecutive same-raw-label segments into speaker
   turns. Chunk boundaries are hard turn boundaries (raw labels restart per
   chunk), so a same-label run never spans two chunks.
2. 'upgma_cluster' clusters turn embeddings with average-linkage agglomerative
   clustering (UPGMA) on cosine distance, either to a target K (the diarizer's
   speaker count) or by a cosine-distance threshold (robust when K is unknown
   - the default).
3. 'relabel_segments' relabels every segment with a canonical SPEAKER_XX label
   in order of first appearance, optionally snapping clusters to campaign
   enrollment anchors (per-user centroids) so known voices are pulled together
   and get a name for free.

Everything here is deterministic, dependency-free Python so the clustering is
unit-tested in the dev venv (no torch/speechbrain/qdrant).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from dnd_common.transcript import GAP_TOLERANCE_SEC, Turn, speaker_turns

# Re-exported for callers that used to import them from app.relabel.
__all__ = ["GAP_TOLERANCE_SEC", "Turn", "speaker_turns"]

# Canonical label scheme (matches the on-prem pyannote convention downstream
# consumers/tests already expect).
LABEL_FORMAT = "SPEAKER_%02d"


@dataclass
class RelabelResult:
    """Everything the worker needs after re-clustering."""

    segments: list[dict[str, Any]]            # diarization segments, labels overridden
    labels_by_index: list[str | None]         # new label per input segment index (None = untouched)
    labels: list[str]                         # new labels in first-appearance order
    label_embeddings: dict[str, list[float]]  # label -> unit centroid (embedded turns only)
    label_durations: dict[str, float]         # label -> total embedded turn duration (s)
    anchors: dict[str, tuple[str, float]]     # label -> (user_id, cosine) snapped to enrollment
    stats: dict[str, Any]


def _cosine(a: list[float], b: list[float]) -> float:
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


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    if n == 0.0:
        return v
    return [x / n for x in v]


def centroid(vectors: list[list[float]]) -> list[float]:
    """Unit-norm mean of a set of embeddings."""
    if not vectors:
        return []
    d = len(vectors[0])
    s = [0.0] * d
    for v in vectors:
        for i in range(d):
            s[i] += v[i]
    return _unit([x / len(vectors) for x in s])


def upgma_cluster(
    vectors: list[list[float]],
    *,
    k: int | None = None,
    threshold: float | None = None,
) -> list[int]:
    """Average-linkage agglomerative clustering (UPGMA) on cosine distance.

    'k': stop once this many clusters remain (the diarizer's speaker count).
    'threshold': cosine distance; stop once the closest pair is farther than
    this (robust when K is unknown). Exactly one of the two must be given.

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

    # cosine distance matrix
    dist = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = 1.0 - max(-1.0, min(1.0, _cosine(vectors[i], vectors[j])))
            dist[i][j] = dist[j][i] = d

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


def relabel_segments(
    segments: list[dict[str, Any]],
    turns: list[Turn],
    turn_embeddings: list[list[float] | None],
    *,
    threshold: float = 0.5,
    k: int | None = None,
    anchors: list[dict[str, Any]] | None = None,
    anchor_threshold: float = 0.6,
    label_format: str = LABEL_FORMAT,
) -> RelabelResult:
    """Cluster turns by voice embedding and override each segment's label.

    'turn_embeddings' is aligned to 'turns'; a None entry marks a turn the
    worker chose not to (or could not) embed - those turns inherit the label
    of their temporally nearest embedded turn.

    'anchors' is an optional list of {"user_id", "embedding"} (one unit vector
    per enrolled campaign member). Clusters whose centroid is within
    'anchor_threshold' cosine of an anchor are snapped to that user - this both
    identifies known speakers and prevents a known voice from being split.
    """
    n_seg = len(segments)
    n_turns = len(turns)
    if len(turn_embeddings) != n_turns:
        raise ValueError("turn_embeddings must be aligned to turns")

    embedded = [ti for ti, emb in enumerate(turn_embeddings) if emb is not None]
    cluster_of_turn: dict[int, int] = {}
    n_clusters = 0
    if embedded:
        vectors = [turn_embeddings[ti] for ti in embedded]  # type: ignore[list-item]
        cluster_ids = upgma_cluster(vectors, k=k, threshold=threshold)
        for ti, cid in zip(embedded, cluster_ids):
            cluster_of_turn[ti] = cid
        n_clusters = max(cluster_ids) + 1

    cluster_members: dict[int, list[int]] = {cid: [] for cid in range(n_clusters)}
    for ti, cid in cluster_of_turn.items():
        cluster_members[cid].append(ti)

    # snap clusters to enrollment anchors (best cosine >= anchor_threshold)
    snap: dict[int, tuple[str, float]] = {}
    if anchors and n_clusters:
        for cid, tis in cluster_members.items():
            cent = centroid([turn_embeddings[ti] for ti in tis])  # type: ignore[list-item]
            best: tuple[str, float] | None = None
            for a in anchors:
                s = _cosine(cent, a["embedding"])
                if s >= anchor_threshold and (best is None or s > best[1]):
                    best = (str(a["user_id"]), s)
            if best is not None:
                snap[cid] = best

    def _first(cid: int) -> int:
        return min(turns[ti].indices[0] for ti in cluster_members[cid])

    ordered = sorted(cluster_members.keys(), key=_first)
    cluster_label = {cid: label_format % i for i, cid in enumerate(ordered)}

    turn_label: dict[int, str | None] = {
        ti: cluster_label[cid] for ti, cid in cluster_of_turn.items()
    }
    for ti in range(n_turns):
        if ti in cluster_of_turn:
            continue
        if not embedded:
            turn_label[ti] = None  # nothing to inherit from; leave raw label
            continue
        mid = (turns[ti].start + turns[ti].end) / 2
        nearest = min(
            embedded, key=lambda e: abs((turns[e].start + turns[e].end) / 2 - mid)
        )
        turn_label[ti] = turn_label[nearest]

    labels_by_index: list[str | None] = [None] * n_seg
    new_segments = [dict(seg) for seg in segments]
    for ti, turn in enumerate(turns):
        lbl = turn_label[ti]
        for si in turn.indices:
            labels_by_index[si] = lbl
            if lbl is not None:
                new_segments[si]["speaker"] = lbl

    label_embeddings: dict[str, list[float]] = {}
    label_durations: dict[str, float] = {}
    for cid, tis in cluster_members.items():
        lbl = cluster_label[cid]
        label_embeddings[lbl] = centroid([turn_embeddings[ti] for ti in tis])  # type: ignore[list-item]
        label_durations[lbl] = sum(turns[ti].end - turns[ti].start for ti in tis)

    anchor_map = {cluster_label[cid]: snap[cid] for cid in snap}

    return RelabelResult(
        segments=new_segments,
        labels_by_index=labels_by_index,
        labels=[cluster_label[cid] for cid in ordered],
        label_embeddings=label_embeddings,
        label_durations=label_durations,
        anchors=anchor_map,
        stats={
            "n_segments": n_seg,
            "n_turns": n_turns,
            "n_embedded": len(embedded),
            "n_clusters": n_clusters,
            "n_anchored": len(snap),
            "mode": "k" if k is not None else "threshold",
        },
    )