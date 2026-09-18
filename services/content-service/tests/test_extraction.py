"""Tests for LLM extraction: lenient JSON parsing and corrective retries.

_parse is exercised directly with malformed outputs (missing commas,
trailing commas, fences, ...) plus strict-failure cases; the corrective
retry path is tested by injecting a fake litellm module so no real
network/LLM call happens.
"""

from __future__ import annotations

import json
import sys
import types

import pytest
from conftest import make_extraction

from app.core.config import ServiceSettings
from app.extraction import (
    CORRECT_JSON_MESSAGE,
    ExtractionError,
    LLMClient,
    TruncatedResponse,
)


def _client() -> LLMClient:
    return LLMClient(ServiceSettings())


# ------------------------------------------------------------- _parse: repair


def test_parse_valid_json():
    data = make_extraction()
    parsed = _client()._parse(json.dumps(data), 0)
    # lenient coercion fills the v3-v5 category-hint fields the fixture omits
    expected = json.loads(json.dumps(data))
    expected["characters"][0]["is_party"] = False
    expected["characters"][0]["physical_look"] = ""
    expected["characters"][0]["personality"] = ""
    expected["characters"][0]["race"] = ""
    expected["characters"][0]["class"] = ""
    expected["characters"][0]["gender"] = ""
    expected["characters"][0]["height"] = ""
    expected["characters"][0]["weight"] = ""
    expected["characters"][0]["age"] = ""
    expected["characters"][0]["relationships"] = {}
    expected["locations"][0]["place_type"] = ""
    expected["locations"][0]["part_of"] = ""
    assert parsed == expected


def test_parse_repairs_missing_comma():
    """The exact failure mode seen in production: 'Expecting ',' delimiter'."""
    good = json.dumps(make_extraction())
    # drop the comma between session_summary and characters
    bad = good.replace('", "characters"', '" "characters"', 1)
    parsed = _client()._parse(bad, 0)
    assert parsed["session_summary"] == make_extraction()["session_summary"]
    assert parsed["characters"][0]["name"] == "Aragorn"


def test_parse_repairs_trailing_comma():
    good = json.dumps(make_extraction())
    bad = good[:-1] + "," + good[-1]  # '...}' -> '... ,}'
    parsed = _client()._parse(bad, 0)
    assert parsed["events"][0]["title"] == "Entering Moria"


def test_parse_strips_code_fence():
    good = json.dumps(make_extraction())
    fence = chr(96) * 3  # triple backtick
    bad = fence + "json\n" + good + "\n" + fence
    parsed = _client()._parse(bad, 0)
    assert parsed["locations"][0]["name"] == "Moria"


def test_parse_repairs_single_quotes():
    bad = "{'session_summary': 'hello', 'characters': [], 'locations': [], 'events': [], 'timeline_entries': []}"
    parsed = _client()._parse(bad, 0)
    assert parsed["session_summary"] == "hello"


def test_parse_garbage_raises():
    with pytest.raises(ExtractionError, match="invalid JSON"):
        _client()._parse("this is not json at all", 0)


def test_parse_empty_raises():
    with pytest.raises(ExtractionError, match="empty LLM response"):
        _client()._parse("", 0)
    with pytest.raises(ExtractionError, match="empty LLM response"):
        _client()._parse(None, 0)


def test_parse_non_object_raises():
    with pytest.raises(ExtractionError, match="non-object"):
        _client()._parse("[1, 2, 3]", 0)


def test_parse_accepts_omitted_empty_categories():
    """The production failure: the model omitted 'events' and
    'timeline_entries' for a chunk where nothing qualified. Missing category
    keys default to empty lists — a chunk must never fail over them."""
    parsed = _client()._parse(
        '{"session_summary": "hello", "characters": [], "locations": []}', 0
    )
    assert parsed["session_summary"] == "hello"
    assert parsed["characters"] == []
    assert parsed["locations"] == []
    assert parsed["events"] == []
    assert parsed["timeline_entries"] == []
    assert parsed["language"] == ""


