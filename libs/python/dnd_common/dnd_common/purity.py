"""Cluster purity: is this voice identity really one person?

A voice identity is a *hypothesis*, and the engine must be able to say "this
hypothesis is wrong" (docs/attribution-model.md S5.1). Diarization routinely
merges two people who alternate - most predictably the DM voicing several NPCs,
which is not an edge case but the normal shape of a D&D session.

The test is deliberately conservative, because a false split is worse than a
missed one: it destroys a correct identity and asks the DM to fix what was
already right. A verdict therefore needs BOTH a geometric margin at the point
estimate AND stability under bootstrap resampling.

This module is pure (standard library + dnd_common.clustering) so both
speaker-service (the enrollment gate, S12.3) and attribution-service (the engine)
share exactly one definition of "impure".
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from dnd_common.clustering import (
    adjusted_rand_index,
    centroid,
    cosine,
    mean_intra_cosine,
    spherical_kmeans,
)

Vector = Sequence[float]

#: How much the between-cluster separation must beat the worst within-cluster
#: spread before a split is even considered.
DEFAULT_MARGIN = 0.10
#: A cluster of two utterances is not evidence of anything; two people must each
#: account for a real share of the audio.
DEFAULT_MIN_MEMBERS = 3
DEFAULT_MIN_SPEECH_SEC = 10.0
#: Cap on the sample fed to the splitter: the longest/cleanest observations.
DEFAULT_MAX_OBSERVATIONS = 200
#: Bootstrap settings (S5.1 step 4).
DEFAULT_RESAMPLES = 20
DEFAULT_RESAMPLE_FRACTION = 0.8
DEFAULT_ARI_THRESHOLD = 0.8
DEFAULT_STABILITY_THRESHOLD = 0.8
#: Seed for the bootstrap; fixed so a re-run of the engine reaches the same
#: verdict, which is what makes an attribution revision reproducible.
DEFAULT_SEED = 20240914
#: How far apart two observations of the SAME speaker normally are, as a cosine
#: DISTANCE. Every quantity in this module is a distance: 'sep', 'within' and the
#: baseline are all 1 - cosine, so comparisons are dimensionally consistent.
#: The old code approximated d_same_p95 with the global constant
#: relabel_merge_threshold = 0.5, which cannot know how similar two samples of
#: THIS person's voice are (S12.5).
COLD_START_D50 = 0.28
COLD_START_D95 = 0.50


@dataclass(frozen=True)
class SameVoiceBaseline:
    """Within-speaker pairwise cosine DISTANCES, calibrated per campaign.

    'p50' is the typical distance between two samples of one voice and 'p95' the
    far tail: a pair farther apart than p95 is unremarkable for one speaker.
    Both are distances (1 - cosine), matching every other quantity here.

    The redesign fits these from the campaign's confirmed history and enrollment
    samples; the cold-start values stand in until enough labelled audio exists.
    """

    p50: float = COLD_START_D50
    p95: float = COLD_START_D95
    n_samples: int = 0

    @classmethod
    def cold_start(cls) -> SameVoiceBaseline:
        return cls()

    @classmethod
    def from_distances(cls, distances: Sequence[float]) -> SameVoiceBaseline:
        """Fit the baseline from observed within-speaker pairwise distances."""
        values = sorted(float(d) for d in distances)
        if not values:
            return cls.cold_start()
        return cls(
            p50=_percentile(values, 0.50),
            p95=_percentile(values, 0.95),
            n_samples=len(values),
        )

    def as_payload(self) -> dict[str, Any]:
        return {"p50": round(self.p50, 4), "p95": round(self.p95, 4), "n": self.n_samples}


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile of an already sorted sequence."""
    if not sorted_values:
        raise ValueError("empty sequence")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = q * (len(sorted_values) - 1)
    low = math.floor(position)
    high = min(low + 1, len(sorted_values) - 1)
    weight = position - low
    return float(sorted_values[low] * (1.0 - weight) + sorted_values[high] * weight)


@dataclass(frozen=True)
class SplitShape:
    """Geometry of the best two-way split of one cluster.

    Both numbers are cosine DISTANCES: 'sep' is how far apart the two centroids
    are, 'within' is the worst of the two clusters' mean internal distance.
    """

    sep: float
    within: float
    sizes: tuple[int, int]

    @property
    def margin(self) -> float:
        return self.sep - self.within


