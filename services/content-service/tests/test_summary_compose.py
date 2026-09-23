"""The summary composition pass: the beats, the prompt, and the fallback.

The extraction pass writes 1-3 beats per chunk and never sees the other chunks,
so what the merger produces is a list of separate moments with no train of
thought - and a being can be "una creatura" in one beat and named in the next,
as if they were two. One extra call sees the whole session and writes the STORY
from it, in scene blocks the DM can highlight portions of (app/summary.py);
these tests are about that call's contract, the material it is given, and never
losing the beats when it fails.
"""

import pytest

from app.prompts import (
    SUMMARY_COMPOSE_SYSTEM_PROMPT,
    build_summary_compose_message,
)
from app.workers.generate import MIN_SUMMARY_LINES_TO_COMPOSE, _compose_summary


class FakeLLM:
    def __init__(self, composed=None, error=None):
        self.composed = composed
        self.error = error
        self.calls = []

    async def compose_summary(self, current, *, language=None, scenes=None):
        self.calls.append({"current": current, "language": language, "scenes": scenes})
        if self.error is not None:
            raise self.error
        return [dict(block) for block in self.composed or []]


def merged(lines, *, events=None, timeline=None, locations=None, characters=None, language="it"):
    return {
        "language": language,
        "session_summary": "\n".join(lines),
        "characters": characters or [],
        "locations": locations or [],
        "events": events or [],
        "timeline_entries": timeline or [],
    }


# --- the call ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_story_of_several_beats_is_composed():
    llm = FakeLLM(
        composed=[
            {"location": "Locanda del Fumo Aspro", "text": "La sessione si apre nella locanda."},
            {"location": "", "text": "Poi il gruppo esce."},
        ]
    )
    out = await _compose_summary(llm, merged([f"beat {index}" for index in range(5)]))
    assert out == [
        {"location": "Locanda del Fumo Aspro", "text": "La sessione si apre nella locanda."},
        {"location": "", "text": "Poi il gruppo esce."},
    ]
    assert llm.calls[0]["language"] == "it"


@pytest.mark.asyncio
async def test_a_single_beat_is_left_alone():
    """One beat IS the session: the call would be a cost with nothing to buy, and
    every extra LLM call is a chance to invent a fact."""
    llm = FakeLLM(composed=[{"location": "", "text": "should not be used"}])
    short = merged(["one", "two"][: MIN_SUMMARY_LINES_TO_COMPOSE - 1])
    assert await _compose_summary(llm, short) == []
    assert llm.calls == []


@pytest.mark.asyncio
async def test_a_failed_composition_keeps_the_beats():
    """The DM's review must not depend on this call succeeding: the merged beats
    are exactly what they saw before the pass existed."""
    llm = FakeLLM(error=RuntimeError("model unavailable"))
    assert await _compose_summary(llm, merged([f"beat {index}" for index in range(5)])) == []


@pytest.mark.asyncio
async def test_an_empty_composition_keeps_the_beats():
    llm = FakeLLM(composed=[])
    assert await _compose_summary(llm, merged([f"beat {index}" for index in range(5)])) == []


# --- the material the model is given ----------------------------------------


def test_the_message_carries_the_beats_the_events_and_the_timeline():
    message = build_summary_compose_message(
        merged(
            ["the party enters the tavern", "a fight breaks out"],
            events=[{"title": "The tavern brawl", "description": "a fight with the guards"}],
            timeline=[{"label": "the guards arrive", "timestamp": "00:12:30"}],
        )
    )
    assert "  - the party enters the tavern" in message
    assert "  - a fight breaks out" in message
    assert "The tavern brawl: a fight with the guards" in message
    assert "00:12:30 the guards arrive" in message
    assert "it" in message


def test_the_message_survives_a_thin_extraction():
    message = build_summary_compose_message(merged(["one beat"]))
    assert "  - one beat" in message
    assert "Events:" not in message


def test_the_message_carries_the_places_and_the_scene_reading():
    """Where the session happens: the places the extraction found, and the
    engine's own reading when it ran (which is what the block labels and the
    'somewhere else' exclusions come from)."""
    message = build_summary_compose_message(
        merged(
            ["a beat"],
            locations=[{"name": "Locanda del Fumo Aspro"}, {"name": "Fatumastra"}],
        ),
        scenes=[
            {
                "place": "Locanda del Fumo Aspro",
                "present": ["Hann Caleto"],
                "absent": ["Galgith Baurd"],
            }
        ],
    )
    assert "Places mentioned in this session:" in message
    assert "  - Locanda del Fumo Aspro" in message
    assert "  - Fatumastra" in message
    assert "Where the session happens" in message
    assert "there: Hann Caleto" in message
    assert "somewhere else: Galgith Baurd" in message


