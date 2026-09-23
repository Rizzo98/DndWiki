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
from difflib import SequenceMatcher
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


def _match_key(value: Any) -> str:
    """How two paragraphs are recognised as the same one, labels aside."""
    return _clean(value).casefold()


def _places_of(stored: list[dict[str, str]], blocks: list[dict[str, str]]) -> list[str]:
    """One place label per block, taken from the blocks a text came from.

    The two lists are aligned by the paragraphs' own text, so a paragraph that
    the DM did not touch finds its block wherever it moved to (a paragraph
    inserted above it is an insertion, not a shift of everything below).
    """
    labels = [""] * len(blocks)
    matcher = SequenceMatcher(
        None,
        [_match_key(block["text"]) for block in stored],
        [_match_key(block["text"]) for block in blocks],
        autojunk=False,
    )
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            span = i2 - i1
        elif tag == "replace":
            # Reworded, not moved: the paragraphs still stand where they stood,
            # so the first ones keep the places of the blocks they replace.
            span = min(i2 - i1, j2 - j1)
        else:  # a paragraph that was inserted has no place of its own
            continue
        for offset in range(span):
            labels[j1 + offset] = stored[i1 + offset]["location"]
    return labels


def blocks_with_places(text: Any, previous: Any) -> list[dict[str, str]]:
    """Blocks from a replacement narrative, carrying the place labels over.

    The DM's client sends the narrative as the plain TEXT the page displays
    (the passages they highlight are quoted from it). A place label is block
    METADATA, so the text cannot carry it: rebuilding the blocks from the text
    alone blanks every label at once - and the summary-revision call, which is
    handed blocks and told to copy the ones it does not touch "verbatim, labels
    included", can only copy the labels it was given. That is how a correction
    to a single sentence used to arrive with the whole session re-labelled as
    "somewhere unknown".

    So the labels of the blocks the text came from are re-attached here: a
    paragraph that survived the DM's edit keeps its place, and a reworded one
    keeps the place of the block it stands in for, so that fixing a sentence
    does not cost its scene the name. A paragraph with no counterpart - text
    the DM added - is left unlabelled, which the format already reads as
    "continues the previous scene".
    """
    blocks = text_to_blocks(text)
    stored = normalize_blocks(previous)
    if not blocks or not stored:
        return blocks
    for block, label in zip(blocks, _places_of(stored, blocks)):
        block["location"] = label
    return blocks


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
