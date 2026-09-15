"""Who can do what: the capability store and the compatibility solver.

The `capability` channel is the cheapest generalisation in the system
(docs/attribution-model.md S4.2, S10.3): "I cast Fireball" restricts the
candidate set to whoever can cast Fireball, and once the DM confirms one such
moment, every other utterance requiring it is resolved - including in future
sessions.

Three sources, in descending authority:

  dm        a capability sheet the DM filled in  -> CAN veto (compat = -1)
  wiki      the character page's class/abilities -> strong, but inferred
  answer    learned from a question the DM answered
  transcript one-off mentions, weak

The veto is deliberately gated on the DM sheet: without it, our "only the wizard
can cast Fireball" is itself an inference, and an inference must never veto
(S16).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

#: Capability requirement syntax: 'kind:name'. Anything else is ignored rather
#: than guessed at.
KNOWN_KINDS = ("class", "spell", "feature", "item", "weapon", "language", "skill")

#: How much each source is worth when the same capability arrives twice.
SOURCE_WEIGHT: dict[str, float] = {
    "dm": 1.0,
    "wiki": 0.8,
    "answer": 0.9,
    "transcript": 0.4,
}
#: Only a DM-authored sheet may turn a capability into a veto.
AUTHORITATIVE_SOURCES = frozenset({"dm"})


@dataclass(frozen=True)
class Capability:
    """One (member, capability) fact with its provenance."""

    member_id: str
    capability: str
    polarity: str = "can"  # can | cannot
    source: str = "transcript"
    confidence: float = 0.5

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.member_id, self.capability, self.polarity)


def is_well_formed(requirement: str) -> bool:
    """A requirement must be 'kind:name' with a kind the engine understands."""
    kind, _, name = (requirement or "").partition(":")
    return bool(name.strip()) and kind.strip().lower() in KNOWN_KINDS


def normalize(requirement: str) -> str:
    kind, _, name = (requirement or "").partition(":")
    return f"{kind.strip().lower()}:{name.strip().lower()}"


def normalize_all(requirements: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for requirement in requirements:
        if not is_well_formed(requirement):
            continue
        value = normalize(requirement)
        if value not in seen:
            seen.append(value)
    return seen


@dataclass
class CapabilityStore:
    """The campaign's capability knowledge, queryable without a database."""

    entries: dict[str, Capability] = field(default_factory=dict)

    def add(self, capability: Capability) -> None:
        """Add one fact, keeping the most authoritative version of it.

        A weaker source never overwrites a stronger one: the DM's sheet is not
        silently replaced by something the engine inferred from a transcript.
        """
        key = capability.key
        existing = self.entries.get(key)
        if existing is None:
            self.entries[key] = capability
            return
        if SOURCE_WEIGHT.get(capability.source, 0.0) > SOURCE_WEIGHT.get(
            existing.source, 0.0
        ):
            self.entries[key] = capability
        elif capability.confidence > existing.confidence:
            self.entries[key] = Capability(
                member_id=existing.member_id,
                capability=existing.capability,
                polarity=capability.polarity,
                source=existing.source,
                confidence=capability.confidence,
            )

    def of(self, member_id: str) -> frozenset[str]:
        return frozenset(
            entry.capability
            for entry in self.entries.values()
            if entry.member_id == member_id and entry.polarity == "can"
        )

    def by_member(self) -> dict[str, frozenset[str]]:
        out: dict[str, set[str]] = {}
        for entry in self.entries.values():
            if entry.polarity != "can":
                continue
            out.setdefault(entry.member_id, set()).add(entry.capability)
        return {member: frozenset(values) for member, values in out.items()}

    @property
    def authoritative(self) -> bool:
        """Whether ANY capability came from a DM-authored sheet."""
        return any(entry.source in AUTHORITATIVE_SOURCES for entry in self.entries.values())

    def compat(self, member_id: str, requirement: str) -> float:
        """+1 known capable, 0 unknown, -1 only provably incapable AND authoritative."""
        requirement = normalize(requirement)
        for entry in self.entries.values():
            if entry.member_id != member_id or entry.capability != requirement:
                continue
            if entry.polarity == "cannot":
                return -1.0 if entry.source in AUTHORITATIVE_SOURCES else 0.0
            return 1.0 if entry.confidence >= 0.5 else 0.0
        if self.authoritative and self.of(member_id):
            return -1.0
        return 0.0

    def as_payload(self) -> dict[str, object]:
        return {
            "members": {k: sorted(v) for k, v in self.by_member().items()},
            "authoritative": self.authoritative,
        }


