"""The identity-evidence pass: which moments tell us WHO was speaking.

The wiki extraction in content-service answers "what is worth documenting?".
Attribution needs a different, earlier, cheaper question, and the two sets are
not the same (docs/attribution-model.md S6.1): "the party bought rope" is
wiki-worthy and says nothing about identity, while "I rage and attack the
captain" is identity-decisive and may never reach a page.

So this is a dedicated pass over the diarized, text-corrected transcript, run
BEFORE the content pass, on the cheap model, with a small output. It never
rewrites the transcript and never assigns names.

The view shows the model the CURRENT anonymous voice identity (`V3`) on
purpose: it lets the pass say "u_00411 and u_00420 are the same voice and both
narrate", which text alone cannot see. The model is explicitly told these are
anonymous voice groups, not people.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Bumped when the pass's OUTPUT CONTRACT changes, because the artifact records
#: which prompt produced its evidence. v2: the roster is part of every chunk (it
#: used to be dropped on the way to the model), and the response is asked for one
#: object per utterance instead of "at most 40, drop the rest" - which is what
#: left 272 of a 419-utterance session with no evidence of any kind.
PROMPT_VERSION = "attr-ev-v2"

#: The schema the pass must return. Deliberately small: the model is doing
#: classification and mention-detection, not summarisation, and a small schema is
#: what keeps the pass cheap and reliable (S6.3).
EVIDENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "language": {"type": "string"},
        "utterances": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": [
                            "action",
                            "dialogue",
                            "decision",
                            "narration",
                            "meta",
                            "backchannel",
                        ],
                    },
                    "voice_mode": {
                        "type": "string",
                        "enum": ["pc_dialogue", "npc_dialogue", "narration", "ooc"],
                    },
                    "gist": {"type": "string"},
                    "capability_requirements": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "claims": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": [
                                        "self_character",
                                        "other_character",
                                        "self_player",
                                    ],
                                },
                                "value": {"type": "string"},
                                "strength": {"type": "number"},
                            },
                            "required": ["type", "value"],
                        },
                    },
                    "addresses": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "target": {
                                    "type": "string",
                                    "enum": ["next_turn", "same_turn", "unknown"],
                                },
                            },
                            "required": ["name"],
                        },
                    },
                    "stakes": {"type": "number"},
                },
                "required": ["ref"],
            },
        },
        "identity_notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["member_absent", "delegation", "voice_group_hint"],
                    },
                    "member": {"type": "string"},
                    "from_member": {"type": "string"},
                    "to_member": {"type": "string"},
                    "voices": {"type": "array", "items": {"type": "string"}},
                    "from": {"type": "string"},
                    "to": {"type": ["string", "null"]},
                    "reason": {"type": "string"},
                },
                "required": ["type"],
            },
        },
    },
    "required": ["utterances"],
}


SYSTEM_PROMPT = """You are analysing the transcript of a tabletop RPG (Dungeons & Dragons) session.

Your job is NOT to summarise the session and NOT to decide who was speaking.
Your job is to find the moments that would TELL SOMEBODY who was speaking, and
to record them precisely.

You will receive the session as numbered utterances:

  [u_00412 00:41:15 V3] <text>

The reference (u_00412) identifies the utterance: ALWAYS echo it back exactly.
The time is when it was said. The "V3" marker is an ANONYMOUS VOICE GROUP
produced by the audio diarization: it is NOT a person, NOT a player and NOT a
character. Two utterances with the same V marker were probably said by the same
voice; that is all it means. Use it as a hint, never as an answer.

For every utterance record:

* "kind": action | dialogue | decision | narration | meta | backchannel.
  "meta" is table talk (scheduling, snacks, rules lawyering). "backchannel" is
  "mm-hm", "yeah", laughter.

* "voice_mode": pc_dialogue (a player speaking as their character),
  npc_dialogue (somebody voicing a non-player character), narration (describing
  the world), ooc (out of character).

* "gist": a SHORT phrase describing what happens, in the third person and
  WITHOUT a subject. Write "casts Fireball on the three goblins", not "Aramil
  casts Fireball". The gist is used to phrase a question for the Dungeon Master,
  so it must be answerable from memory and must not name anybody.

* "capability_requirements": what the utterance requires of whoever said it,
  as "kind:name" with kind one of class, spell, feature, item, weapon,
  language, skill. "I cast Fireball" -> ["spell:fireball"]. "I rage" ->
  ["feature:rage"]. Record the REQUIREMENT, never resolve it: do not decide
  that only the wizard could have said it.

