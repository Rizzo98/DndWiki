"""Pure LLM-refinement logic (no I/O/LLM deps) - testable without litellm.

The refiner works at *turn* level (see dnd_common.transcript.speaker_turns):
segments are grouped into speaker turns, the LLM fixes each turn's text and
speaker label, and the corrections are mapped back onto the original segments,
preserving every segment's start/end/chunk. That is what keeps downstream
consumers working unchanged: voiceprint matching slices audio by time, the
transcript UI seeks by time, and content-service chunks by segment text.

Long sessions are refined in sliding windows (see 'turn_windows'); windows
overlap so the LLM sees already-finalized turns and reuses their labels across
the boundary (see 'build_context_blocks').
"""

from __future__ import annotations

import re
from typing import Any

from dnd_common.transcript import Turn

# Canonical label scheme (matches the on-prem pyannote convention downstream
# consumers/tests already expect).
LABEL_FORMAT = "SPEAKER_%02d"

_CANONICAL = re.compile(r"^SPEAKER_(\d+)$", re.IGNORECASE)


class RefineError(Exception):
    """The LLM returned something that is not usable refinement JSON."""


def turn_text(segments: list[dict[str, Any]], turn: Turn) -> str:
    """Join a turn's segment texts into a single utterance string."""
    return " ".join((segments[i].get("text") or "").strip() for i in turn.indices).strip()


def _numeric_confidence(value: Any) -> bool:
    """True for a usable 0..1 confidence (int/float, not bool)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def turn_view(turns: list[Turn], segments: list[dict[str, Any]], index: int) -> dict[str, Any]:
    """The LLM-facing view of one turn (index, time span, chunk, raw label, text).

    The source segments may carry an optional per-sentence ASR probability
    ("confidence", 0..1) when the transcription backend provides one (e.g.
    AssemblyAI per-utterance confidence). When present it is surfaced as:
      - "confidence": the turn-level mean, so the LLM knows how sure the
        speech engine was about this utterance as a whole;
      - "sentences": the per-segment text + confidence breakdown (only when
        the turn groups several segments), so the LLM can judge, sentence
        by sentence, whether the context can raise that probability.
    Backends without confidence data simply omit both keys.
    """
    turn = turns[index]
    view = {
        "index": index,
        "start": round(turn.start, 3),
        "end": round(turn.end, 3),
        "chunk": turn.chunk,
        "speaker": turn.label,
        "text": turn_text(segments, turn),
    }
    confidences = [
        float(segments[i].get("confidence"))
        for i in turn.indices
        if _numeric_confidence(segments[i].get("confidence"))
    ]
    if confidences:
        view["confidence"] = round(sum(confidences) / len(confidences), 3)
        if len(turn.indices) > 1:
            view["sentences"] = [
                {
                    "text": (segments[i].get("text") or "").strip(),
                    "confidence": segments[i].get("confidence")
                    if _numeric_confidence(segments[i].get("confidence"))
                    else None,
                }
                for i in turn.indices
            ]
    return view


def build_cast_block(members: list[dict[str, Any]]) -> list[str]:
    """Compact per-member cast lines for the LLM prompt.

    One line per member with a character name: "character (player) — physical
    description". The DM row is tagged so the LLM knows one speaker is the
    narrator. Members without a character name (e.g. freshly invite-joined
    players) are skipped; descriptions are truncated to keep the window lean.
    """
    lines: list[str] = []
    for member in members or []:
        character = (member.get("character_name") or "").strip()
        if not character:
            continue
        player = (member.get("player_name") or "").strip()
        suffix = " (dm, narrator)" if member.get("role") == "dm" else ""
        desc = (member.get("character_description") or "").strip()
        if desc:
            desc = " ".join(desc.split())[:200]
            lines.append(f"- {character} ({player}){suffix} — {desc}")
        else:
            lines.append(f"- {character} ({player}){suffix}")
    return lines


def turn_windows(n_turns: int, window: int, overlap: int) -> list[tuple[int, int, int]]:
    """Sliding windows over turn indexes 0..n_turns-1.

    Returns (start, end, fixed) triples. 'fixed' is the number of leading
    turns that are already-finalized context (the overlap with the previous
    window): their labels must be preserved and their decisions ignored, which
    is how speaker identity carries across windows.
    """
    if n_turns <= 0:
        return []
    step = max(1, window - overlap)
    windows: list[tuple[int, int, int]] = []
    start = 0
    while start < n_turns:
        end = min(n_turns, start + window)
        fixed = overlap if start > 0 else 0
        windows.append((start, end, fixed))
        if end >= n_turns:
            break
        start += step
    return windows


def build_context_blocks(
    turns: list[Turn], segments: list[dict[str, Any]], decisions: dict[int, tuple[str, str]]
) -> list[str]:
    """A compact per-label "rolodex" of already-finalized speakers.

    Helps the LLM reuse labels across windows even when a speaker does not
    appear in the overlap turns: each block shows the label and the last words
    that speaker said so far (first-appearance order).
    """
    last: dict[str, tuple[int, str]] = {}
    for index, (label, text) in sorted(decisions.items()):
        last[label] = (index, text)
    blocks: list[str] = []
    for label in sorted(last, key=lambda lbl: last[lbl][0]):
        _, text = last[label]
        snippet = " ".join(text.split())[:160]
        blocks.append(f'{label}: "{snippet}"' if snippet else f"{label}: (no text)")
    return blocks


def parse_decisions(raw: Any) -> dict[int, tuple[str, str]]:
    """Normalize one LLM response into turn index -> (speaker, text).

    Lenient on count: turns missing from the response simply have no decision
    (the caller falls back to their original label/text) and unknown indexes
    are ignored. Raises RefineError when the response is structurally
    unusable (not an object with a 'turns' list).
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("turns"), list):
        raise RefineError("LLM output must be a JSON object with a 'turns' list")
    decisions: dict[int, tuple[str, str]] = {}
    for item in raw["turns"]:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        speaker = str(item.get("speaker") or "").strip()
        text = str(item.get("text") or "").strip()
        decisions[index] = (speaker, text)
    return decisions


