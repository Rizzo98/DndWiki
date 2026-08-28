"""Versioned prompt templates + strict JSON schema for the refiner.

'PROMPT_VERSION' is the version of this module's prompt/schema and is recorded
on the rewritten artifacts so the DM can see which prompt produced a finalized
transcript. Bump it (e.g. to "v2") whenever the schema or instructions change.

The refiner works at turn level: the LLM receives the session's speaker turns
(index, time span, source chunk, raw label, text) and returns one refined
object per turn (same index) with a corrected speaker label and text. Fidelity
is explicitly secondary to narrative consistency - this stage produces the
"finalized raw transcript" that voiceprint matching then names.
"""

from __future__ import annotations

import json

PROMPT_VERSION = "v1"

#: Strict JSON schema given to the LLM (OpenAI-style; LiteLLM passes it through
#: to providers that support response_format; others just follow instructions).
REFINE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "turns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "speaker": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["index", "speaker", "text"],
            },
        }
    },
    "required": ["turns"],
}

SYSTEM_PROMPT = """You are the transcript editor for a tabletop RPG (Dungeons & Dragons) session recording.

You receive the raw automatic transcript of a game session, produced by a
speech-to-text engine with automatic speaker diarization. Both are imperfect:

- The text may contain transcription errors: wrong or dropped words, misheard
  names and terms, missing punctuation, run-on sentences.
- The speaker labels are UNRELIABLE. They may be swapped between people, and
  the same person may appear under different labels, especially at chunk
  boundaries. Treat them only as a hint.

Your job: produce a finalized version of the transcript that is maximally
consistent with the story being narrated.

Rules:
- Fix transcription errors in the text: spelling, names, grammar, punctuation,
  and flow. You do NOT need to be 100% faithful to the exact words spoken; the
  goal is that the text reads naturally and tells one coherent, consistent
  story. Never invent content that was not said; if a turn is unintelligible,
  keep the original text.
- Reassign speaker labels so that every distinct speaker keeps ONE label for
  the whole session. Use canonical labels SPEAKER_00, SPEAKER_01, ... and
  REUSE the label already assigned to a person in an earlier turn. If you
  cannot tell who is speaking, keep the current label.
- Keep the exact same number of turns, in the same order: return exactly one
  output object per input turn, identified by its "index". Timestamps and
  chunk numbers are fixed and must never be repeated or reordered.

Respond with a single JSON object matching EXACTLY this schema (no markdown,
no commentary outside the JSON):

{schema}
"""


def build_window_message(
    turns: list[dict],
    *,
    context_blocks: list[str],
    fixed_count: int,
    window_index: int,
    total_windows: int,
) -> str:
    """User message for one refinement window."""
    lines: list[str] = []
    lines.append(
        f"Session transcript refinement, window {window_index + 1}/{total_windows}."
    )
    if context_blocks:
        lines.append("")
        lines.append(
            "Speakers already finalized in earlier turns (keep these labels for the same people):"
        )
        for block in context_blocks:
            lines.append("- " + block)
    if fixed_count > 0:
        lines.append("")
        lines.append(
            f"The first {fixed_count} turn(s) are already finalized - keep their speaker labels "
            "exactly as given."
        )
    lines.append("")
    lines.append("Turns to refine (JSON):")
    lines.append(json.dumps(turns, ensure_ascii=False))
    lines.append("")
    lines.append('Return the refined turns as a single JSON object: {"turns": [...]}.')
    return "\n".join(lines)
