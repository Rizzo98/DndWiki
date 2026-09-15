"""Split and merge: the engine saying "this hypothesis is wrong".

A voice identity is a guess, and the two ways it can be wrong are mirror images
(docs/attribution-model.md S5):

- **one identity, several people** - the classic is the DM voicing four NPCs, so
  the diarizer hears one voice that is really the narrator wearing hats. This is
  the NORMAL shape of a D&D session, not an edge case.
- **one person, several identities** - a player who shouts in combat and
  whispers at the table, or a diarizer that restarts labels at a chunk boundary.

Running the purity test on every identity is affordable because it is just
linear algebra; running it EARLY on suspicious identities is what makes the
engine fast. Five cheap triggers raise the prior, and only an identity that
trips one pays for the bootstrap (S5.2).

Pure module: no database, no audio. The audio-side re-diarization lives in
app.local_diarize; this module decides *where* to look and *what it means*.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from dnd_common.purity import (
    SameVoiceBaseline,
    assess_merge,
    assess_purity,
)

#: Triggers, in the order they are cheap to evaluate.
TRIGGER_KINDS = (
    "anchor_conflict",
    "length_outlier",
    "aba_pattern",
    "capability_contradiction",
    "confidence_collapse",
)

#: An A-B-A within this many seconds is the signature of a diarizer merging two
#: people who alternate, rather than of one person pausing.
ABA_WINDOW_SEC = 3.0
#: How far below the session median a segment's speaker confidence has to sit to
#: count as the diarizer itself being unsure.
CONFIDENCE_COLLAPSE_RATIO = 0.6


@dataclass(frozen=True)
class ObservationRef:
    """One observation, as the structure pass needs to see it."""

    key: str
    start: float
    end: float
    label: str | None = None
    quality: float | None = None
    diar_confidence: float | None = None
    diar_chunk: int | None = None
    text: str = ""
    #: (candidate_key, cosine) of the nearest enrolled voices, best first.
    nearest: tuple[tuple[str, float], ...] = ()
    #: Capability requirements the evidence pass extracted from this utterance.
    capability_reqs: tuple[str, ...] = ()

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class Trigger:
    """One reason to suspect an identity is not one person."""

    kind: str
    weight: float
    detail: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {"kind": self.kind, "weight": round(self.weight, 4), "detail": self.detail}


@dataclass
class StructureVerdict:
    """What the engine decided about one identity."""

    handle: str
    observation_keys: list[str]
    speech_sec: float
    purity: float
    impure: bool
    reason: str
    triggers: list[Trigger] = field(default_factory=list)
    #: The two halves of the accepted split, as indices into observation_keys.
    split: tuple[list[int], list[int]] | None = None

    @property
    def needs_local_rediarization(self) -> bool:
        return self.impure and self.split is not None

    def as_payload(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "purity": round(self.purity, 4),
            "impure": self.impure,
            "reason": self.reason,
            "triggers": [t.as_payload() for t in self.triggers],
            "speech_sec": round(self.speech_sec, 2),
            "n_observations": len(self.observation_keys),
        }


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return float((ordered[middle - 1] + ordered[middle]) / 2)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile (None for an empty sequence)."""
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return float(ordered[low] * (1 - weight) + ordered[high] * weight)


# --- the five triggers (S5.2) ----------------------------------------------


def anchor_conflict(
    observations: Sequence[ObservationRef], *, threshold: float = 0.75
) -> Trigger | None:
    """Two DIFFERENT enrolled members matched confidently inside one identity.

    The only way both can be right is that the identity holds two people, which
    makes this the strongest trigger in the set.
    """
    best: dict[str, list[str]] = {}
    for observation in observations:
        for candidate, cosine in observation.nearest:
            if cosine >= threshold:
                best.setdefault(candidate, []).append(observation.key)
                break
    if len(best) < 2:
        return None
    return Trigger(
        kind="anchor_conflict",
        weight=1.0,
        detail={"candidates": {key: len(keys) for key, keys in best.items()}},
    )


def length_outlier(
    observations: Sequence[ObservationRef], session_durations: Sequence[float]
) -> Trigger | None:
    """The identity's longest turn exceeds the session's p99 turn length.

    A four-minute "turn" at a table is almost never one person continuing to
    speak; it is a handover the diarizer missed.
    """
    if not observations or not session_durations:
        return None
    p99 = percentile(session_durations, 0.99)
    longest = max((o.duration for o in observations), default=0.0)
    if p99 is None or p99 <= 0 or longest <= p99:
        return None
    return Trigger(
        kind="length_outlier",
        weight=0.6,
        detail={"longest_sec": round(longest, 2), "session_p99_sec": round(p99, 2)},
    )


