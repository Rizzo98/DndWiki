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
description (details level), and the roster's names are sent as keyterms so
fantasy names are transcribed correctly.

Keyterm expansion: a character name such as "Thorin Oakenshield" is sent
BOTH as the full name AND as every single word ("Thorin", "Oakenshield") —
players rarely say the full name every time, so the short forms / nicknames
they actually use at the table are covered too. Terms are capped at 6 words
per phrase (AssemblyAI limit) and deduplicated case-insensitively.
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

# AssemblyAI allows at most 6 words per keyterm phrase; longer names are
# split so their single words still go through (see build_keyterms).
MAX_KEYTERM_WORDS = 6

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


def _add_name_terms(keyterms: list[str], name: str, seen: set[str]) -> None:
    """Append keyterm variants for one name (full name + single words).

    A multi-word character name (e.g. "Thorin Oakenshield") is sent both as
    the full name and as each individual word, because players usually call
    each other by a short form / nickname at the table rather than the full
    name every time. Names longer than MAX_KEYTERM_WORDS words cannot be
    sent as a phrase (AssemblyAI caps phrases at 6 words), so only their
    single-word forms go through. Case-insensitive dedup, first-seen order.
    """
    words = name.split()
    candidates = [name] if len(words) <= MAX_KEYTERM_WORDS else []
    if len(words) > 1:
        candidates.extend(words)
    for term in candidates:
        if not term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        if len(keyterms) < MAX_KEYTERMS:
            keyterms.append(term)


def build_keyterms(settings: ServiceSettings, members: list[dict[str, Any]] | None) -> list[str]:
    """Keyterms for the transcription job: the roster's names (expanded).

    Every character name is sent as a keyterm (full name when it fits the
    6-word phrase limit, plus each single word of multi-word names so the
    short forms players use at the table are covered too). Player names are
    used when the member has no character name (e.g. the DM's own row).
    Disabled with ASSEMBLYAI_KEYTERMS_ENABLED=false. Best-effort and capped.
    """
    if not settings.assemblyai_keyterms_enabled or not members:
        return []

    keyterms: list[str] = []
    seen: set[str] = set()
    for member in members:
        character = (member.get("character_name") or "").strip()
        player = (member.get("player_name") or "").strip()
        if character:
            _add_name_terms(keyterms, character, seen)
        elif player:
            _add_name_terms(keyterms, player, seen)
        if len(keyterms) >= MAX_KEYTERMS:
            break
    return keyterms
