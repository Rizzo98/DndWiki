"""Transcript chunking: overlapping chunks of the compact speaker view.

The worker builds one line per transcript segment ('[HH:MM:SS] NAME: text')
and greedily groups lines into token-bounded chunks with a configurable
overlap. Lines are never split mid-segment, and a single oversized segment
is kept whole (the LLM still extracts from it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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


def markers_in_lines(lines: list[str]) -> set[str]:
    """Every reference the given view lines actually offer.

    This is the set a citation may be drawn from: 'u_00412' on the attributed
    view, '00:41:15' on the diarized one. Anything else is invented, which is what
    a model does when it is asked to cite lines that carry no reference at all.
    """
    return {marker for line in lines if (marker := line_marker(line))}


def chunk_artifact(
    artifact: dict[str, Any], *, max_tokens: int, overlap: float
) -> list[list[str]]:
    """Chunk the attributed view (the content pass's input after the redesign)."""
    return chunk_lines(
        build_view_lines_from_artifact(artifact), max_tokens, overlap
    )


def chunk_ranges(
    lines: list[str],
    max_tokens: int,
    overlap: float,
) -> list[tuple[int, int]]:
    """Greedily group view lines into overlapping chunks: one [start, end) each.

    The primitive, because the indices carry information the line lists do not:
    a chunk's END is where the next chunk's overlap begins, and that boundary is
    what owned_ranges needs to say which part of the session a chunk narrates.

    The next chunk starts 'overlap' of the way back into the previous one (at
    least one line), so entities near a boundary are seen twice. Ends strictly
    increase, so the ranges advance through the session even for one-line chunks.
    """
    if not lines:
        return []
    tokens = [estimate_tokens(line) for line in lines]
    ranges: list[tuple[int, int]] = []
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
        ranges.append((start, end))
        if end >= n:
            break  # everything is covered; no new content for another chunk
        count = end - start
        overlap_count = max(1, round(count * overlap))
        start = max(start + 1, end - overlap_count)
    return ranges


def chunk_lines(
    lines: list[str],
    max_tokens: int,
    overlap: float,
) -> list[list[str]]:
    """Greedily group view lines into overlapping chunks.

    The next chunk starts 'overlap' of the way back into the previous one
    (at least one line), so entities near a boundary are seen twice.
    """
    return [lines[start:end] for start, end in chunk_ranges(lines, max_tokens, overlap)]


def owned_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The part of the session each chunk is responsible for NARRATING.

    The overlap exists so that an entity near a boundary is seen twice, and for
    characters, locations, events and timeline entries that is exactly right. For
    the session's STORY it does the opposite of what it is for: two writers, each
    shown one half of the same scene, describe that scene twice in different words,
    and the merge step cannot tell that the two lines are the same moment because
    it de-duplicates on identical text. That is how one NPC became two people in
    one sentence:

        "incontra una ragazza che porta il pranzo a suo zio, e sempre li fuori
         finge di essere la famiglia ... e a una nana che porta il pranzo allo
         zio primario"

    So the BEATS are partitioned instead: chunk k owns the lines from where the
    previous chunk stopped to where it stops itself. Those ranges TILE the session
    exactly - the last owned line of one chunk is the line before the next chunk's
    first, because chunk k+1 begins by repeating the tail of chunk k. Every moment
    is therefore written by exactly one chunk, while every line is still seen by
    two of them for everything that benefits from the overlap.
    """
    if not ranges:
        return []
    owned: list[tuple[int, int]] = [ranges[0]]
    for index in range(1, len(ranges)):
        owned.append((ranges[index - 1][1], ranges[index][1]))
    return owned


#: The legacy view opens a line with its time ('[00:41:15] Aramil: ...').
_STAMP_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]")


def line_marker(line: str) -> str:
    """The identifier a view line opens with: its utterance ref, else its time.

    The attributed view marks every line '[u_00412 00:41:15] ...' and the legacy
    view '[00:41:15] ...'. Either one names the line a chunk's ownership starts at,
    which is all the extraction prompt needs to say "your part starts here".
    """
    ref = _REF.match(line)
    if ref:
        return ref.group(1)
    stamp = _STAMP_RE.match(line)
    return stamp.group(1) if stamp else ""


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

# --------------------------------------------------------------------------
# how much of the story one chunk owes
# --------------------------------------------------------------------------
#
# v16 partitioned the beats and the partition alone was not enough, for a reason
# the fixture showed plainly: the pipeline asked every chunk for "1-3 SHORT
# lines", so five chunks owed the session fifteen beats - about one per three and
# a half minutes - and a chunk whose part ended with a scene simply left that
# scene out rather than describing it twice. Losing a scene is a different defect
# from splitting a person, not a fix for it.
#
# The budget is therefore proportional to the work: a chunk that owns a quarter
# of an hour owes more beats than one that owns five minutes.

#: Transcript lines per beat, calibrated on the fixture session (649 lines over
#: 52 minutes, so ~5s a line): ~25 lines is a couple of minutes of play, which is
#: about one moment. The stored timeline of that session has 46 entries, so this
#: is still a compression, just not a tenfold one.
LINES_PER_BEAT = 25

#: No part is ever told to write fewer than this, or more than this, however
#: small or large it is. The floor keeps a short part from vanishing; the ceiling
#: keeps a long one from drowning the composer, whose only job is to weave the
#: beats into a story.
MIN_BEATS = 3
MAX_BEATS = 10


def beat_budget(lines: int, *, lines_per_beat: int = LINES_PER_BEAT) -> tuple[int, int]:
    """The number of beats a part of 'lines' transcript lines should be told in.

    'lines_per_beat' is the compression this run applies and it is a SETTING
    (core.config.beat_lines), not a constant, because it is the one lever left on
    the length of the finished summary: the composer writes what the beats carry,
    so a session distilled into 14 moments produces a shorter story than the same
    session distilled into 28. The default is the value this pipeline has always
    used; raising it is the experiment.

    Returns (low, high). A RANGE, not a number, because how many moments a
    stretch of play contains is a judgement about the recording and not
    arithmetic: the count is a scale for that judgement to sit on.
    """
    target = max(MIN_BEATS, min(MAX_BEATS, round(lines / max(1, lines_per_beat))))
    low = max(MIN_BEATS, target - 1)
    return low, max(low, min(MAX_BEATS, target + 1))


@dataclass(frozen=True)
class OwnedPart:
    """The slice of a session one chunk narrates: its span, and how big it is.

    'start' and 'end' are line markers - the utterance refs, or the timestamps on
    the legacy view. 'start' is what the extraction prompt quotes back at the
    model so the chunk knows where its part begins, and 'lines' is what turns the
    beat budget from a flat guess into something proportional.

    The whole SPAN is what the composer is given with every beat, and that is a
    separate job: it is how the composer can tell a moment the session recorded
    twice from two moments, which the merger cannot do because it only ever sees
    text. The owned spans of two chunks never overlap (see owned_ranges), so
    "beats with different spans" means "beats written by readings that could not
    see each other".
    """

    start: str
    end: str
    lines: int


def owned_parts(lines: list[str], ranges: list[tuple[int, int]]) -> list[OwnedPart]:
    """One OwnedPart per chunk: the slice of the session it narrates.

    Every chunk gets a real start marker. Whether a chunk has anything ABOVE it
    to exclude is a question about its position among the chunks, not about its
    span, so the prompt decides that from the chunk index.
    """
    return [
        OwnedPart(
            start=line_marker(lines[start]),
            end=line_marker(lines[end - 1]),
            lines=end - start,
        )
        for start, end in owned_ranges(ranges)
    ]
