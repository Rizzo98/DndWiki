"""Voice evidence: what speaker-service now returns instead of a verdict.

Before the redesign, this service decided: a pooled cosine above
SPEAKER_MATCH_THRESHOLD became 'auto', anything else became 'pending', and the
session blocked until the DM validated every label. That is a policy decision
living inside an ML pipeline, and it is the single behaviour the redesign
removes (docs/attribution-model.md S12.1).

After it, the service returns **evidence only**:

    observation -> { nearest_centroids: [{member_id, user_id, centroid_id,
                                          cosine, quality, source}],
                     model_version }

and the attribution engine turns a cosine into a log-likelihood ratio using the
campaign's fitted calibration. The threshold 0.75 stops being a policy and
becomes one point on a fitted score-to-LLR curve.

Three things this module is responsible for keeping honest:

- **multi-centroid scoring** (S12.2): a member is scored against their best
  centroids, never against one average that represents none of their voices;
- **no verdict leaks**: nothing here returns 'auto' or 'pending'. The legacy
  verdict lives in app.identify and is only used while the redesign is off;
- **userless members are representable** (S12.3 defect 4): a voice model is
  keyed on member_id, and the user_id is only ever a convenience link, so a
  member with no account can finally be learned. During the transition, prints
  written before this change carry a user_id and no member_id, so a hit exposes
  both and 'candidate_key' prefers the member.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

#: Aggregation modes for turning a member's centroid similarities into one score.
TOP_K_MODES = ("top1", "topk_mean")


@dataclass(frozen=True)
class CentroidHit:
    """One (candidate, centroid) similarity for one observation."""

    centroid_id: str
    cosine: float
    member_id: str | None = None
    user_id: str | None = None
    quality: float | None = None
    source: str | None = None

    @property
    def candidate_key(self) -> str | None:
        """How the attribution engine names this candidate.

        'member:<uuid>' when the print knows its campaign member (everything
        written from now on), else 'user:<uuid>' for the legacy prints that only
        carry an account link, else None for a print that can identify nobody.
        """
        if self.member_id:
            return f"member:{self.member_id}"
        if self.user_id:
            return f"user:{self.user_id}"
        return None

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "centroid_id": self.centroid_id,
            "cosine": round(self.cosine, 4),
        }
        if self.member_id is not None:
            payload["member_id"] = self.member_id
        if self.user_id is not None:
            payload["user_id"] = self.user_id
        if self.quality is not None:
            payload["quality"] = round(self.quality, 4)
        if self.source is not None:
            payload["source"] = self.source
        return payload


def hits_from_points(
    points: Sequence[Any],
    *,
    centroid_prefix: str,
    default_source: str | None = None,
) -> list[CentroidHit]:
    """Convert Qdrant search hits into CentroidHits (payload-shape tolerant).

    Old voiceprints carry only {user_id, source, session_id}; the new member
    voice models carry {member_id, user_id, source, n_samples}. Both are read
    here so the transition needs no big-bang re-enrollment.
    """
    hits: list[CentroidHit] = []
    for point in points:
        payload = getattr(point, "payload", None) or {}
        score = getattr(point, "score", None)
        if score is None:
            continue
        member_id = payload.get("member_id")
        user_id = payload.get("user_id")
        if not member_id and not user_id:
            continue  # identifies nobody; not evidence
        hits.append(
            CentroidHit(
                centroid_id=f"{centroid_prefix}:{getattr(point, 'id', '?')}",
                cosine=float(score),
                member_id=str(member_id) if member_id else None,
                user_id=str(user_id) if user_id else None,
                quality=(
                    float(payload["quality"])
                    if isinstance(payload.get("quality"), (int, float))
                    else None
                ),
                source=str(payload.get("source") or default_source or "") or None,
            )
        )
    return hits


def select_nearest(
    hits: Sequence[CentroidHit],
    *,
    limit: int = 8,
    floor: float = 0.2,
) -> list[CentroidHit]:
    """The centroids worth sending: best-first, floored, capped.

    The single best hit is always kept even when it is below the floor: the
    engine needs to know that the best match was weak (that is evidence of
    'nobody in the party'), and an empty list would be indistinguishable from
    'no audio'.
    """
    ordered = sorted(
        hits, key=lambda h: (-h.cosine, h.candidate_key or "", h.centroid_id)
    )
    if not ordered:
        return []
    kept = [ordered[0]]
    for hit in ordered[1:]:
        if hit.cosine < floor or len(kept) >= max(1, limit):
            break
        kept.append(hit)
    return kept


def aggregate_candidate_scores(
    hits: Iterable[CentroidHit],
    *,
    top_k: int = 3,
    mode: str = "topk_mean",
) -> dict[str, float]:
    """Collapse a candidate's centroid similarities into one score per candidate.

    'top1' takes the best centroid; 'topk_mean' averages the best 'top_k' (a
    candidate with fewer centroids simply averages what they have). Averaging
    over the best few rather than over all centroids is what keeps one odd print
    (a bad laptop mic, a shouted line) from dragging down every genuine match.
    """
    if mode not in TOP_K_MODES:
        raise ValueError(f"unknown aggregation mode: {mode}")
    by_candidate: dict[str, list[float]] = {}
    for hit in hits:
        key = hit.candidate_key
        if key is None:
            continue
        by_candidate.setdefault(key, []).append(hit.cosine)
    scores: dict[str, float] = {}
    for key, cosines in by_candidate.items():
        cosines.sort(reverse=True)
        if mode == "top1":
            scores[key] = cosines[0]
        else:
            window = cosines[: max(1, top_k)]
            scores[key] = sum(window) / len(window)
    return scores


def observation_evidence(
    *,
    observation_id: str,
    label: str,
    index: int,
    start: float,
    end: float,
    quality: float | None,
    hits: Sequence[CentroidHit],
    limit: int = 8,
    floor: float = 0.2,
) -> dict[str, Any]:
    """One observation's evidence record for the speakers.identified payload."""
    return {
        "id": observation_id,
        "label": label,
        "index": index,
        "start": round(start, 3),
        "end": round(end, 3),
        "quality": None if quality is None else round(quality, 4),
        "nearest_centroids": [
            h.as_payload() for h in select_nearest(hits, limit=limit, floor=floor)
        ],
    }


def evidence_payload(
    observations: Sequence[dict[str, Any]],
    *,
    model_version: str,
    embedding_version: int,
) -> dict[str, Any]:
    """The 'evidence' block published alongside the legacy verdicts."""
    return {
        "model_version": model_version,
        "embedding_version": embedding_version,
        "observations": list(observations),
    }
