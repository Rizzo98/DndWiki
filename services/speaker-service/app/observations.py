"""Per-observation windows: the unit of voice evidence.

Today speaker-service embeds ONE pooled window per diarized label
(app.audio.speaker_windows) and matches that single vector. The redesign needs
the opposite granularity: every speaker turn gets its own embedding, its own
quality score and its own row in the Qdrant voice_observations collection, so
that

- a cluster's internal spread can be measured at all (purity, S5.1);
- an individual utterance can defect from its cluster (alpha_u, S4.3);
- answering one question can re-score hundreds of utterances at once (S10.2).

This module is pure: it turns diarization segments into observations and decides
which of them are worth embedding. It never touches audio or a network.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from dnd_common.transcript import GAP_TOLERANCE_SEC, Turn, speaker_turns

#: Namespace for deterministic Qdrant point ids (re-runs overwrite, not append).
OBSERVATION_NAMESPACE = uuid.UUID("6f1a4a5e-1a2b-4c3d-8e4f-5a6b7c8d9e0f")


@dataclass(frozen=True)
class Observation:
    """One speaker turn, as the voice evidence for the utterances inside it."""

    index: int
    label: str
    chunk: int | None
    start: float
    end: float
    segment_indices: tuple[int, ...] = ()
    text: str = ""
    #: Highest diarization confidence among the segments (None when the backend
    #: reports none).
    diar_confidence: float | None = None

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def key(self) -> str:
        """Stable identity of the observation inside its session."""
        return observation_key(self.label, self.index)


def observation_key(label: str, index: int) -> str:
    """Idempotency key: one observation per (label, turn index) in a session."""
    return f"{label}#{index}"


def observation_point_id(session_id: str, label: str, index: int) -> str:
    """Deterministic Qdrant id of one observation (re-runs overwrite it)."""
    return str(
        uuid.uuid5(OBSERVATION_NAMESPACE, f"dnd:observation:{session_id}:{label}:{index}")
    )


def _segment_confidence(segment: dict[str, Any]) -> float | None:
    for key in ("speaker_confidence", "confidence"):
        value = segment.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def build_observations(
    segments: list[dict[str, Any]],
    *,
    chunk_boundaries: Iterable[int] | None = None,
    gap_tolerance: float = GAP_TOLERANCE_SEC,
) -> list[Observation]:
    """Turn diarization segments into one observation per speaker turn.

    Chunk boundaries are hard turn boundaries (raw labels restart per chunk).
    When the caller assembled the segment list from several per-chunk responses
    but the segments carry no 'chunk' key, it MUST pass 'chunk_boundaries':
    otherwise the absent keys compare equal and a same-label run merges across
    the boundary, silently turning two people into one observation.
    """
    turns = speaker_turns(
        segments, gap_tolerance=gap_tolerance, chunk_boundaries=chunk_boundaries
    )
    observations: list[Observation] = []
    for index, turn in enumerate(turns):
        confidences = [
            c
            for c in (_segment_confidence(segments[i]) for i in turn.indices)
            if c is not None
        ]
        observations.append(
            Observation(
                index=index,
                label=turn.label,
                chunk=turn.chunk,
                start=turn.start,
                end=turn.end,
                segment_indices=tuple(turn.indices),
                text=segments_text(segments, turn),
                diar_confidence=(sum(confidences) / len(confidences)) if confidences else None,
            )
        )
    return observations


def segments_text(segments: list[dict[str, Any]], turn: Turn) -> str:
    """Join the text of a turn's segments (used for the evidence view)."""
    parts = []
    for i in turn.indices:
        text = segments[i].get("text")
        if text:
            parts.append(str(text).strip())
    return " ".join(parts).strip()


@dataclass
class ObservationBatch:
    """Observations of one session plus the embed plan derived from them."""

    observations: list[Observation]
    #: Indices into 'observations' that are long enough to embed.
    embeddable: list[int] = field(default_factory=list)

    def by_label(self) -> dict[str, list[Observation]]:
        groups: dict[str, list[Observation]] = {}
        for observation in self.observations:
            groups.setdefault(observation.label, []).append(observation)
        return groups

    @property
    def labels(self) -> list[str]:
        """Labels in first-appearance order."""
        seen: list[str] = []
        for observation in self.observations:
            if observation.label not in seen:
                seen.append(observation.label)
        return seen


def plan_observations(
    segments: list[dict[str, Any]],
    *,
    min_sec: float,
    chunk_boundaries: Iterable[int] | None = None,
    gap_tolerance: float = GAP_TOLERANCE_SEC,
) -> ObservationBatch:
    """Build the observations and mark which turns are worth embedding.

    Turns shorter than 'min_sec' are kept as observations (the utterances inside
    them still exist and still need an attribution) but carry no own embedding:
    the engine treats them as voice-silent and lets the other channels decide.
    """
    observations = build_observations(
        segments, chunk_boundaries=chunk_boundaries, gap_tolerance=gap_tolerance
    )
    embeddable = [o.index for o in observations if o.duration >= min_sec]
    return ObservationBatch(observations=observations, embeddable=embeddable)
