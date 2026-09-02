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


def test_build_keyterms_disabled_or_empty():
    assert build_keyterms(_settings(assemblyai_keyterms_enabled=False), [{"character_name": "A"}]) == []
    assert build_keyterms(_settings(), None) == []
    assert build_keyterms(_settings(), []) == []
