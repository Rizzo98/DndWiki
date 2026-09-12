"""Tests for the contextual prompt + keyterms builders."""

from app.core.config import ServiceSettings
from app.prompts import (
    DEFAULT_SESSION_PROMPT,
    build_keyterms,
    build_prompt,
)


def _settings(**overrides) -> ServiceSettings:
    defaults = {}
    defaults.update(overrides)
    return ServiceSettings(**defaults)


def test_build_prompt_default_without_campaign():
    prompt = build_prompt(_settings(), None)
    assert prompt == DEFAULT_SESSION_PROMPT
    assert "Dungeons & Dragons" in prompt


def test_build_prompt_env_override_wins():
    settings = _settings(assemblyai_prompt="Custom domain prompt.")
    assert build_prompt(settings, None) == "Custom domain prompt."


def test_build_prompt_injects_campaign_context():
    campaign = {
        "name": "The Fellowship",
        "description": "Nine companions hunt the Ring across Middle-earth.",
    }
    prompt = build_prompt(_settings(), campaign)
    assert prompt.startswith(DEFAULT_SESSION_PROMPT)
    assert "The campaign is named 'The Fellowship'." in prompt
    assert "Nine companions hunt the Ring" in prompt


def test_build_keyterms_uses_roster_names():
    """Every name at the table is a keyterm: characters, and player names
    when the member has no character (e.g. the DM's own row)."""
    members = [
        {"role": "dm", "character_name": "", "player_name": "Alice"},
        {"role": "player", "character_name": "Aragorn", "player_name": "Bob"},
        {"role": "player", "character_name": "Legolas", "player_name": "Carol"},
    ]
    assert build_keyterms(_settings(), members) == ["Alice", "Aragorn", "Legolas"]


def test_build_keyterms_falls_back_to_player_name():
    members = [
        {"role": "player", "character_name": "", "player_name": "Bob"},
    ]
    assert build_keyterms(_settings(), members) == ["Bob"]


def test_build_keyterms_expands_multi_word_names():
    """Multi-word names are sent as the full name AND as every single word,
    so the short forms players actually use at the table are recognized.
    """
    members = [
        {"role": "player", "character_name": "Thorin Oakenshield", "player_name": "Bob"},
    ]
    assert build_keyterms(_settings(), members) == [
        "Thorin Oakenshield",
        "Thorin",
        "Oakenshield",
    ]


def test_build_keyterms_expansion_is_case_insensitive_and_deduped():
    members = [
        {"role": "player", "character_name": "Elara Moonshadow", "player_name": "A"},
        {"role": "player", "character_name": "MOONSHADOW", "player_name": "B"},
    ]
    assert build_keyterms(_settings(), members) == [
        "Elara Moonshadow",
        "Elara",
        "Moonshadow",
    ]


def test_build_keyterms_single_word_name_not_duplicated():
    assert build_keyterms(_settings(), [{"character_name": "Gimli", "player_name": "A"}]) == ["Gimli"]


def test_build_keyterms_overlong_name_sends_only_single_words():
    # > MAX_KEYTERM_WORDS (6) words: the full phrase exceeds AssemblyAI's
    # per-phrase limit, so only the single-word forms go through.
    name = " ".join(["W" + str(i) for i in range(8)])
    out = build_keyterms(_settings(), [{"character_name": name, "player_name": "A"}])
    assert name not in out
    assert out == name.split()


def test_build_keyterms_disabled_or_empty():
    assert build_keyterms(_settings(assemblyai_keyterms_enabled=False), [{"character_name": "A"}]) == []
    assert build_keyterms(_settings(), None) == []
    assert build_keyterms(_settings(), []) == []
