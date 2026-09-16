"""Transcript chunking: overlapping chunks of the compact speaker view.

The worker builds one line per transcript segment ('[HH:MM:SS] NAME: text')
and greedily groups lines into token-bounded chunks with a configurable
overlap. Lines are never split mid-segment, and a single oversized segment
is kept whole (the LLM still extracts from it).
"""

from __future__ import annotations

import re
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


#: How an uncertain or unresolved line is rendered into the view (S14.2).
VIEW_UNATTRIBUTED = "(unattributed)"


def build_view_lines_from_artifact(artifact: dict[str, Any]) -> list[str]:
    """View lines from the ATTRIBUTED transcript, not from a label map.

    Each line carries the utterance reference the extraction must echo back in
    its source_refs, plus the certainty IN THE TEXT ITSELF:

        [u_00412 00:41:15] Aramil: I cast fireball on the three goblins
        [u_00413 00:41:19] Aramil?: wait, do I still get my attack?
        [u_00414 00:41:24] (unattributed): ok so I move up to the door

    The '?' is not decoration: the prompt rule that goes with it is "facts stated
    BY an uncertain speaker may be recorded, but must not be attributed to that
    character", and the '(unattributed)' rule is "describe this at party level or
    omit it". That single change is what makes every fact on every page traceable
    back to a timestamp and an audio span.
    """
    lines: list[str] = []
    for utterance in artifact.get("utterances") or []:
        text = (utterance.get("text") or "").strip()
        if not text:
            continue
        ref = str(utterance.get("id") or "")
        stamp = format_timestamp(float(utterance.get("start") or 0.0))
        status = str(utterance.get("status") or "unresolved")
        speaker = utterance.get("speaker") or {}
        name = (speaker.get("character_name") or speaker.get("label") or "").strip()
        if status == "unresolved" or not name:
            head = VIEW_UNATTRIBUTED
        elif status == "auto_low":
            head = f"{name}?"
        else:
            head = name
        lines.append(f"[{ref} {stamp}] {head}: {text}")
    return lines


#: The stretch note, rendered before the lines of the chunk it belongs to. See
#: stretch_note for why it exists at all.
_REF = re.compile(r"^\[(u_\d+)\s")


def refs_in_lines(lines: list[str]) -> list[str]:
    """The utterance references a view chunk starts its lines with."""
    return [match.group(1) for line in lines if (match := _REF.match(line))]


def _ordinal(ref: str) -> int:
    try:
        return int(ref.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return -1


def stretch_note(artifact: dict[str, Any], lines: list[str]) -> str:
    """What the record says about where these lines happen, when it says anything.

    The extraction sees one line per utterance and a name only for whoever is
    SPEAKING, so a beat about somebody else had nothing to name itself with and
    came out as "un personaggio" - which is the one thing the prompt forbids. The
    two cases the reading can settle are exactly two:

    * a stretch where ONE party member is present and the reading names the
      others as elsewhere: everything a party member does or experiences in those
      lines is about that one member, and the writer is told to name them;
    * a stretch that puts a member elsewhere: nothing in those lines may be
      attributed to them.

    Only stretches that STATE an absence produce a note, in either direction.
    A reading that lists four members present is a reading of who stayed
    together, and it licenses nothing at all - which is why the presence
    questions built on it were retired (see docs/attribution-model.md S12.6).
    """
    refs = {_ordinal(ref) for ref in refs_in_lines(lines)}
    if not refs:
        return ""
    notes: list[str] = []
    for stretch in artifact.get("stretches") or []:
        absent = [str(name) for name in stretch.get("absent") or []]
        if not absent:
            continue
        lo, hi = _ordinal(str(stretch.get("from") or "")), _ordinal(
            str(stretch.get("to") or "")
        )
        if lo < 0 or hi < lo or not any(lo <= ref <= hi for ref in refs):
            continue
        place = str(stretch.get("place") or "").strip()
        where = f'"{place}"' if place else "this stretch"
        solo = stretch.get("solo_character")
        if solo:
            notes.append(
                f"A stretch of this session is {where}, and the only party member "
                f"present in it is {solo} ({', '.join(absent)} "
                f"{'is' if len(absent) == 1 else 'are'} elsewhere). So a narration "
                f"here that shows a party member acting or experiencing something "
                f"is about {solo}: NAME that member. Never write \"un "
                f'personaggio\" / "a character" / "someone" for them.'
            )
        else:
            present = [str(name) for name in stretch.get("present") or []]
            here = f" The party here: {', '.join(present)}." if present else ""
            notes.append(
                f"A stretch of this session is {where}, and the reading puts "
                f"{', '.join(absent)} elsewhere during it. Attribute NOTHING in "
                f"these lines to them.{here}"
            )
    if not notes:
        return ""
    return "[Stretches] " + " ".join(notes) + "\n\n"


def chunk_artifact(
    artifact: dict[str, Any], *, max_tokens: int, overlap: float
) -> list[list[str]]:
    """Chunk the attributed view (the content pass's input after the redesign)."""
    return chunk_lines(
        build_view_lines_from_artifact(artifact), max_tokens, overlap
    )


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