* "claims": explicit self-identification, when the speaker says who they are.
  type "self_character" for "I, Aramil, ..." or "Aramil moves to the door"
  said by Aramil's own player (third-person self-narration is very common at
  real tables); "other_character" when a speaker names somebody else's
  character. "strength" is 0..1: how sure you are that this really identifies
  the speaker. A hedge ("I think I'm playing Thorin tonight") is 0.3, not 1.0.

* "addresses": names the utterance calls out to. "target" is "next_turn" when
  the speaker is clearly handing the turn over ("Thorin, what do you do?").

* "stakes": 0..1, how much this moment matters to the story. A decision that
  changes the plot is 0.9; a joke about snacks is 0.05.

Also record "identity_notes" - facts that change WHO COULD BE SPEAKING:

* member_absent: somebody is not there ("Keth isn't here tonight").
* delegation: somebody is playing another person's character ("Bob is running
  Keth for the rest of the night").
* voice_group_hint: a statement about the anonymous voice groups, e.g. that two
  of them both narrate the scene.

RULES YOU MUST FOLLOW:

* Never assign, change or invent a speaker label. The diarization labels are
  measurements taken from the audio; a label you guess destroys the measurement.
* Never invent a member who is not in the roster you were given.
* Never output a raw label such as SPEAKER_00 or V3 as a name.
* Never resolve an actor by reasoning ("probably the wizard"). Record the
  requirement and let the engine decide.
* Echo back exactly one object per utterance reference, in order.
* If the transcript is unclear, record less. An empty claim is always better
  than a wrong one.

Respond with a single JSON object matching EXACTLY this schema (no markdown, no
commentary outside the JSON):

{schema}
"""


@dataclass(frozen=True)
class EvidenceItem:
    """One utterance's extracted evidence."""

    ref: str
    kind: str | None = None
    voice_mode: str | None = None
    gist: str | None = None
    capability_requirements: tuple[str, ...] = ()
    claims: tuple[dict[str, Any], ...] = ()
    addresses: tuple[dict[str, Any], ...] = ()
    stakes: float = 0.5


@dataclass(frozen=True)
class IdentityNote:
    """An in-session fact that changes the candidate set for a window."""

    type: str
    member: str | None = None
    from_member: str | None = None
    to_member: str | None = None
    voices: tuple[str, ...] = ()
    start_ref: str | None = None
    end_ref: str | None = None
    reason: str | None = None


@dataclass
class EvidencePass:
    """The whole pass's output for one session."""

    language: str = "en"
    items: dict[str, EvidenceItem] = field(default_factory=dict)
    notes: list[IdentityNote] = field(default_factory=list)
    prompt_version: str = PROMPT_VERSION

    def item(self, ref: str) -> EvidenceItem:
        return self.items.get(ref, EvidenceItem(ref=ref))


def roster_block(
    members: Sequence[Mapping[str, Any]],
    capabilities: Mapping[str, Sequence[str]] | None = None,
) -> list[str]:
    """The roster header of the view: player -> character (class)."""
    capabilities = capabilities or {}
    lines: list[str] = ["Roster (player -> character):"]
    for member in members:
        member_id = str(member.get("member_id") or member.get("id") or "")
        player = str(member.get("player_name") or "").strip() or "?"
        if str(member.get("role") or "") == "dm":
            lines.append(f"  {player} -> (the Dungeon Master, narrator)")
            continue
        character = str(member.get("character_name") or "").strip() or "(unnamed)"
        lines.append(f"  {player} -> {character}")
        known = capabilities.get(member_id)
        if known:
            lines.append(f"      known abilities: {', '.join(sorted(known))}")
    return lines


def render_view(
    utterances: Sequence[Any],
    *,
    voice_of: Mapping[str, str] | None = None,
    member_of: Mapping[str, str] | None = None,
    roster: Sequence[str] = (),
) -> str:
    """The text the pass reads: one line per utterance, with its reference.

    `utterances` are app.utterances.Utterance values (or anything with ref,
    start, text). The voice marker is the CURRENT anonymous identity, and the
    member name is shown only when the attribution is already confident - it
    gives the pass anchors without ever asking it to produce one.
    """
    voice_of = voice_of or {}
    member_of = member_of or {}
    lines = list(roster)
    if roster:
        lines.append("")
    for utterance in utterances:
        ref = getattr(utterance, "ref", "")
        start = float(getattr(utterance, "start", 0.0))
        text = " ".join(str(getattr(utterance, "text", "")).split())
        voice = voice_of.get(ref, "-")
        speaker = member_of.get(ref)
        stamp = _timestamp(start)
        if speaker:
            lines.append(f"[{ref} {stamp} {voice}] {speaker}: {text}")
        else:
            lines.append(f"[{ref} {stamp} {voice}] {text}")
    return "\n".join(lines)


