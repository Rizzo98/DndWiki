"""The attribution gate: uncertainty decides what may reach the wiki.

docs/attribution-model.md S14.4. Enforced HERE, in the merger and the planner,
not in a prompt: a prompt is a request, and this is the safety property of the
whole redesign.

    character page fact    requires source_refs resolving to a confident status
    location session_refs  the same
    event participants     the same; unresolved actors are dropped from the list
    event actor            the same, else left empty
    timeline characters    the same
    session summary line   may use party-level wording for unresolved content
    any page               may never contain a raw diarization label, nor a
                           player's real name in a character slot

TWO THINGS THIS MODULE MUST NOT CONFUSE:

1. **A raw label is not a name.** The model can and does emit 'SPEAKER_00' as a
   character entity: `is_generic_name('SPEAKER_00', 'character')` used to be
   False, so nothing stopped it. Every page produced here is filtered.
2. **The merger's existing `confidence` is NOT attribution confidence.**
   `_entity_confidence` computes appearances/total_chunks - cross-chunk
   agreement, not attribution certainty - and the LLM is never asked for it. A
   fact repeated in twelve chunks has confidence 1.0 and can still be attributed
   to the wrong character. The gate keys on the attribution STATUS of the
   source_refs and never on that number.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: The statuses that may back a character-level fact (S7.1).
CONFIDENT_STATUSES = frozenset({"user_confirmed", "auto_high", "propagated"})
#: Statuses whose text may still be quoted, but never attributed to a character.
QUOTABLE_STATUSES = CONFIDENT_STATUSES | {"auto_low"}

#: Diarization labels and voice-identity handles. Neither is a name.
RAW_LABEL_RE = re.compile(r"^(SPEAKER[_ -]?\d+|SPEAKER[_ -]?[A-Z]|V\d+|VOICE[_ -]?\d+)$", re.IGNORECASE)

#: Party-level wording allowed for content the engine could not attribute.
PARTY_WORDS = (
    "the party", "the group", "the adventurers", "someone", "a voice",
    "il gruppo", "la compagnia", "qualcuno", "una voce",
)


def is_raw_label(name: str) -> bool:
    """True for a diarization label or a voice-identity handle.

    Used as a hard filter on every name that reaches a page: a label is a
    measurement of audio, not a person, and a page named 'SPEAKER_00' is the
    single most visible symptom of the old design.
    """
    return bool(RAW_LABEL_RE.match((name or "").strip()))


def artifact_statuses(artifact: Mapping[str, Any] | None) -> dict[str, str]:
    """ref -> status, from the attributed transcript."""
    if not artifact:
        return {}
    return {
        str(utterance.get("id")): str(utterance.get("status") or "unresolved")
        for utterance in artifact.get("utterances") or []
    }


def confident_refs(artifact: Mapping[str, Any] | None) -> set[str]:
    return {
        ref for ref, status in artifact_statuses(artifact).items()
        if status in CONFIDENT_STATUSES
    }


def resolve_refs(
    refs: Iterable[str], statuses: Mapping[str, str]
) -> tuple[list[str], list[str]]:
    """Split a fact's source references into (confident, unresolved)."""
    confident: list[str] = []
    unresolved: list[str] = []
    for ref in refs:
        status = statuses.get(str(ref))
        if status is None:
            unresolved.append(str(ref))
        elif status in CONFIDENT_STATUSES:
            confident.append(str(ref))
        else:
            unresolved.append(str(ref))
    return confident, unresolved


def gate_fact(
    text: str,
    refs: Sequence[str],
    statuses: Mapping[str, str],
    *,
    require_refs: bool = True,
) -> bool:
    """Whether one fact may be written as a character-level statement.

    A fact with NO refs is dropped when the artifact is available: the engine
    cannot verify it, and an unverifiable fact on a character page is exactly the
    silent error the redesign exists to prevent. When there is no artifact at all
    (attribution disabled) the gate is a pass-through, so the old path keeps
    behaving exactly as before.
    """
    if not statuses:
        return True
    if not refs:
        return not require_refs
    confident, _ = resolve_refs(refs, statuses)
    return bool(confident) and len(confident) == len(refs)


def gate_actor(actor: str, refs: Sequence[str], statuses: Mapping[str, str]) -> str:
    """An event's actor, or '' when the attribution does not support it."""
    if not actor:
        return ""
    if is_raw_label(actor):
        return ""
    if not statuses:
        return actor
    return actor if gate_fact(actor, refs, statuses) else ""


