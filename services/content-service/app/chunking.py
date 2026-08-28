"""Transcript chunking: overlapping chunks of the compact speaker view.

The worker builds one line per transcript segment ('[HH:MM:SS] NAME: text')
and greedily groups lines into token-bounded chunks with a configurable
overlap. Lines are never split mid-segment, and a single oversized segment
is kept whole (the LLM still extracts from it).
"""

from __future__ import annotations

from typing import Any

#: Rough chars-per-token estimate (English ~4 chars/token). Good enough for
#: chunk sizing; a tokenizer (tiktoken) can replace this later if needed.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Cheap token estimate for chunk sizing (never zero for non-empty text)."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def format_timestamp(start_sec: float) -> str:
    """Float seconds -> 'HH:MM:SS'."""
    total = max(0, int(start_sec))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def build_view_lines(segments: list[dict[str, Any]], speaker_names: dict[str, str]) -> list[str]:
    """Compact '[HH:MM:SS] NAME: text' lines, one per non-empty segment."""
    lines: list[str] = []
    for segment in segments:
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        speaker = segment.get("speaker")
        if speaker is None:
            name = "UNKNOWN"
        else:
            name = speaker_names.get(speaker, speaker)
        lines.append(f"[{format_timestamp(segment.get('start', 0.0))}] {name}: {text}")
    return lines


def chunk_lines(
    lines: list[str],
    max_tokens: int,
    overlap: float,
) -> list[list[str]]:
    """Greedily group view lines into overlapping chunks.

    The next chunk starts 'overlap' of the way back into the previous one
    (at least one line), so entities near a boundary are seen twice.
    """
    if not lines:
        return []
    tokens = [estimate_tokens(line) for line in lines]
    chunks: list[list[str]] = []
    start = 0
    n = len(lines)
    while start < n:
        end = start
        acc = 0
        while end < n and acc + tokens[end] <= max_tokens:
            acc += tokens[end]
            end += 1
        if end == start:
            end = start + 1  # single line exceeds the budget; keep it whole
        chunks.append(lines[start:end])
        if end >= n:
            break  # everything is covered; no new content for another chunk
        count = end - start
        overlap_count = max(1, round(count * overlap))
        start = max(start + 1, end - overlap_count)
    return chunks


def chunk_transcript(
    segments: list[dict[str, Any]],
    speaker_names: dict[str, str],
    *,
    max_tokens: int,
    overlap: float,
) -> list[list[str]]:
    """Full pipeline: transcript segments -> list of chunk view line-lists."""
    lines = build_view_lines(segments, speaker_names)
    return chunk_lines(lines, max_tokens, overlap)