def _timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def parse_evidence(raw: Any) -> EvidencePass:
    """Normalise one LLM response into an EvidencePass.

    Lenient by design: a malformed item is DROPPED rather than failing the pass.
    The engine degrades gracefully to voice + continuity when the evidence is
    thin, which is strictly better than failing a four-hour session because the
    model emitted one bad row (S16).
    """
    if not isinstance(raw, dict):
        return EvidencePass()
    pass_ = EvidencePass(language=str(raw.get("language") or "en"))

    for item in raw.get("utterances") or []:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "").strip()
        if not ref:
            continue
        pass_.items[ref] = EvidenceItem(
            ref=ref,
            kind=_one_of(item.get("kind"), {"action", "dialogue", "decision",
                                            "narration", "meta", "backchannel"}),
            voice_mode=_one_of(
                item.get("voice_mode"),
                {"pc_dialogue", "npc_dialogue", "narration", "ooc"},
            ),
            gist=(str(item["gist"]).strip() or None) if item.get("gist") else None,
            capability_requirements=tuple(
                str(c).strip()
                for c in (item.get("capability_requirements") or [])
                if isinstance(c, str) and c.strip()
            ),
            claims=tuple(
                {
                    "type": str(c.get("type") or ""),
                    "value": str(c.get("value") or "").strip(),
                    "strength": _clamp(c.get("strength"), 0.0, 1.0, default=1.0),
                }
                for c in (item.get("claims") or [])
                if isinstance(c, dict) and c.get("value")
            ),
            addresses=tuple(
                {
                    "name": str(a.get("name") or "").strip(),
                    "target": str(a.get("target") or "unknown"),
                }
                for a in (item.get("addresses") or [])
                if isinstance(a, dict) and a.get("name")
            ),
            stakes=_clamp(item.get("stakes"), 0.0, 1.0, default=0.5),
        )

    for note in raw.get("identity_notes") or []:
        if not isinstance(note, dict):
            continue
        note_type = str(note.get("type") or "").strip()
        if note_type not in {"member_absent", "delegation", "voice_group_hint"}:
            continue
        pass_.notes.append(
            IdentityNote(
                type=note_type,
                member=_clean(note.get("member")),
                from_member=_clean(note.get("from_member")),
                to_member=_clean(note.get("to_member")),
                voices=tuple(
                    str(v) for v in (note.get("voices") or []) if isinstance(v, str)
                ),
                start_ref=_clean(note.get("from")),
                end_ref=_clean(note.get("to")),
                reason=_clean(note.get("reason")),
            )
        )
    return pass_


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _one_of(value: Any, allowed: set[str]) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in allowed else None


def _clamp(value: Any, low: float, high: float, *, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(low, min(high, float(value)))


def split_view(view: str) -> tuple[str, list[str]]:
    """Split a rendered view into (header, utterance lines).

    The header is everything above the first utterance line: the roster and the
    blank line that follows it. It is returned separately because the pass is
    CHUNKED, and every chunk has to carry it: a chunk without the roster asks the
    model to fill in 'claims' and 'addresses' for members it was never shown,
    while the system prompt insists it must never invent one that is not in the
    roster. Dropping it (the lines that do not start with '[' used to be filtered
    out here) starved the strongest channel in the engine: 4 claims and 3
    addresses in a whole 419-utterance session.
    """
    header: list[str] = []
    utterances: list[str] = []
    for line in view.splitlines():
        if line.startswith("["):
            utterances.append(line)
        elif not utterances:
            header.append(line)
    return "\n".join(header).strip(), utterances


def build_chunk_message(
    view: str,
    *,
    window: int,
    total: int,
    header: str | None = None,
    expected: int | None = None,
) -> str:
    """One chunk's user message: the part number, the roster, the utterances.

    The count is stated because the pass is scored on COVERAGE: one object per
    utterance, echoing every ref. Asking for "the ones with the most identity
    evidence" instead is how a chunk quietly returned a third of itself.
    """
    parts = [f"Session transcript, part {window + 1}/{total}."]
    if header:
        parts += ["", header]
    parts += ["", view, ""]
    count = f"{expected} " if expected else "one object per "
    parts.append(
        "Return the JSON object described in your instructions: "
        f"{count}utterance(s), in the order given, echoing every ref exactly. "
        "Every utterance gets an object; leave a field out when you cannot tell, "
        "never the utterance."
    )
    return "\n".join(parts)


def system_message() -> str:
    return SYSTEM_PROMPT.format(schema=json.dumps(EVIDENCE_SCHEMA))