def test_parse_accepts_partial_extraction():
    """A dict with only one category is a partial extraction, not garbage."""
    parsed = _client()._parse('{"events": []}', 0)
    assert parsed["events"] == []
    assert parsed["session_summary"] == ""


def test_parse_unrelated_object_raises():
    """Valid JSON that is not an extraction at all still goes to retry."""
    with pytest.raises(ExtractionError, match="no extraction keys"):
        _client()._parse('{"foo": "bar", "n": 42}', 0)


# ------------------------------------------------- extract_chunk: retry path


class _FakeLiteLLM:
    """Minimal stand-in for the litellm module (sys.modules injection).

    'finish_reasons' mirrors the provider's per-choice finish_reason ('stop',
    'length', ...); it defaults to 'stop' for every output.
    """

    drop_params = False

    def __init__(self, outputs: list[str], finish_reasons: list[str] | None = None) -> None:
        self.outputs = list(outputs)
        self.finish_reasons = list(finish_reasons or ["stop"] * len(outputs))
        self.calls: list[dict] = []

    async def acompletion(self, **kwargs: object) -> types.SimpleNamespace:
        self.calls.append(kwargs)
        content = self.outputs.pop(0)
        reason = self.finish_reasons.pop(0) if self.finish_reasons else "stop"
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content=content),
                    finish_reason=reason,
                )
            ]
        )


@pytest.fixture
def fake_litellm(monkeypatch):
    def _install(
        outputs: list[str], finish_reasons: list[str] | None = None
    ) -> _FakeLiteLLM:
        fake = _FakeLiteLLM(outputs, finish_reasons)
        monkeypatch.setitem(sys.modules, "litellm", fake)
        return fake

    return _install


async def test_extract_chunk_passes_out_of_world_to_prompt(fake_litellm):
    fake = fake_litellm([json.dumps(make_extraction())])
    client = _client()
    await client.extract_chunk("view", 0, 1, out_of_world=["Dungeon Master"])
    user_message = fake.calls[0]["messages"][1]["content"]
    assert "Out-of-world speakers" in user_message
    assert "Dungeon Master" in user_message
    assert "never characters" in user_message


async def test_extract_chunk_repairs_malformed_json(fake_litellm):
    good = json.dumps(make_extraction())
    bad = good.replace('", "characters"', '" "characters"', 1)
    fake = fake_litellm([bad])
    client = _client()
    data = await client.extract_chunk("view", 0, 1)
    assert data["characters"][0]["name"] == "Aragorn"
    assert len(fake.calls) == 1  # repair was local; no LLM retry


async def test_extract_chunk_retries_with_correction(fake_litellm):
    good = json.dumps(make_extraction())
    fake = fake_litellm(["not json", good])
    client = _client()
    data = await client.extract_chunk("view", 0, 1)
    assert data["events"][0]["title"] == "Entering Moria"
    assert len(fake.calls) == 2
    # second call appends the bad assistant output + corrective user message
    second_messages = fake.calls[1]["messages"]
    assert second_messages[-2] == {"role": "assistant", "content": "not json"}
    assert second_messages[-1]["role"] == "user"
    assert "not valid extraction JSON" in second_messages[-1]["content"]
    assert "Error:" in second_messages[-1]["content"]


async def test_extract_chunk_retries_exhausted(fake_litellm):
    fake = fake_litellm(["not json", "still not json"])
    client = _client()
    with pytest.raises(ExtractionError, match="invalid JSON"):
        await client.extract_chunk("view", 0, 1)
    assert len(fake.calls) == 2  # initial + 1 retry (llm_json_retries default 1)


async def test_extract_chunk_no_retry_when_disabled(fake_litellm):
    fake = fake_litellm(["not json"])
    client = _client()
    client._settings.llm_json_retries = 0
    with pytest.raises(ExtractionError, match="invalid JSON"):
        await client.extract_chunk("view", 0, 1)
    assert len(fake.calls) == 1


