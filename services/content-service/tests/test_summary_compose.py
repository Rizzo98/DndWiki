"""The summary composition pass: the beats, the prompt, and the fallback.

The extraction pass writes 1-3 beats per chunk and never sees the other chunks,
so what the merger produces is a list of separate moments - "a patch of sentences
with no train of thought", in the DM's words. One extra call sees the whole
session and rewrites them into a story; these tests are about that call's
contract and about never losing the beats when it fails.
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

    async def compose_summary(self, current, *, language=None):
        self.calls.append({"current": current, "language": language})
        if self.error is not None:
            raise self.error
        return list(self.composed or [])


def merged(lines, *, events=None, timeline=None, language="it"):
    return {
        "language": language,
        "session_summary": "\n".join(lines),
        "events": events or [],
        "timeline_entries": timeline or [],
    }


# --- the call ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_story_of_several_beats_is_composed():
    llm = FakeLLM(composed=["La sessione si apre nella locanda.", "Poi il gruppo esce."])
    out = await _compose_summary(
        llm, merged([f"beat {index}" for index in range(5)])
    )
    assert out == ["La sessione si apre nella locanda.", "Poi il gruppo esce."]
    assert llm.calls[0]["language"] == "it"


@pytest.mark.asyncio
async def test_a_short_summary_is_left_alone():
    """Two beats are already a story: the call would be a cost with nothing to
    buy, and every extra LLM call is a chance to invent a fact."""
    llm = FakeLLM(composed=["should not be used"])
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
    assert "1. the party enters the tavern" in message
    assert "2. a fight breaks out" in message
    assert "The tavern brawl: a fight with the guards" in message
    assert "00:12:30 the guards arrive" in message
    assert "it" in message


def test_the_message_survives_a_thin_extraction():
    message = build_summary_compose_message(merged(["one beat"]))
    assert "1. one beat" in message
    assert "Events:" not in message


def test_the_prompt_asks_for_an_opening_and_for_lines_that_stand_alone():
    """The two properties the DM asked for: a train of thought, and lines that
    can still be selected and corrected one at a time."""
    prompt = SUMMARY_COMPOSE_SYSTEM_PROMPT
    assert "FIRST line sets the scene" in prompt
    assert "STANDS ON ITS OWN" in prompt
    assert "CONNECT the beats" in prompt
    assert "Never invent" in prompt
