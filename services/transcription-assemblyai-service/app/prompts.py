"""Contextual prompt + keyterms for the AssemblyAI transcription jobs.

Universal-3.5 Pro supports two prompting mechanisms (see
https://www.assemblyai.com/docs/pre-recorded-audio/universal-3-5-pro/prompting):
- prompt: a plain-language description of WHAT the audio is (domain /
  scenario / details) — formatting or behavioral instructions are ignored;
- keyterms_prompt: an explicit list of terms the model must recognize
  accurately (up to 1000 phrases of at most 6 words).

For a D&D session the domain is a tabletop role-playing game around a table:
the Dungeon Master narrates and plays non-player characters, the players
speak as their characters. The built-in prompt describes exactly that; when
campaign-service is reachable it is enriched with the campaign name and
description (details level), and the roster's character/player names are
sent as keyterms so fantasy names are transcribed correctly.
"""

from __future__ import annotations

from typing import Any

from app.core.config import ServiceSettings

# Detailed-level contextual prompt (plain complete sentences describing what
# the audio is). Used when the campaign context is unavailable or the env
# override ASSEMBLYAI_PROMPT is empty.
DEFAULT_SESSION_PROMPT = (
    "This is a recording of a Dungeons & Dragons tabletop role-playing game "
    "session. "
    "The Dungeon Master narrates the story and plays the non-player characters; "
    "the players speak as their characters, discussing quests, exploring, and "
    "role-playing during scenes and combat."
)

# Hard cap on keyterms sent to the API (AssemblyAI supports up to 1000;
# campaigns have a handful of members, so this is just a safety net).
MAX_KEYTERMS = 200

# Cap on the campaign description injected into the prompt (the prompting
# guide recommends short, detailed prompts).
MAX_DESCRIPTION_CHARS = 200


def build_prompt(settings: ServiceSettings, campaign: dict[str, Any] | None) -> str:
    """The contextual prompt for a session transcription job.

    Starts from ASSEMBLYAI_PROMPT when set, otherwise the built-in D&D
    prompt, then appends the campaign name and (truncated) description when
    available so the model knows the scenario. Never raises: campaign data
    is optional and best-effort.
    """
    base = settings.assemblyai_prompt.strip() or DEFAULT_SESSION_PROMPT
    if not campaign:
        return base

    parts = [base]
    name = (campaign.get("name") or "").strip()
    if name:
        parts.append(f"The campaign is named {name!r}.")
    description = (campaign.get("description") or "").strip()
    if description:
        parts.append(description[:MAX_DESCRIPTION_CHARS].rstrip())
    return " ".join(parts)


def build_keyterms(settings: ServiceSettings, members: list[dict[str, Any]] | None) -> list[str]:
    """Keyterms for the transcription job: the roster's names.

    Character names (and player names when the character name is empty) are
    sent as keyterms so the model transcribes fantasy names accurately.
    Disabled with ASSEMBLYAI_KEYTERMS_ENABLED=false. Best-effort and capped.
    """
    if not settings.assemblyai_keyterms_enabled or not members:
        return []

    keyterms: list[str] = []
    for member in members:
        character = (member.get("character_name") or "").strip()
        player = (member.get("player_name") or "").strip()
        if character and character not in keyterms:
            keyterms.append(character)
        elif player and player not in keyterms:
            keyterms.append(player)
        if len(keyterms) >= MAX_KEYTERMS:
            break
    return keyterms
