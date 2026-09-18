"""The session summary as a narrative in SCENE BLOCKS.

The summary is the layer the DM reviews, and it used to be a list of
independent one-beat lines: every sentence was extracted on its own, so a line
could say "Hann Caleto si risveglia in una gabbia" and the next one "il gruppo
libera una creatura piumata" as if the creature were somebody else. A story
told one sentence at a time is not a story.

So the summary is prose now: one narrative, written by the compose call, in
blocks that follow the session from place to place.

    [{"location": "Locanda del Fumo Aspro", "text": "La sessione si apre ..."},
     {"location": "", "text": "Fuori, ..."},
     {"location": "Ospedale di Fatumastra", "text": "All'ospedale ..."}]

- 'text' is a paragraph of the story. Consecutive blocks are read as one
  continuous text, so the sentences have to carry over from one to the next.
- 'location' is the place that portion of the story happens in; empty when the
  material does not say (the block then simply continues the previous scene).
  The session page renders it as a small place chip above the paragraph - this
  is where the old "Where this session happens" reading lives now.

'blocks_to_text' is the plain text every other consumer sees (the review API
and the DM's selection targets work on text): the blocks joined by blank
lines, which is exactly what the DM highlights portions of.
"""

from __future__ import annotations

import re
from typing import Any

#: A leading list marker ("- ", "* ", "• ") never belongs in a paragraph of
#: the story: models write one when they answer with a list despite the
#: contract, and the DM would read it as part of the text.
_BULLET = re.compile("^[-*\u2022]\\s+")

#: Most blocks one summary may hold (a session is a handful of scenes; the cap
#: only exists so a model stuck in a loop cannot flood the row).
MAX_SUMMARY_BLOCKS = 40

#: Most characters one block may hold. A "scene" that is longer than this is
#: prose the DM cannot review, not a scene.
MAX_BLOCK_CHARS = 4000

#: How the blocks are joined into the text the rest of the pipeline reads.
BLOCK_SEPARATOR = "\n\n"

#: Keys a block may carry: 'location' is metadata, 'text' is the story.
BLOCK_KEYS = ("location", "text")


def _clean(value: Any) -> str:
    """Collapse whitespace, drop a leading list marker."""
    return _BULLET.sub("", " ".join(str(value or "").split()))


def normalize_blocks(value: Any) -> list[dict[str, str]]:
    """Whatever the model returned -> the canonical block list.

    Accepts a list of blocks (the contract), a list of plain strings (a model
    that dropped the labels) and a plain string (an old summary, or a model
    that answered with prose): the point is that ONE malformed shape must never
    lose the DM's summary. Blocks without text are dropped, an empty list stays
    empty, and 'location' comes back as "" when it is missing.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return text_to_blocks(value)
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []

    blocks: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            text = _clean(item)
            location = ""
        elif isinstance(item, dict):
            text = _clean(item.get("text"))
            location = _clean(item.get("location"))
        else:
            continue
        if not text:
            continue
        blocks.append({"location": location, "text": text[:MAX_BLOCK_CHARS]})
        if len(blocks) >= MAX_SUMMARY_BLOCKS:
            break
    return blocks


def text_to_blocks(text: Any) -> list[dict[str, str]]:
    """A plain narrative -> blocks, one per paragraph.

    Used for summaries stored before the blocks existed (and for the model
    answer that is prose and nothing else): the DM still sees the story they
    were reading, with no place labels attached.
    """
    normalized = str(text or "").replace("\r\n", "\n")
    separator = BLOCK_SEPARATOR if BLOCK_SEPARATOR in normalized else "\n"
    # A single newline means the old one-beat-per-line draft: it still has to
    # render as a readable story rather than one enormous paragraph.
    paragraphs = normalized.split(separator)
    return [{"location": "", "text": _clean(p)} for p in paragraphs if _clean(p)]


def blocks_to_text(blocks: Any) -> str:
    """The narrative as one text: the form the review works on."""
    return BLOCK_SEPARATOR.join(block["text"] for block in normalize_blocks(blocks))


def summary_from(value: Any) -> tuple[list[dict[str, str]], str]:
    """(blocks, text) for whatever a model, a row or a request carried."""
    blocks = normalize_blocks(value)
    return blocks, blocks_to_text(blocks)


def describe_blocks(value: Any) -> str:
    """One log-sized line: how many blocks, and in which places."""
    blocks = normalize_blocks(value)
    if not blocks:
        return "no summary"
    places = [block["location"] or "?" for block in blocks]
    return f"{len(blocks)} block(s) in {', '.join(places)}"