def test_the_message_has_no_scene_section_without_the_engine():
    message = build_summary_compose_message(merged(["a beat"]))
    assert "Where the session happens" not in message


def test_the_message_lists_the_cast_so_one_being_keeps_one_name():
    """The beats are written one at a time, so the same being can be "una
    creatura piumata" in one and named in the next. The cast is what the story
    has to call them."""
    message = build_summary_compose_message(
        merged(["a beat"], characters=[{"name": "Hann Caleto"}, {"name": "Galgith Baurd"}])
    )
    assert "Characters in this session" in message
    assert "never list them" in message
    assert "  - Hann Caleto" in message
    assert "  - Galgith Baurd" in message


def test_the_prompt_asks_for_one_connected_story():
    """The DM's complaint: the summary read as independent sentences, so the
    same being was "una creatura" in one and named in the next. The prompt has
    to ask for one continuous narrative, in blocks that follow the places."""
    prompt = SUMMARY_COMPOSE_SYSTEM_PROMPT
    assert "FIRST block opens the session" in prompt
    assert "ONE CONTINUOUS TEXT" in prompt
    assert "CONNECT everything" in prompt
    assert "START A NEW BLOCK" in prompt
    # the creature/Hann Caleto case, spelled out
    assert "ONE BEING, ONE NAME" in prompt
    assert "creature IS Hann Caleto" in prompt
    assert "Never invent" in prompt

# --- the span each beat came from (v18) --------------------------------------


def test_beats_are_rendered_with_the_span_they_came_from():
    """The span is the only thing here the merger could not supply by comparing
    text, and it is what lets this call tell one moment recorded twice - the
    chunks overlap, so a scene on a boundary is described from both sides - from
    two moments that merely look alike."""
    current = merged(["a girl brings lunch to her uncle"])
    current["summary_beats"] = [
        {"text": "a girl brings lunch to her uncle", "from": "u_00300", "to": "u_00435"},
        {"text": "a dwarf brings lunch to her uncle", "from": "u_00436", "to": "u_00595"},
    ]
    message = build_summary_compose_message(current)
    assert "  - [u_00300-u_00435] a girl brings lunch to her uncle" in message
    assert "  - [u_00436-u_00595] a dwarf brings lunch to her uncle" in message
    assert "the part of the session it came from" in message
    # the list must not read as an output format: a numbered list got echoed
    # into the narrative, numbers and all, the first time this shipped
    assert "1. [u_00300" not in message
    assert "NOTES for you to write from" in message


def test_a_summary_without_provenance_falls_back_to_the_plain_lines():
    """A summary reloaded from the database has no beats-with-spans. The call
    then works as it did before v18 rather than being handed a wrong span."""
    message = build_summary_compose_message(merged(["one beat", "two beats"]))
    assert "  - one beat" in message
    assert "  - two beats" in message
    # no span was invented for beats that have none
    assert "[u_" not in message


def test_a_beat_with_no_span_is_rendered_without_brackets():
    current = merged(["a beat"])
    current["summary_beats"] = [{"text": "a beat", "from": "", "to": ""}]
    assert "  - a beat" in build_summary_compose_message(current)


def test_the_scene_reading_is_flagged_as_another_language():
    """Its place names come from the attribution engine, whose prompt is in
    English, and the labels must be written in the table's language."""
    message = build_summary_compose_message(
        merged(["a beat"]), scenes=[{"place": "the hospital in the city", "present": []}]
    )
    assert "the hospital in the city" in message  # still given: it says WHERE
    assert "NOT in the session's language" in message
    assert "NOT what to call those places" in message


def test_the_prompt_builds_the_merge_rule_on_the_spans():
    assert "EVERY BEAT CARRIES THE SPAN" in SUMMARY_COMPOSE_SYSTEM_PROMPT
    assert "they are ONE moment seen twice" in SUMMARY_COMPOSE_SYSTEM_PROMPT
    # the spans are a cut of the transcript, not a structure for the prose: the
    # first version of this rule said "never write them as one scene", and the
    # composer answered with one block per span and no story at all
    assert "The spans are a mechanical cut of the transcript" in SUMMARY_COMPOSE_SYSTEM_PROMPT
    assert "never mentioned in it" in SUMMARY_COMPOSE_SYSTEM_PROMPT
    assert "no sentence" in SUMMARY_COMPOSE_SYSTEM_PROMPT


def test_the_prompt_forbids_copying_the_record_language_into_a_label():
    assert "A 'location' LABEL IS WRITTEN IN THAT SAME LANGUAGE" in (
        SUMMARY_COMPOSE_SYSTEM_PROMPT
    )
    assert "never copy its words into a label" in SUMMARY_COMPOSE_SYSTEM_PROMPT