@dataclass(frozen=True)
class PurityVerdict:
    """The stored purity of one voice identity."""

    purity: float
    impure: bool
    sep: float
    within: float
    sizes: tuple[int, int]
    stability: float
    reason: str
    n_observations: int
    speech_sec: float

    def as_payload(self) -> dict[str, Any]:
        return {
            "purity": round(self.purity, 4),
            "impure": self.impure,
            "sep": round(self.sep, 4),
            "within": round(self.within, 4),
            "sizes": list(self.sizes),
            "stability": round(self.stability, 4),
            "reason": self.reason,
            "n_observations": self.n_observations,
        }


def split_shape(vectors: Sequence[Vector], labels: Sequence[int]) -> SplitShape:
    """Separation and worst within-cluster cohesion of a two-way partition.

    'sep' is the cosine distance between the two centroids; 'within' is the
    WORST of the two clusters' mean internal distance (the weakest link decides,
    not the average: one tight cluster beside one mush is still a mush).
    """
    groups: dict[int, list[int]] = {}
    for index, label in enumerate(labels):
        groups.setdefault(label, []).append(index)
    ids = sorted(groups)
    if len(ids) != 2:
        return SplitShape(sep=0.0, within=1.0, sizes=tuple(len(groups[i]) for i in ids))
    a, b = ids
    cent_a = centroid([vectors[i] for i in groups[a]])
    cent_b = centroid([vectors[i] for i in groups[b]])
    sep = 1.0 - cosine(cent_a, cent_b)
    intra = mean_intra_cosine(vectors, labels)
    # A cluster with a single member has no pair: treat its internal distance as
    # zero so it cannot, by itself, block a split.
    within = max(1.0 - intra.get(a, 1.0), 1.0 - intra.get(b, 1.0))
    return SplitShape(sep=sep, within=within, sizes=(len(groups[a]), len(groups[b])))


def _sum(values: Sequence[float]) -> float:
    return float(sum(values))


def bootstrap_stability(
    vectors: Sequence[Vector],
    reference: Sequence[int],
    *,
    durations: Sequence[float] | None = None,
    resamples: int = DEFAULT_RESAMPLES,
    fraction: float = DEFAULT_RESAMPLE_FRACTION,
    ari_threshold: float = DEFAULT_ARI_THRESHOLD,
    seed: int = DEFAULT_SEED,
) -> float:
    """Fraction of bootstrap resamples that reproduce the same partition.

    Each resample clusters a random subset of the observations; the induced
    partition on that subset is compared to the reference partition restricted
    to the same subset with the Adjusted Rand Index (invariant to the cluster
    numbering, which is arbitrary here). A resample whose subset is too small to
    split is counted as not reproducing, because it produced no evidence.
    """
    n = len(vectors)
    if n < 4 or resamples <= 0:
        return 1.0
    rng = random.Random(seed)
    size = max(2, round(fraction * n))
    stable = 0
    for _ in range(resamples):
        subset = sorted(rng.sample(range(n), min(size, n)))
        subset_vectors = [vectors[i] for i in subset]
        labels = spherical_kmeans(subset_vectors, 2, restarts=5, seed=rng.randrange(1 << 30))
        if len(set(labels)) < 2:
            continue
        reference_subset = [reference[i] for i in subset]
        if len(set(reference_subset)) < 2:
            continue
        if adjusted_rand_index(reference_subset, labels) >= ari_threshold:
            stable += 1
    return stable / resamples