def name_key(name: object) -> str:
    """Case/whitespace-insensitive key for a character name."""
    return " ".join(str(name or "").lower().split())


def from_wiki_characters(
    characters: Sequence[Mapping[str, object]],
    *,
    member_of: Mapping[str, str] | None = None,
) -> list[Capability]:
    """Mine a wiki CHARACTER page for the class it records.

    Two things make this less obvious than it looks:

    - the shape is the page wiki-service actually returns (title / kind /
      content_json.attributes), not a bespoke "character" object;
    - the page carries NO link to a person at the table. `member_of` is the
      DM's roster, keyed by normalised character name, and is what ties a page
      to a member id. Without it the page is information about nobody and is
      skipped rather than guessed at.

    A character page records a class, not a spell list, so what this yields is
    `class:<class>` per party member. The ability names the page might carry
    later are read through the same path.
    """
    out: list[Capability] = []
    for character in characters:
        member_id = str(character.get("member_id") or "").strip()
        if not member_id and member_of:
            candidates: list[object] = [character.get("title")]
            aliases = character.get("aliases")
            if isinstance(aliases, Sequence) and not isinstance(aliases, str):
                candidates.extend(aliases)
            for candidate in candidates:
                member_id = member_of.get(name_key(candidate), "")
                if member_id:
                    break
        if not member_id:
            continue

        attributes = character.get("attributes")
        if not isinstance(attributes, Mapping):
            content = character.get("content_json")
            attributes = (
                content.get("attributes") if isinstance(content, Mapping) else None
            )
        if not isinstance(attributes, Mapping):
            continue

        for key in ("class", "subclass"):
            value = attributes.get(key)
            if isinstance(value, str) and value.strip():
                out.append(
                    Capability(
                        member_id=member_id,
                        capability=normalize(f"class:{value}"),
                        source="wiki",
                        confidence=0.9,
                    )
                )
        abilities = attributes.get("abilities")
        if isinstance(abilities, Sequence) and not isinstance(abilities, str):
            for ability in abilities:
                if isinstance(ability, str) and ability.strip():
                    out.append(
                        Capability(
                            member_id=member_id,
                            capability=normalize(f"feature:{ability}"),
                            source="wiki",
                            confidence=0.7,
                        )
                    )
    return [c for c in out if is_well_formed(c.capability)]


def from_dm_sheet(sheet: Mapping[str, Sequence[str]]) -> list[Capability]:
    """A DM-filled party sheet: member id -> ['class:wizard', 'spell:fireball']."""
    out: list[Capability] = []
    for member_id, requirements in sheet.items():
        for requirement in requirements:
            if not is_well_formed(requirement):
                continue
            out.append(
                Capability(
                    member_id=str(member_id),
                    capability=normalize(requirement),
                    source="dm",
                    confidence=1.0,
                )
            )
    return out


def from_answer(
    *, member_id: str, requirements: Iterable[str], session_id: str | None = None
) -> list[Capability]:
    """Capabilities learned from a confirmed moment (S10.3).

    "Player 3 cast Fireball once" is permanently useful knowledge about that
    character, so it is written with high confidence and reused across sessions.
    """
    return [
        Capability(
            member_id=member_id,
            capability=normalize(requirement),
            source="answer",
            confidence=0.95,
        )
        for requirement in normalize_all(requirements)
    ]