def aba_pattern(
    observations: Sequence[ObservationRef], *, window_sec: float = ABA_WINDOW_SEC
) -> Trigger | None:
    """The diarizer label changed and changed back inside a short window.

    Observed A-B-A within a few seconds is the signature of two alternating
    speakers that the diarizer merged into one identity.
    """
    if len(observations) < 3:
        return None
    ordered = sorted(observations, key=lambda o: o.start)
    for i in range(len(ordered) - 2):
        first, middle, last = ordered[i], ordered[i + 1], ordered[i + 2]
        if first.label is None or middle.label is None or last.label is None:
            continue
        if first.label != last.label or first.label == middle.label:
            continue
        if last.start - first.end <= window_sec:
            return Trigger(
                kind="aba_pattern",
                weight=0.7,
                detail={
                    "labels": [first.label, middle.label, last.label],
                    "window_sec": round(last.start - first.end, 3),
                },
            )
    return None


def capability_contradiction(
    observations: Sequence[ObservationRef],
    member_capabilities: dict[str, frozenset[str]],
) -> Trigger | None:
    """The identity's utterances require capabilities no single member has.

    Semantic evidence that the audio contradicts itself: if one voice needs
    'spell:fireball' AND 'feature:rage' and no member can do both, it is not one
    member. Without a DM-authored sheet this only raises the prior - capability
    constraints are never a hard veto (S4.2, S16).
    """
    required: set[str] = set()
    for observation in observations:
        required.update(observation.capability_reqs)
    if not required:
        return None
    if not member_capabilities:
        return None
    feasible = [member for member, caps in member_capabilities.items() if required <= caps]
    if feasible:
        return None
    # Report the smallest subset that no member can satisfy, so the audit trail
    # names the contradiction rather than dumping every requirement.
    for size in range(2, len(required) + 1):
        for combo in _combinations(sorted(required), size):
            if not any(
                set(combo) <= caps for caps in member_capabilities.values()
            ):
                return Trigger(
                    kind="capability_contradiction",
                    weight=0.8,
                    detail={"requires": list(combo)},
                )
    return None


def _combinations(items: Sequence[str], size: int) -> Iterable[tuple[str, ...]]:
    if size == 0:
        yield ()
        return
    for index in range(len(items) - size + 1):
        head = items[index]
        for tail in _combinations(items[index + 1 :], size - 1):
            yield (head, *tail)


def confidence_collapse(
    observations: Sequence[ObservationRef], session_confidences: Sequence[float]
) -> Trigger | None:
    """The identity holds segments the diarizer was itself unsure about."""
    median = _median([c for c in session_confidences if c is not None])
    if median is None or median <= 0:
        return None
    flagged = [
        o.key
        for o in observations
        if o.diar_confidence is not None
        and o.diar_confidence < median * CONFIDENCE_COLLAPSE_RATIO
    ]
    if not flagged:
        return None
    return Trigger(
        kind="confidence_collapse",
        weight=0.5,
        detail={
            "session_median": round(median, 4),
            "flagged": len(flagged),
            "observations": flagged[:20],
        },
    )


def evaluate_triggers(
    observations: Sequence[ObservationRef],
    *,
    session_durations: Sequence[float] = (),
    session_confidences: Sequence[float] = (),
    member_capabilities: dict[str, frozenset[str]] | None = None,
    anchor_threshold: float = 0.75,
) -> list[Trigger]:
    """Every trigger that fires for one identity, strongest first."""
    found = [
        anchor_conflict(observations, threshold=anchor_threshold),
        capability_contradiction(observations, member_capabilities or {}),
        aba_pattern(observations),
        length_outlier(observations, session_durations),
        confidence_collapse(observations, session_confidences),
    ]
    triggers = [t for t in found if t is not None]
    triggers.sort(key=lambda t: (-t.weight, t.kind))
    return triggers


# --- the verdict ------------------------------------------------------------


