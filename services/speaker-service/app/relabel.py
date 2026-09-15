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

from dataclasses import dataclass
from typing import Any

from dnd_common.clustering import centroid, upgma_cluster
from dnd_common.clustering import cosine as _cosine
from dnd_common.transcript import GAP_TOLERANCE_SEC, Turn, speaker_turns

# Re-exported for callers that used to import them from app.relabel: the
# clustering primitives now live in dnd_common so the attribution engine shares
# one implementation of the merge semantics (docs/attribution-model.md S12.5).
__all__ = [
    "GAP_TOLERANCE_SEC",
    "Turn",
    "centroid",
    "speaker_turns",
    "upgma_cluster",
]

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