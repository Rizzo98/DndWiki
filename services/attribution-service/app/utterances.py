"""Layer 1: diarization segments -> utterances.

An utterance is the UNIT OF ATTRIBUTION. Everything downstream - the evidence
channels, the posterior, the wiki gate - operates on utterances, because a
diarization label cannot be attributed to a person at all
(docs/attribution-model.md S0, S13).

Two things this module has to get right, both of which the old code got wrong:

1. **Chunk boundaries are real.** `speaker_turns` compares `cur.chunk == chunk`
   with `==`, so when the segments carry no chunk key at all both sides are
   `None` and the boundary silently disappears: a same-label run merges across
   the whole file and two different people become one turn. When the caller
   knows where the chunks start (it assembled the list from several per-chunk
   responses) it MUST pass `chunk_boundaries`, and the builder treats those
   indices as hard boundaries.

2. **Provenance is recorded.** Every utterance keeps
   `segment_indices`, the exact diarization segments it was built from. The old
   `_rewrite_artifacts` mapped labels back onto transcript segments by exact
   `(start, end)` tuple equality, which silently dropped the label whenever two
   timings collided or drifted. Index-based provenance cannot drift.

Pure module: no database, no queue, no model. Every rule here is unit-tested.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from dnd_common.transcript import GAP_TOLERANCE_SEC, Turn, speaker_turns

#: Reference format echoed by the content extraction and stored on every fact
#: ('[u_00412]' in the prompt). Five digits covers a 27-hour session.
REF_FORMAT = "u_%05d"

#: A turn longer than this is cut into several utterances: the wiki gate, the
#: question generator and the review card all need a moment, not a monologue.
DEFAULT_MAX_CHARS = 320
#: Default silence tolerance when grouping segments into turns. Same value the
#: rest of the platform uses (dnd_common.transcript.GAP_TOLERANCE_SEC): turns
#: bound what a single person said without stopping, and are the unit the voice
#: evidence is pooled over.
DEFAULT_GAP_TOLERANCE = GAP_TOLERANCE_SEC
#: Segments shorter than this are still utterances (somebody said something),
#: they just cannot carry voice evidence of their own.
DEFAULT_MIN_SEC = 0.2

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"\S+")


def utterance_ref(ordinal: int) -> str:
    """The stable public reference of the ordinal-th utterance of a session."""
    return REF_FORMAT % ordinal


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Utterance:
    """One attributable unit of speech."""

    ordinal: int
    start: float
    end: float
    text: str
    diar_label: str | None = None
    diar_chunk: int | None = None
    diar_confidence: float | None = None
    asr_confidence: float | None = None
    segment_indices: tuple[int, ...] = ()
    gap_before_sec: float | None = None
    words: tuple[Word, ...] = ()

    @property
    def ref(self) -> str:
        return utterance_ref(self.ordinal)

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class UtteranceSet:
    """The utterances of one session plus the indexes the engine needs."""

    utterances: list[Utterance] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.utterances)

    def __iter__(self):
        return iter(self.utterances)

    @property
    def by_ref(self) -> dict[str, Utterance]:
        return {u.ref: u for u in self.utterances}

    def by_label(self) -> dict[str, list[Utterance]]:
        groups: dict[str, list[Utterance]] = {}
        for utterance in self.utterances:
            if utterance.diar_label:
                groups.setdefault(utterance.diar_label, []).append(utterance)
        return groups

    def indices_of_segment(self, segment_index: int) -> list[int]:
        """Which utterances were built from this diarization segment."""
        return [
            i
            for i, u in enumerate(self.utterances)
            if segment_index in u.segment_indices
        ]


# --- segment field readers --------------------------------------------------


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _mean(values: Sequence[float]) -> float | None:
    return float(sum(values) / len(values)) if values else None


def segment_words(segment: dict[str, Any]) -> tuple[Word, ...]:
    """The segment's word timings, when the backend reports them.

    Word timings are what let a later local re-diarization cut an utterance at a
    precise instant instead of guessing proportionally (S5.3).
    """
    words: list[Word] = []
    for raw in segment.get("words") or []:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("word") or raw.get("text") or "").strip()
        start = _float(raw.get("start"))
        end = _float(raw.get("end"))
        if not text or start is None or end is None or end < start:
            continue
        words.append(Word(text=text, start=start, end=end))
    return tuple(words)


def segment_text(segment: dict[str, Any]) -> str:
    return str(segment.get("text") or "").strip()


def diarization_confidence(segment: dict[str, Any]) -> float | None:
    """The diarizer's own confidence in this segment's speaker label.

    Only 'speaker_confidence' is read here. The bare 'confidence' key means the
    ASR confidence of the TEXT (see asr_confidence), and the legacy
    speaker-service history backfill reading it as a diarization confidence is a
    superseded mistake, not a convention to copy.
    """
    return _float(segment.get("speaker_confidence"))


def asr_confidence(segment: dict[str, Any]) -> float | None:
    return _float(segment.get("confidence"))


# --- splitting --------------------------------------------------------------


def _sentences(text: str) -> list[str]:
    parts = [part.strip() for part in _SENTENCE_END.split(text) if part.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def _pack(parts: list[str], max_chars: int) -> list[str]:
    """Greedily pack sentences into chunks of at most max_chars characters.

    A single sentence longer than the cap is kept whole: cutting mid-sentence
    would produce two utterances nobody can attribute independently, and the
    hook we show the DM would be a fragment.
    """
    if not parts:
        return []
    chunks: list[str] = []
    current = parts[0]
    for part in parts[1:]:
        candidate = f"{current} {part}"
        if len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current)
            current = part
    chunks.append(current)
    return chunks


def _time_for_text_fraction(utterance: Utterance, fraction: float) -> float:
    """The instant at which 'fraction' of the utterance's text has been said.

    Word timings when available, character proportion otherwise. This is the
    function that keeps a sub-split honest: cutting a turn into two utterances
    must land the boundary where the words actually change hands.
    """
    if utterance.words:
        total = sum(len(w.text) for w in utterance.words) or 1
        seen = 0
        for word in utterance.words:
            seen += len(word.text)
            if seen / total >= fraction:
                return min(max(word.end, utterance.start), utterance.end)
        return utterance.end
    span = utterance.end - utterance.start
    return min(utterance.start + span * fraction, utterance.end)


def _text_for_words(words: Sequence[Word], start: float, end: float) -> str:
    picked = [w.text for w in words if start <= (w.start + w.end) / 2 < end]
    return " ".join(picked).strip()


def split_utterance(
    utterance: Utterance, cut_sec: float, *, next_ordinal: int
) -> tuple[Utterance, Utterance]:
    """Cut one utterance in two at 'cut_sec', preserving the text by word timing.

    Used when local re-diarization finds that an utterance straddles two voices
    (S5.3): the audio evidence says two people spoke, so the attribution must be
    able to say so too. When the utterance has word timings the text is split on
    the words themselves; otherwise it falls back to a proportional character
    split, which is worse but never loses or duplicates text.
    """
    cut = min(max(cut_sec, utterance.start), utterance.end)
    if cut <= utterance.start or cut >= utterance.end:
        return utterance, Utterance(
            ordinal=next_ordinal,
            start=cut,
            end=cut,
            text="",
            diar_label=utterance.diar_label,
            diar_chunk=utterance.diar_chunk,
            diar_confidence=utterance.diar_confidence,
            asr_confidence=utterance.asr_confidence,
            segment_indices=utterance.segment_indices,
            words=(),
        )

    if utterance.words:
        first = _text_for_words(utterance.words, utterance.start, cut)
        second = _text_for_words(utterance.words, cut, utterance.end)
        if not first or not second:
            # The cut fell in a silence: fall back to the proportional split so
            # neither half comes back empty.
            first, second = _proportional_text(utterance.text, cut, utterance)
    else:
        first, second = _proportional_text(utterance.text, cut, utterance)

    head = Utterance(
        ordinal=utterance.ordinal,
        start=utterance.start,
        end=cut,
        text=first,
        diar_label=utterance.diar_label,
        diar_chunk=utterance.diar_chunk,
        diar_confidence=utterance.diar_confidence,
        asr_confidence=utterance.asr_confidence,
        segment_indices=utterance.segment_indices,
        gap_before_sec=utterance.gap_before_sec,
        words=tuple(w for w in utterance.words if w.end <= cut),
    )
    tail = Utterance(
        ordinal=next_ordinal,
        start=cut,
        end=utterance.end,
        text=second,
        diar_label=utterance.diar_label,
        diar_chunk=utterance.diar_chunk,
        diar_confidence=utterance.diar_confidence,
        asr_confidence=utterance.asr_confidence,
        segment_indices=utterance.segment_indices,
        gap_before_sec=0.0,
        words=tuple(w for w in utterance.words if w.start >= cut),
    )
    return head, tail


def _proportional_text(
    text: str, cut: float, utterance: Utterance
) -> tuple[str, str]:
    words = _WORD.findall(text)
    if not words:
        return "", ""
    span = utterance.end - utterance.start
    fraction = 0.0 if span <= 0 else (cut - utterance.start) / span
    index = min(max(1, round(len(words) * fraction)), len(words) - 1)
    return " ".join(words[:index]), " ".join(words[index:])


# --- the builder ------------------------------------------------------------


def _merge_segments(
    segments: list[dict[str, Any]], turn: Turn
) -> tuple[float | None, float | None]:
    diar = [
        c for c in (diarization_confidence(segments[i]) for i in turn.indices) if c is not None
    ]
    asr = [c for c in (asr_confidence(segments[i]) for i in turn.indices) if c is not None]
    return _mean(diar), _mean(asr)


def _piece(
    segments: list[dict[str, Any]],
    turn: Turn,
    indices: list[int],
    *,
    ordinal: int,
) -> Utterance:
    """One speaker turn, as a single utterance (before any length packing)."""
    text = " ".join(t for t in (segment_text(segments[i]) for i in indices) if t).strip()
    words = tuple(w for i in indices for w in segment_words(segments[i]))
    diar = [
        c for c in (diarization_confidence(segments[i]) for i in indices) if c is not None
    ]
    asr = [c for c in (asr_confidence(segments[i]) for i in indices) if c is not None]
    return Utterance(
        ordinal=ordinal,
        start=min(float(segments[i].get("start") or 0.0) for i in indices),
        end=max(float(segments[i].get("end") or 0.0) for i in indices),
        text=text,
        diar_label=turn.label,
        diar_chunk=turn.chunk,
        diar_confidence=_mean(diar),
        asr_confidence=_mean(asr),
        segment_indices=tuple(indices),
        gap_before_sec=None,
        words=words,
    )


def _turn_utterances(
    segments: list[dict[str, Any]],
    turn: Turn,
    *,
    start_ordinal: int,
    max_chars: int,
) -> list[Utterance]:
    """Cut one speaker turn into one or more utterances.

    Silence is already handled upstream: `speaker_turns` ends a turn when the
    gap exceeds the tolerance, so a pause inside a turn has, by definition, been
    tolerated as the same person still talking. What is left to cut on is
    LENGTH: a monologue of several sentences is not one attributable moment, and
    a question about it ("who decided to open the door?") would have no single
    answer.

    A turn that is short enough stays exactly one utterance, which is the common
    case and keeps ordinals stable for sessions that need no splitting.
    """
    piece = _piece(segments, turn, list(turn.indices), ordinal=start_ordinal)
    chunks = _pack(_sentences(piece.text), max_chars) if piece.text else []
    if len(chunks) <= 1:
        return [piece]

    out: list[Utterance] = []
    cursor = piece.start
    consumed = 0
    total_chars = len(piece.text) or 1
    for position, chunk_text in enumerate(chunks):
        consumed += len(chunk_text)
        end = (
            piece.end
            if position == len(chunks) - 1
            else _time_for_text_fraction(piece, min(1.0, consumed / total_chars))
        )
        end = max(end, cursor)
        out.append(
            Utterance(
                ordinal=start_ordinal + position,
                start=cursor,
                end=end,
                text=chunk_text,
                diar_label=turn.label,
                diar_chunk=turn.chunk,
                diar_confidence=piece.diar_confidence,
                asr_confidence=piece.asr_confidence,
                segment_indices=piece.segment_indices,
                gap_before_sec=None if position == 0 else 0.0,
                words=tuple(
                    w for w in piece.words if cursor <= (w.start + w.end) / 2 < end
                ),
            )
        )
        cursor = end
    return out


def build_utterances(
    segments: list[dict[str, Any]],
    *,
    chunk_boundaries: Iterable[int] | None = None,
    gap_tolerance: float = DEFAULT_GAP_TOLERANCE,
    max_chars: int = DEFAULT_MAX_CHARS,
    start_ordinal: int = 1,
) -> UtteranceSet:
    """Build the session's utterances from its diarization segments.

    Ordinals start at 1 so the first reference is 'u_00001'; they are assigned
    in chronological order and are stable for a given input, which is what makes
    a stored 'source_refs' meaningful across a recompute.
    """
    turns = speaker_turns(
        segments, gap_tolerance=gap_tolerance, chunk_boundaries=chunk_boundaries
    )
    utterances: list[Utterance] = []
    ordinal = start_ordinal
    for turn in turns:
        produced = _turn_utterances(
            segments, turn, start_ordinal=ordinal, max_chars=max_chars
        )
        # Only utterances that actually carry text are attributable; a segment
        # with no words still exists as audio but says nothing about identity.
        kept = [u for u in produced if u.text]
        utterances.extend(kept)
        ordinal += len(kept)

    for index, utterance in enumerate(utterances):
        if utterance.ordinal != index + start_ordinal:
            utterances[index] = Utterance(
                ordinal=index + start_ordinal,
                start=utterance.start,
                end=utterance.end,
                text=utterance.text,
                diar_label=utterance.diar_label,
                diar_chunk=utterance.diar_chunk,
                diar_confidence=utterance.diar_confidence,
                asr_confidence=utterance.asr_confidence,
                segment_indices=utterance.segment_indices,
                gap_before_sec=utterance.gap_before_sec,
                words=utterance.words,
            )
    return UtteranceSet(utterances=utterances)


def gap_before(utterances: Sequence[Utterance]) -> list[Utterance]:
    """Recompute 'gap_before_sec' against the previous KEPT utterance.

    The turn builder cannot know which of its outputs survive; the continuity
    channel needs the real silence before each utterance, not the silence before
    the segment it happened to come from.
    """
    out: list[Utterance] = []
    previous_end: float | None = None
    for utterance in utterances:
        gap = None if previous_end is None else round(utterance.start - previous_end, 3)
        out.append(
            Utterance(
                ordinal=utterance.ordinal,
                start=utterance.start,
                end=utterance.end,
                text=utterance.text,
                diar_label=utterance.diar_label,
                diar_chunk=utterance.diar_chunk,
                diar_confidence=utterance.diar_confidence,
                asr_confidence=utterance.asr_confidence,
                segment_indices=utterance.segment_indices,
                gap_before_sec=gap,
                words=utterance.words,
            )
        )
        previous_end = max(previous_end or 0.0, utterance.end)
    return out