async def test_extract_chunk_retries_bounded_by_setting(fake_litellm):
    fake = fake_litellm(["bad", "worse", "nope"])
    client = _client()
    client._settings.llm_json_retries = 2
    with pytest.raises(ExtractionError):
        await client.extract_chunk("view", 0, 1)
    assert len(fake.calls) == 3  # initial + 2 retries


async def test_corrective_message_constant_contains_error():
    msg = CORRECT_JSON_MESSAGE.format(error="boom")
    assert "boom" in msg

# ------------------------------------------------- revise_summary: the loop


async def test_revise_summary_sends_extraction_and_feedback(fake_litellm):
    """The DM's feedback reaches the model together with the extraction that
    is being corrected (one call), and the answer is the PATCH over that
    extraction - never a second copy of it."""
    patch = {
        "session_summary": [
            {"location": "Moria", "text": "Boromir was going to the city center."}
        ],
        "updates": {"characters": {"c0": {"name": "Boromir"}}},
    }
    fake = fake_litellm([json.dumps(patch)])
    client = _client()
    current = make_extraction()
    data = await client.revise_summary(
        current,
        [{"targets": ["The party reaches the gates of Moria."],
          "instruction": "It wasn't Aragorn, it was Boromir."}],
    )
    assert data == patch
    assert len(fake.calls) == 1
    system, user = fake.calls[0]["messages"][0]["content"], fake.calls[0]["messages"][1]["content"]
    # the narrative is shown to the model as its BLOCKS, which is the shape the
    # answer has to use (one version of the story, not two)
    assert '"summary_blocks"' not in user
    assert '{"location": "", "text": "The party reaches the gates of Moria."}' in user
    assert "CORRECTION REQUESTS" in user
    assert "It wasn't Aragorn, it was Boromir." in user
    assert "The party reaches the gates of Moria." in user
    # the extraction the model sees carries an id per item, which is how the
    # patch addresses them ('c0' first character, 'e0' first event, 't0' first
    # timeline entry)
    assert '"id": "c0"' in user
    assert '"id": "e0"' in user
    assert '"id": "l0"' in user
    assert '"id": "t0"' in user
    # the revision prompt, not the chunk-extraction one
    assert "You maintain the session record" in system
    assert "You are given one chunk of a session transcript" not in system


async def test_revise_summary_honors_client_text(fake_litellm):
    fake = fake_litellm([json.dumps({"session_summary": "hand edited narrative"})])
    client = _client()
    await client.revise_summary(
        make_extraction(), [], summary_text_override="hand edited narrative"
    )
    assert "hand edited narrative" in fake.calls[0]["messages"][1]["content"]
    # the narrative the model is shown is the DM's, not the stored one
    assert "The party reaches the gates of Moria." not in fake.calls[0]["messages"][1]["content"]


async def test_revise_summary_repairs_and_retries(fake_litellm):
    good = json.dumps(
        {
            "session_summary": "Rewritten.",
            "updates": {"events": {"e0": {"description": "The party enters."}}},
        }
    )
    bad = good.replace('", "updates"', '" "updates"', 1)
    fake = fake_litellm([bad])
    client = _client()
    data = await client.revise_summary(make_extraction(), [])
    assert data["updates"]["events"]["e0"]["description"] == "The party enters."
    assert len(fake.calls) == 1  # local repair, no retry


async def test_revise_summary_raises_on_garbage(fake_litellm):
    fake = fake_litellm(["not json", "still not json"])
    client = _client()
    with pytest.raises(ExtractionError, match="session summary revision"):
        await client.revise_summary(make_extraction(), [])
    assert len(fake.calls) == 2  # initial + 1 corrective retry


async def test_revise_summary_rejects_a_full_extraction_echo(fake_litellm):
    """A model that answers with the old whole-extraction echo is told so and
    asked again, instead of having its answer half-ignored: that echo is what
    silently lost the correction on every event and timeline entry."""
    patch = {
        "session_summary": "Rewritten.",
        "updates": {"events": {"e0": {"description": "The party enters."}}},
    }
    fake = fake_litellm([json.dumps(make_extraction()), json.dumps(patch)])
    client = _client()
    data = await client.revise_summary(make_extraction(), [])
    assert data == patch
    assert len(fake.calls) == 2
    corrective = fake.calls[1]["messages"][-1]["content"]
    assert "does not fit the session's extraction" in corrective
    assert "characters" in corrective


