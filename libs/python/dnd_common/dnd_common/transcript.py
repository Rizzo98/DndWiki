"""Shared transcript primitives: segment -> speaker turn grouping.

Both speaker-service (embedding re-clustering) and refiner-service (LLM
contextual diarization) need to group diarized segments into speaker turns:
maximal runs of consecutive segments sharing the same raw diarizer label,
bounded by silence gaps and by source-chunk boundaries (raw labels restart
per chunk, so a same-label run never spans two chunks).
"""

from __future__ import annotations

from collections.abc import Iterable
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
    segments: list[dict[str, Any]],
    gap_tolerance: float = GAP_TOLERANCE_SEC,
    *,
    chunk_key: str = "chunk",
    chunk_boundaries: Iterable[int] | None = None,
) -> list[Turn]:
    """Split segments into speaker turns (contiguous same-raw-label runs).

    A turn ends when the raw label changes, the silence gap exceeds
    'gap_tolerance', or the source chunk changes (chunk boundaries are hard
    turn boundaries because raw labels restart per chunk). Segments without a
    speaker or with non-positive duration are skipped (they stay unrelabeled).

    'chunk_boundaries' is the escape hatch for the one case the key cannot
    express: a caller that KNOWS where the chunks start (because it assembled
    the segment list from several per-chunk responses) but whose segments carry
    no 'chunk' key. Without it, absent keys compare equal on both sides of
    'cur.chunk == chunk' (None == None), so a same-label run silently merges
    across the chunk boundary and two different people become one turn. When
    the caller declares the boundaries, index N starts a new chunk and the
    merge cannot happen. Boundaries are segment indices; a boundary at index 0
    (the first segment) is implied and ignored.
    """
    boundaries = set(chunk_boundaries or ())
    turns: list[Turn] = []
    cur: Turn | None = None
    for i, seg in enumerate(segments):
        label = seg.get("speaker")
        start = float(seg.get("start") or 0.0)
        end = float(seg.get("end") or 0.0)
        chunk = seg.get(chunk_key)
        if not label or end <= start:
            continue
        new_chunk = i in boundaries
        if (
            cur is not None
            and cur.label == label
            and cur.chunk == chunk
            and not new_chunk
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

