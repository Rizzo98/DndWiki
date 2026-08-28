"""Shared transcript primitives: segment -> speaker turn grouping.

Both speaker-service (embedding re-clustering) and refiner-service (LLM
contextual diarization) need to group diarized segments into speaker turns:
maximal runs of consecutive segments sharing the same raw diarizer label,
bounded by silence gaps and by source-chunk boundaries (raw labels restart
per chunk, so a same-label run never spans two chunks).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Maximum silence gap between two segments of the same turn.
GAP_TOLERANCE_SEC = 1.5


@dataclass
class Turn:
    """A maximal run of consecutive same-raw-label segments (one speaker turn)."""

    label: str
    start: float
    end: float
    chunk: int | None
    indices: list[int] = field(default_factory=list)


def speaker_turns(
    segments: list[dict[str, Any]], gap_tolerance: float = GAP_TOLERANCE_SEC
) -> list[Turn]:
    """Split segments into speaker turns (contiguous same-raw-label runs).

    A turn ends when the raw label changes, the silence gap exceeds
    'gap_tolerance', or the source chunk changes (chunk boundaries are hard
    turn boundaries because raw labels restart per chunk). Segments without a
    speaker or with non-positive duration are skipped (they stay unrelabeled).
    """
    turns: list[Turn] = []
    cur: Turn | None = None
    for i, seg in enumerate(segments):
        label = seg.get("speaker")
        start = float(seg.get("start") or 0.0)
        end = float(seg.get("end") or 0.0)
        chunk = seg.get("chunk")
        if not label or end <= start:
            continue
        if (
            cur is not None
            and cur.label == label
            and cur.chunk == chunk
            and start - cur.end <= gap_tolerance
        ):
            cur.end = max(cur.end, end)
            cur.indices.append(i)
        else:
            if cur is not None:
                turns.append(cur)
            cur = Turn(label=label, start=start, end=end, chunk=chunk, indices=[i])
    if cur is not None:
        turns.append(cur)
    return turns