async def test_revise_summary_rejects_an_unknown_item_id(fake_litellm):
    """The ids are the addressing scheme: a patch that points at an item the
    session does not have is refused inside the retry loop (the DM's
    correction must not be dropped on the floor)."""
    patch = {"session_summary": "Rewritten.", "updates": {"events": {"e9": {"description": "x"}}}}
    good = {
        "session_summary": "Rewritten.",
        "updates": {"events": {"e0": {"description": "The party enters."}}},
    }
    fake = fake_litellm([json.dumps(patch), json.dumps(good)])
    client = _client()
    data = await client.revise_summary(make_extraction(), [])
    assert data == good
    assert len(fake.calls) == 2
    assert "e9" in fake.calls[1]["messages"][-1]["content"]


async def test_compose_summary_returns_scene_blocks(fake_litellm):
    """The story comes back as blocks with the place each part happens in, so
    the session page can label the narrative (app/summary.py)."""
    answer = {
        "session_summary": [
            {"location": "Locanda del Fumo Aspro", "text": "La sessione si apre."},
            {"location": "", "text": "Poi il gruppo esce."},
        ]
    }
    fake = fake_litellm([json.dumps(answer)])
    client = _client()
    blocks = await client.compose_summary(
        {"language": "it", "session_summary": "a beat\nanother beat"},
        language="it",
        scenes=[{"place": "Locanda del Fumo Aspro", "present": ["Hann Caleto"], "absent": []}],
    )
    assert blocks == answer["session_summary"]
    user_message = fake.calls[0]["messages"][1]["content"]
    assert "Where the session happens" in user_message
    assert "Hann Caleto" in user_message


async def test_compose_summary_accepts_plain_prose(fake_litellm):
    """A model that answers with prose instead of blocks still gives the DM a
    story: the paragraphs become blocks without place labels."""
    fake_litellm([json.dumps({"session_summary": "Un paragrafo.\n\nUn altro."})])
    client = _client()
    blocks = await client.compose_summary({"session_summary": "a beat\nanother beat"})
    assert blocks == [
        {"location": "", "text": "Un paragrafo."},
        {"location": "", "text": "Un altro."},
    ]


async def test_compose_summary_refuses_an_empty_story(fake_litellm):
    fake_litellm([json.dumps({"session_summary": []}), json.dumps({"session_summary": []})])
    client = _client()
    with pytest.raises(ExtractionError, match="no story"):
        await client.compose_summary({"session_summary": "a beat\nanother beat"})


# --------------------------------------------------------- truncated answers


async def test_a_truncated_answer_is_never_repaired_into_a_partial_one(fake_litellm):
    """THE production bug: the provider cut the echo of a whole extraction off
    at max_tokens, json_repair closed the tail, and the categories the cut had
    removed kept the previous revision's text - so the DM's correction reached
    the summary and never the events. A cut-off answer must fail loudly."""
    truncated = json.dumps(make_extraction())[:-40]  # missing its last fields
    fake = fake_litellm([truncated], finish_reasons=["length"])
    client = _client()
    client._settings.llm_json_retries = 0
    with pytest.raises(TruncatedResponse, match="cut off"):
        await client.extract_chunk("view", 0, 1)
    assert len(fake.calls) == 1


async def test_a_truncated_answer_is_retried_as_a_shorter_one(fake_litellm):
    good = json.dumps(make_extraction())
    fake = fake_litellm([good[:-40], good], finish_reasons=["length", "stop"])
    client = _client()
    data = await client.extract_chunk("view", 0, 1)
    assert data["characters"][0]["name"] == "Aragorn"
    assert len(fake.calls) == 2
    corrective = fake.calls[1]["messages"][-1]["content"]
    assert "cut off before the JSON was complete" in corrective
    assert "SHORTER" in corrective