def canonicalize(
    turns: list[Turn], decisions: dict[int, tuple[str, str]]
) -> dict[int, tuple[str, str]]:
    """Map the LLM's speaker strings onto canonical SPEAKER_XX labels.

    Walked in session order so first-appearance numbering is stable: the same
    string always maps to the same canonical label, preserving the LLM's
    consistency decisions. Well-formed SPEAKER_N labels pass through as-is
    (normalized zero-padding); any other string gets a fresh canonical label
    in first-appearance order. Turns without a decision keep their raw label
    and original text.
    """
    first_seen: dict[str, str] = {}
    out: dict[int, tuple[str, str]] = {}
    for index, turn in enumerate(turns):
        label, text = decisions.get(index, (None, None))
        if not label:
            label = turn.label
        text = (text or "").strip()
        key = label.strip().upper()
        match = _CANONICAL.match(key)
        if match:
            canonical = LABEL_FORMAT % int(match.group(1))
        elif key in first_seen:
            canonical = first_seen[key]
        else:
            canonical = LABEL_FORMAT % len(first_seen)
            first_seen[key] = canonical
        out[index] = (canonical, text)
    return out


def split_text(text: str, weights: list[int]) -> list[str]:
    """Split 'text' into len(weights) chunks proportional to the weights.

    Splits on word boundaries, greedily packing words until the proportional
    target is reached; the last chunk takes the remainder so every word is
    consumed. Each non-final chunk gets at least one word when words allow.
    Empty text yields one empty string per chunk.
    """
    words = text.split()
    n = len(weights)
    if not words:
        return [""] * n
    if n == 1:
        return [" ".join(words)]

    total = sum(max(w, 1) for w in weights)
    targets: list[int] = []
    remaining_words = len(words)
    remaining_weight = total
    for i in range(n):
        if i == n - 1:
            targets.append(remaining_words)
        else:
            target = max(1, round(remaining_words * max(weights[i], 1) / remaining_weight))
            # leave at least one word for every later chunk when possible
            target = min(target, remaining_words - (n - 1 - i))
            targets.append(target)
        remaining_words -= targets[-1]
        remaining_weight -= max(weights[i], 1)

    out: list[str] = []
    start = 0
    for target in targets:
        out.append(" ".join(words[start : start + target]))
        start += target
    return out


def apply_refined(
    segments: list[dict[str, Any]],
    turns: list[Turn],
    decisions: dict[int, tuple[str, str]],
) -> list[dict[str, Any]]:
    """Map turn-level decisions back onto the segments (labels + text).

    Segment timings (start/end/chunk) and every other field are preserved;
    only 'speaker' and 'text' change. The corrected turn text is re-split
    across the turn's segments proportionally to their original text lengths
    (an empty correction keeps the original wording). Segments not covered by
    any turn (unlabeled / zero duration) are untouched.
    """
    new_segments = [dict(seg) for seg in segments]
    for index, turn in enumerate(turns):
        decision = decisions.get(index)
        if decision is None:
            continue
        label, text = decision
        turn_segments = [new_segments[i] for i in turn.indices]
        weights = [len((seg.get("text") or "").strip()) for seg in turn_segments]
        pieces = split_text(text, weights) if text else [
            (seg.get("text") or "").strip() for seg in turn_segments
        ]
        for si, piece in zip(turn.indices, pieces):
            new_segments[si]["speaker"] = label
            new_segments[si]["text"] = piece
    return new_segments


def rewrite_transcript(
    transcript: dict[str, Any], refined_segments: list[dict[str, Any]]
) -> dict[str, Any]:
    """Apply the refined labels/text onto transcript.json by (start, end).

    diarization.json and transcript.json are built from the same segment list,
    but matching by time span (like speaker-service) is robust to any drift in
    key order or extra fields.
    """
    new_transcript = dict(transcript)
    span: dict[tuple[float, float], tuple[str, str]] = {}
    for seg in refined_segments:
        start, end = seg.get("start"), seg.get("end")
        if start is not None and end is not None:
            span[(float(start), float(end))] = (seg.get("speaker"), seg.get("text"))
    new_segments: list[dict[str, Any]] = []
    for seg in new_transcript.get("segments") or []:
        item = dict(seg)
        key = (float(seg.get("start")), float(seg.get("end")))
        if key in span:
            speaker, text = span[key]
            item["speaker"] = speaker
            item["text"] = text
        new_segments.append(item)
    new_transcript["segments"] = new_segments
    return new_transcript