def evaluate_identity(
    handle: str,
    observation_keys: Sequence[str],
    *,
    vectors: Sequence[Sequence[float]],
    durations: Sequence[float],
    observations: Sequence[ObservationRef] | None = None,
    baseline: SameVoiceBaseline | None = None,
    session_durations: Sequence[float] = (),
    session_confidences: Sequence[float] = (),
    member_capabilities: dict[str, frozenset[str]] | None = None,
    anchor_threshold: float = 0.75,
    purity_split_threshold: float = 0.80,
    purity_margin: float = 0.10,
    purity_min_members: int = 3,
    purity_min_speech_sec: float = 10.0,
    seed: int = 20240914,
) -> StructureVerdict:
    """Decide whether one identity is one voice, and where to cut it if not.

    The bootstrap only runs when a trigger fired or the observations are
    numerous enough to be worth the check, so the common case - a clean identity
    of a few turns - costs one k-means and no resampling.
    """
    observations = list(observations or [])
    triggers = evaluate_triggers(
        observations,
        session_durations=session_durations,
        session_confidences=session_confidences,
        member_capabilities=member_capabilities,
        anchor_threshold=anchor_threshold,
    )
    verdict = assess_purity(
        vectors,
        durations=durations,
        baseline=baseline,
        margin=purity_margin,
        min_members=purity_min_members,
        min_speech_sec=purity_min_speech_sec,
        seed=seed,
    )
    split = None
    if verdict.impure:
        split = _split_indices(vectors, seed=seed)

    return StructureVerdict(
        handle=handle,
        observation_keys=list(observation_keys),
        speech_sec=sum(durations),
        purity=verdict.purity,
        impure=verdict.impure,
        reason=verdict.reason,
        triggers=triggers,
        split=split,
    )


def _split_indices(
    vectors: Sequence[Sequence[float]], *, seed: int
) -> tuple[list[int], list[int]]:
    from dnd_common.clustering import spherical_kmeans

    labels = spherical_kmeans(vectors, 2, restarts=25, seed=seed)
    first = [i for i, label in enumerate(labels) if label == 0]
    second = [i for i, label in enumerate(labels) if label == 1]
    return first, second


def split_boundary(
    observations: Sequence[ObservationRef], split: tuple[list[int], list[int]]
) -> float | None:
    """The instant where the two halves of a split meet, in time.

    Local re-diarization re-processes a SPAN; the review card and the split
    question both want the boundary instant, which is the end of the last
    observation of the earlier half.
    """
    left, right = split
    if not left or not right:
        return None
    last_left = max((observations[i].end for i in left), default=None)
    first_right = min((observations[i].start for i in right), default=None)
    if last_left is None or first_right is None:
        return None
    return round((last_left + first_right) / 2, 3)


def rediarize_span(
    observations: Sequence[ObservationRef], padding: float = 2.0
) -> tuple[float, float] | None:
    """The audio span to re-process when an identity is impure (S5.3)."""
    if not observations:
        return None
    return (
        max(0.0, min(o.start for o in observations) - padding),
        max(o.end for o in observations) + padding,
    )


# --- merges (S5.4) ----------------------------------------------------------


@dataclass(frozen=True)
class MergeProposal:
    """Two identities the engine thinks might be one person."""

    left_handle: str
    right_handle: str
    similarity: float
    distance: float
    reason: str
    detail: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {
            "left": self.left_handle,
            "right": self.right_handle,
            "similarity": round(self.similarity, 4),
            "reason": self.reason,
            "detail": self.detail,
        }


def propose_merges(
    identities: Sequence[dict[str, Any]],
    *,
    baseline: SameVoiceBaseline | None = None,
    llr_gain: dict[tuple[str, str], float] | None = None,
) -> list[MergeProposal]:
    """Pairwise merge proposals, strongest first.

    'identities' are dicts with 'handle', 'centroid', optional 'span'
    (start, end), 'is_dm' and 'anchor_conflict'. Every proposal is filtered
    through `assess_merge`, which refuses a merge when the two identities
    speak at the same time (a person cannot be in two places at once, unless
    they are the DM, who can) or when propagation disagrees.
    """
    proposals: list[MergeProposal] = []
    for i in range(len(identities)):
        for j in range(i + 1, len(identities)):
            left, right = identities[i], identities[j]
            gain = None
            if llr_gain is not None:
                gain = llr_gain.get((left["handle"], right["handle"]))
            verdict = assess_merge(
                left.get("centroid") or [],
                right.get("centroid") or [],
                baseline=baseline,
                a_is_dm=bool(left.get("is_dm")),
                b_is_dm=bool(right.get("is_dm")),
                a_span=left.get("span"),
                b_span=right.get("span"),
                anchor_conflict=bool(
                    left.get("anchor_conflict") or right.get("anchor_conflict")
                ),
                llr_gain=gain,
            )
            if not verdict.merge:
                continue
            proposals.append(
                MergeProposal(
                    left_handle=str(left["handle"]),
                    right_handle=str(right["handle"]),
                    similarity=1.0 - verdict.distance,
                    distance=verdict.distance,
                    reason=verdict.reason,
                    detail=verdict.detail,
                )
            )
    proposals.sort(key=lambda p: (-p.similarity, p.left_handle, p.right_handle))
    return proposals