def assess_purity(
    vectors: Sequence[Vector],
    *,
    durations: Sequence[float] | None = None,
    baseline: SameVoiceBaseline | None = None,
    margin: float = DEFAULT_MARGIN,
    min_members: int = DEFAULT_MIN_MEMBERS,
    min_speech_sec: float = DEFAULT_MIN_SPEECH_SEC,
    max_observations: int = DEFAULT_MAX_OBSERVATIONS,
    resamples: int = DEFAULT_RESAMPLES,
    stability_threshold: float = DEFAULT_STABILITY_THRESHOLD,
    seed: int = DEFAULT_SEED,
    kmeans_restarts: int = 25,
) -> PurityVerdict:
    """Decide whether one voice identity holds more than one voice.

    Order of the gates, cheapest first: sample size, per-side speech seconds,
    then the geometric margin, then (only for candidates that pass) the
    bootstrap. The bootstrap is the expensive step and runs at most once per
    identity, which is what keeps "run it on every identity" affordable.
    """
    baseline = baseline or SameVoiceBaseline.cold_start()
    durations = list(durations) if durations is not None else [0.0] * len(vectors)
    n = len(vectors)

    def verdict(
        *, purity: float, impure: bool, shape: SplitShape, stability: float, reason: str
    ) -> PurityVerdict:
        return PurityVerdict(
            purity=purity,
            impure=impure,
            sep=shape.sep,
            within=shape.within,
            sizes=shape.sizes,
            stability=stability,
            reason=reason,
            n_observations=n,
            speech_sec=_sum(durations),
        )

    if n < 2 * min_members:
        return verdict(
            purity=1.0,
            impure=False,
            shape=SplitShape(sep=0.0, within=1.0, sizes=(n, 0)),
            stability=1.0,
            reason="too_few_observations",
        )

    # Longest (cleanest) observations first, capped.
    if n > max_observations:
        order = sorted(range(n), key=lambda i: -durations[i])[:max_observations]
        order.sort()
        vectors = [vectors[i] for i in order]
        durations = [durations[i] for i in order]
        n = len(vectors)

    labels = spherical_kmeans(vectors, 2, restarts=kmeans_restarts, seed=seed)
    shape = split_shape(vectors, labels)
    if len(set(labels)) < 2:
        return verdict(
            purity=1.0,
            impure=False,
            shape=shape,
            stability=1.0,
            reason="no_split_found",
        )

    groups: dict[int, list[int]] = {}
    for index, label in enumerate(labels):
        groups.setdefault(label, []).append(index)
    seconds = [_sum([durations[i] for i in members]) for members in groups.values()]
    if min(shape.sizes) < min_members:
        return verdict(
            purity=1.0,
            impure=False,
            shape=shape,
            stability=1.0,
            reason="lopsided_split",
        )
    if min(seconds) < min_speech_sec:
        return verdict(
            purity=1.0,
            impure=False,
            shape=shape,
            stability=1.0,
            reason="not_enough_speech",
        )

    if not (shape.sep > shape.within + margin):
        return verdict(
            purity=1.0,
            impure=False,
            shape=shape,
            stability=1.0,
            reason="no_margin",
        )
    if not (shape.sep > baseline.p95):
        return verdict(
            purity=1.0,
            impure=False,
            shape=shape,
            stability=1.0,
            reason="within_same_voice_range",
        )

    stability = bootstrap_stability(
        vectors, labels, durations=durations, resamples=resamples, seed=seed
    )
    impure = stability >= stability_threshold
    return verdict(
        purity=stability,
        impure=impure,
        shape=shape,
        stability=stability,
        reason="impure" if impure else "unstable_split",
    )


@dataclass
class MergeVerdict:
    """Whether two identities should be merged (S5.4)."""

    merge: bool
    distance: float
    overlaps: bool
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


def assess_merge(
    cent_a: Vector,
    cent_b: Vector,
    *,
    baseline: SameVoiceBaseline | None = None,
    a_is_dm: bool = False,
    b_is_dm: bool = False,
    a_span: tuple[float, float] | None = None,
    b_span: tuple[float, float] | None = None,
    anchor_conflict: bool = False,
    llr_gain: float | None = None,
) -> MergeVerdict:
    """Propose a merge for two identities that look like one person.

    Three gates, in the order of S5.4: the centroids must be closer than two
    samples of one voice normally are; neither side may have contradicting
    anchor evidence; and two identities that speak at the same time are two
    people (unless one of them is the DM, who can legitimately be in two
    places). A merge is additionally only applied when the propagation math
    agrees ('llr_gain' > 0), so an unresolved-but-plausible pair stays a
    question for the DM instead of a silent rewrite.
    """
    baseline = baseline or SameVoiceBaseline.cold_start()
    distance = 1.0 - cosine(cent_a, cent_b)
    overlaps = _spans_overlap(a_span, b_span) and not (a_is_dm or b_is_dm)
    detail = {
        "distance": round(distance, 4),
        "d_same_p50": round(baseline.p50, 4),
        "overlaps": overlaps,
        "anchor_conflict": anchor_conflict,
    }
    if anchor_conflict:
        return MergeVerdict(False, distance, overlaps, "anchor_conflict", detail)
    if distance > baseline.p50:
        return MergeVerdict(False, distance, overlaps, "not_similar_enough", detail)
    if overlaps:
        return MergeVerdict(False, distance, overlaps, "speaks_at_the_same_time", detail)
    if llr_gain is not None and llr_gain <= 0.0:
        return MergeVerdict(False, distance, overlaps, "propagation_disagrees", detail)
    return MergeVerdict(True, distance, overlaps, "merge", detail)


def _spans_overlap(
    a: tuple[float, float] | None, b: tuple[float, float] | None
) -> bool:
    if not a or not b:
        return False
    return a[0] < b[1] and b[0] < a[1]