def gate_participants(
    participants: Sequence[str], refs: Sequence[str], statuses: Mapping[str, str]
) -> list[str]:
    """Drop the participants the attribution cannot support.

    Dropping is the honest option: keeping a guessed name on an event page is
    how a summary becomes fiction. 'Participants: the party' is a better page
    than 'Participants: Thorin' when nobody knows who was there.
    """
    if not statuses:
        return [p for p in participants if not is_raw_label(p)]
    allowed = gate_fact("", refs, statuses)
    kept = [p for p in participants if not is_raw_label(p)]
    return kept if allowed else []


def party_level(text: str) -> str:
    """Whether a summary line is written at party level rather than naming anybody."""
    lowered = (text or "").lower()
    return any(word in lowered for word in PARTY_WORDS)


def apply_gate(
    merged: dict[str, Any],
    artifact: Mapping[str, Any] | None,
    *,
    player_names: Sequence[str] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Filter a merged extraction down to what the attribution supports.

    Returns (gated_merged, report). The report is what makes the gate auditable:
    "12 facts dropped for unresolved attribution" is a number the DM could be
    shown, and a test asserts on it.
    """
    statuses = artifact_statuses(artifact)
    # The roster the artifact already carries is the source of the players'
    # real names. Taking them from there rather than from an argument means the
    # "a character page is never named after a player" rule cannot be forgotten
    # by a caller; 'player_names' stays as an explicit override for callers that
    # know more (a member who renamed, a campaign roster newer than the run).
    if not player_names and artifact:
        player_names = [
            str(entry.get("player_name") or "")
            for entry in artifact.get("roster") or []
        ]
    blocked_names = {str(n).strip().lower() for n in player_names if n}
    report: dict[str, Any] = {
        "enabled": bool(statuses),
        "dropped_characters": [],
        "dropped_facts": 0,
        "dropped_participants": 0,
        "cleared_actors": 0,
        "raw_label_entities": [],
        "player_named_characters": [],
    }

    def _clean_name(name: str) -> str:
        return (name or "").strip()

    out = dict(merged)

    characters: list[dict[str, Any]] = []
    for character in merged.get("characters", []):
        name = _clean_name(character.get("name"))
        if is_raw_label(name):
            report["raw_label_entities"].append(name)
            continue
        # the name resolver used to fall back to the PLAYER's name when no
        # character name was known, which produced character pages named after
        # people at the table. There is no fallback any more: with no character
        # name the speaker is unresolved, not a player-named character.
        if name.lower() in blocked_names:
            report["player_named_characters"].append(name)
            continue
        item = dict(character)
        facts = [
            fact for fact in item.get("facts", [])
            if gate_fact(str(fact), item.get("source_refs") or [], statuses)
        ]
        report["dropped_facts"] += len(item.get("facts", [])) - len(facts)
        item["facts"] = facts
        if not facts and not (item.get("physical_look") or "").strip() and not (
            item.get("personality") or ""
        ).strip() and not (item.get("description") or "").strip():
            # nothing verifiable survived: an empty page is worse than no page
            report["dropped_characters"].append(name)
            continue
        characters.append(item)
    out["characters"] = characters

    events: list[dict[str, Any]] = []
    for event in merged.get("events", []):
        item = dict(event)
        refs = item.get("source_refs") or []
        actor = _clean_name(item.get("actor"))
        if actor and not gate_fact(actor, refs, statuses):
            item["actor"] = ""
            report["cleared_actors"] += 1
        participants = item.get("participants") or []
        kept = gate_participants(participants, refs, statuses)
        report["dropped_participants"] += len(participants) - len(kept)
        item["participants"] = kept
        events.append(item)
    out["events"] = events

    timeline: list[dict[str, Any]] = []
    for entry in merged.get("timeline_entries", []):
        item = dict(entry)
        refs = item.get("source_refs") or []
        characters_field = item.get("characters") or []
        if statuses and characters_field and not gate_fact("", refs, statuses):
            item["characters"] = []
        item["characters"] = [
            c for c in item.get("characters", []) if not is_raw_label(c)
        ]
        timeline.append(item)
    out["timeline_entries"] = timeline

    return out, report


def strip_raw_labels_from_drafts(drafts: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Last-resort filter on the payloads that would become pages.

    Belt and braces: even if an entity slipped through the merger (an alias, a
    relationship target), no PageCreate may carry a raw label as its title or in
    its aliases.
    """
    out: list[dict[str, Any]] = []
    for draft in drafts:
        title = str(draft.get("title") or "")
        if is_raw_label(title):
            continue
        item = dict(draft)
        content = dict(item.get("content_json") or {})
        aliases = content.get("aliases")
        if isinstance(aliases, list):
            content["aliases"] = [a for a in aliases if not is_raw_label(str(a))]
            if not content["aliases"]:
                content.pop("aliases", None)
        item["content_json"] = content
        out.append(item)
    return out
