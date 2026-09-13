"""Voice samples from previous sessions' manual (DM-confirmed) identifications.

The DM naming a speaker is the strongest label the platform ever gets: they
listened to the session and said "this is Anna". Those namings are stored per
session in speaker_assignments (session-service), so the audio behind them can
be reused: a new session is then identified not only against the profile
enrollment and the prints the naming event happened to write, but against
every turn the DM already labelled in this campaign.

Only turns worth trusting become samples: the diarization must be reasonably
confident about the label, and the turn must be long enough to carry a usable
voiceprint (ECAPA needs a few seconds of speech). Pure functions, no I/O, so
the policy is unit-tested without torch/qdrant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.audio import GAP_TOLERANCE_SEC

#: Payload keys that carry a segment's diarization confidence, in order of
#: preference (Deepgram reports speaker_confidence, the local WhisperX path
#: reports confidence).
CONFIDENCE_KEYS = ("speaker_confidence", "confidence")


def segment_confidence(segment: dict[str, Any]) -> float | None:
    """Diarization confidence of one segment (None when the backend omits it)."""
    for key in CONFIDENCE_KEYS:
        value = segment.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


@dataclass(frozen=True)
class SampleWindow:
    """One turn of a named speaker, usable as a voice sample."""

    start: float
    end: float
    confidence: float | None
    #: Stable position among the windows of one (session, label): the window id
    #: is derived from it, so re-running the backfill is idempotent.
    index: int

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class HistoryEntry:
    """A label the DM confirmed in an earlier session of the campaign."""

    session_id: str
    label: str
    user_id: str


@dataclass(frozen=True)
class HistorySample:
    """A window to embed, with the identity it belongs to."""

    session_id: str
    label: str
    user_id: str
    window: SampleWindow

    @property
    def key(self) -> str:
        """Idempotency key: one sample per (session, label, window)."""
        return history_key(self.session_id, self.label, self.window.index)


def history_key(session_id: str, label: str, index: int) -> str:
    """Payload marker of a backfilled sample (its identity in the store)."""
    return f"{session_id}#{label}#{index}"


def sample_windows(
    segments: list[dict[str, Any]],
    label: str,
    *,
    min_sec: float,
    max_sec: float,
    min_confidence: float,
    max_windows: int,
) -> list[SampleWindow]:
    """Pick the windows of one named label worth embedding.

    Segments of the label are filtered by confidence, merged into runs of
    consecutive speech (a silence gap up to GAP_TOLERANCE_SEC is tolerated) and
    trimmed to max_sec from the start of each run. Runs shorter than min_sec
    are dropped: a short turn carries too little voice to identify reliably.

    The longest runs win, but the returned list is ordered by start time so the
    window indexes (and therefore the stored ids) are stable across runs.
    """
    spans: list[tuple[float, float, float | None]] = []
    for segment in segments:
        if segment.get("speaker") != label:
            continue
        after = segment_confidence(segment)
        if after is not None and after < min_confidence:
            continue
        try:
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", 0.0))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        spans.append((start, end, after))
    if not spans:
        return []

    spans.sort(key=lambda span: span[0])
    runs: list[tuple[float, float, list[float]]] = []
    run_start, run_end, confidences = spans[0][0], spans[0][1], []
    if spans[0][2] is not None:
        confidences.append(spans[0][2])
    for start, end, confidence in spans[1:]:
        if start - run_end <= GAP_TOLERANCE_SEC:
            run_end = max(run_end, end)
        else:
            runs.append((run_start, run_end, confidences))
            run_start, run_end, confidences = start, end, []
        if confidence is not None:
            confidences.append(confidence)
    runs.append((run_start, run_end, confidences))

    candidates: list[tuple[float, float, float | None]] = []
    for run_start, run_end, confidences in runs:
        window_end = min(run_end, run_start + max_sec)
        if window_end - run_start < min_sec:
            continue
        mean = round(sum(confidences) / len(confidences), 4) if confidences else None
        candidates.append((run_start, window_end, mean))
    if not candidates:
        return []

    # Longest (most reliable) first, then earliest: a deterministic cut.
    candidates.sort(key=lambda candidate: (-(candidate[1] - candidate[0]), candidate[0]))
    kept = sorted(candidates[:max_windows], key=lambda candidate: candidate[0])
    return [
        SampleWindow(start=start, end=end, confidence=confidence, index=index)
        for index, (start, end, confidence) in enumerate(kept)
    ]


def manual_samples(
    history: list[HistoryEntry],
    diarizations: dict[str, list[dict[str, Any]]],
    *,
    already_enrolled: set[str],
    min_sec: float,
    max_sec: float,
    min_confidence: float,
    max_windows_per_label: int,
    max_windows_per_run: int,
) -> list[HistorySample]:
    """Turn a campaign's naming history into the samples to embed.

    'diarizations' maps a session id to its diarization segments; a session the
    worker could not read (missing artifact) is skipped. Samples already in the
    voiceprint store ('already_enrolled' holds their keys) are skipped too, so
    re-identifying a session never re-enrolls the same audio. The whole run is
    capped at max_windows_per_run embeddings, newest sessions first.
    """
    samples: list[HistorySample] = []
    seen: set[str] = set()
    for entry in history:
        if len(samples) >= max_windows_per_run:
            break
        segments = diarizations.get(entry.session_id)
        if not segments:
            continue
        for window in sample_windows(
            segments,
            entry.label,
            min_sec=min_sec,
            max_sec=max_sec,
            min_confidence=min_confidence,
            max_windows=max_windows_per_label,
        ):
            sample = HistorySample(
                session_id=entry.session_id,
                label=entry.label,
                user_id=entry.user_id,
                window=window,
            )
            if sample.key in already_enrolled or sample.key in seen:
                continue
            seen.add(sample.key)
            samples.append(sample)
            if len(samples) >= max_windows_per_run:
                break
    return samples
