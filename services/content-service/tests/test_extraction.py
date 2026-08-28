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
from app.extraction import CORRECT_JSON_MESSAGE, ExtractionError, LLMClient


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
    """Minimal stand-in for the litellm module (sys.modules injection)."""

    drop_params = False

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    async def acompletion(self, **kwargs: object) -> types.SimpleNamespace:
        self.calls.append(kwargs)
        content = self.outputs.pop(0)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))]
        )


@pytest.fixture
def fake_litellm(monkeypatch):
    def _install(outputs: list[str]) -> _FakeLiteLLM:
        fake = _FakeLiteLLM(outputs)
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