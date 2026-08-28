"""Per-kind attribute schemas for wiki pages.

A wiki page keeps its free-form `content_json` (summary, facts, aliases, ...);
every category ALSO owns a dedicated, validated `attributes` object inside
that JSON:

    content_json = {
        "summary": "...",
        "facts": ["..."],
        ...
        "attributes": {"character_type": "npc", "race": "Elf"},
    }

The schemas live here so both the API and the service layer enforce one
definition:

- unknown attribute keys are rejected (`extra="forbid"`) - a typo like
  `charecter_type` must not silently become junk;
- known keys are type/range checked (latitudes stay within [-90, 90]);
- location fields are additionally scoped to the `location_type`: a city may
  carry population/government/districts, a dungeon entrance/levels/hazards,
  and a field that belongs to another type is rejected;
- every field is optional with a sensible default, so pages created before a
  schema existed keep working and drafts may carry partial attributes.

The web UI mirrors these shapes (see apps/web/lib/api.ts) to render the
fields as structured blocks instead of raw JSON.
"""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

CharacterType = Literal["npc", "player"]
LocationType = Literal[
    "city",
    "town",
    "village",
    "region",
    "continent",
    "world",
    "building",
    "structure",
    "dungeon",
    "wilderness",
    "other",
]
FactionType = Literal[
    "guild", "order", "government", "military", "criminal", "religious", "other"
]
ItemType = Literal[
    "weapon", "armor", "potion", "artifact", "wondrous", "trinket", "other"
]
ItemRarity = Literal[
    "common", "uncommon", "rare", "very_rare", "legendary", "artifact"
]
QuestStatus = Literal["open", "in_progress", "completed", "failed"]


class CharacterAttributes(BaseModel):
    """Who the character is. `player` marks one of the party's PCs - wiki
    pages always use the CHARACTER name for players, never the player name."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    character_type: CharacterType = "npc"
    race: str | None = Field(default=None, max_length=64)
    # D&D class ("wizard"); aliased because `class` is a Python keyword
    character_class: str | None = Field(default=None, alias="class", max_length=64)
    # Physical traits (free text: unit and phrasing are the DM's call)
    gender: str | None = Field(default=None, max_length=32)
    height: str | None = Field(default=None, max_length=32)
    weight: str | None = Field(default=None, max_length=32)
    age: str | None = Field(default=None, max_length=32)


#: Fields valid for every location type (identity + where it sits).
_COMMON_LOCATION_FIELDS = frozenset(
    {"location_type", "region", "latitude", "longitude", "founded"}
)

#: Fields only valid for settlements (city / town / village).
_SETTLEMENT_LOCATION_FIELDS = frozenset(
    {
        "population",
        "government",
        "ruler",
        "demographics",
        "economy",
        "defenses",
        "religion",
        "districts",
        "notable_locations",
    }
)

#: Fields only valid for regions / continents.
_REGION_LOCATION_FIELDS = frozenset(
    {"government", "ruler", "capital", "terrain", "climate", "notable_locations"}
)

#: Fields only valid for worlds.
_WORLD_LOCATION_FIELDS = frozenset(
    {"terrain", "climate", "planes", "pantheon", "notable_locations"}
)

#: Fields only valid for buildings / structures.
_BUILDING_LOCATION_FIELDS = frozenset({"owner", "purpose"})

#: Fields only valid for dungeons.
_DUNGEON_LOCATION_FIELDS = frozenset({"entrance", "levels", "hazards"})

#: Fields only valid for wilderness.
_WILDERNESS_LOCATION_FIELDS = frozenset(
    {"terrain", "climate", "hazards", "flora_fauna", "notable_locations"}
)

#: location_type -> the type-specific fields it may carry. The common fields
#: are always allowed; anything else provided for a type is rejected
#: (strictness in the same spirit as extra="forbid").
_LOCATION_TYPE_FIELDS: dict[str, frozenset[str]] = {
    "city": _SETTLEMENT_LOCATION_FIELDS,
    "town": _SETTLEMENT_LOCATION_FIELDS,
    "village": _SETTLEMENT_LOCATION_FIELDS,
    "region": _REGION_LOCATION_FIELDS,
    "continent": _REGION_LOCATION_FIELDS,
    "world": _WORLD_LOCATION_FIELDS,
    "building": _BUILDING_LOCATION_FIELDS,
    "structure": _BUILDING_LOCATION_FIELDS,
    "dungeon": _DUNGEON_LOCATION_FIELDS,
    "wilderness": _WILDERNESS_LOCATION_FIELDS,
    "other": frozenset(),
}


class LocationAttributes(BaseModel):
    """Identity of a place: what it is, where it sits, and the facts that
    only matter for its kind.

    `location_type` decides WHICH extra fields are meaningful: a city carries
    population/government/districts, a region carries terrain/climate/capital,
    a dungeon carries entrance/levels/hazards, and so on. Fields that belong
    to another type are rejected with a precise message (see
    _check_type_specific_fields), so a misplaced `population` on a dungeon
    never silently lands on the page.
    """

    model_config = ConfigDict(extra="forbid")

    location_type: LocationType = "other"
    # containing political/geographic unit, e.g. "Eriador", "Regno di Taranto"
    region: str | None = Field(default=None, max_length=128)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    # era/date the place was founded or first appears ("1290 DR", "Age of Myth")
    founded: str | None = Field(default=None, max_length=128)

    # ---- settlements (city / town / village)
    # free text: "12,000", "~12.000 abitanti" - phrasing is the DM's call
    population: str | None = Field(default=None, max_length=64)
    government: str | None = Field(default=None, max_length=128)
    ruler: str | None = Field(default=None, max_length=128)
    demographics: str | None = Field(default=None, max_length=255)
    economy: str | None = Field(default=None, max_length=255)
    defenses: str | None = Field(default=None, max_length=255)
    religion: str | None = Field(default=None, max_length=255)
    districts: list[str] | None = None
    notable_locations: list[str] | None = None

    # ---- regions / continents
    capital: str | None = Field(default=None, max_length=128)
    terrain: str | None = Field(default=None, max_length=255)
    climate: str | None = Field(default=None, max_length=255)

    # ---- worlds
    planes: str | None = Field(default=None, max_length=255)
    pantheon: str | None = Field(default=None, max_length=255)

    # ---- buildings / structures
    owner: str | None = Field(default=None, max_length=128)
    purpose: str | None = Field(default=None, max_length=255)

    # ---- dungeons
    entrance: str | None = Field(default=None, max_length=255)
    levels: str | None = Field(default=None, max_length=64)
    hazards: str | None = Field(default=None, max_length=255)

    # ---- wilderness
    flora_fauna: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _check_type_specific_fields(self) -> LocationAttributes:
        """Reject non-common fields that do not belong to the location type."""
        allowed = _COMMON_LOCATION_FIELDS | _LOCATION_TYPE_FIELDS.get(
            self.location_type, frozenset()
        )
        provided = {
            name for name in self.model_fields_set if getattr(self, name) is not None
        }
        invalid = sorted(provided - allowed)
        if invalid:
            raise ValueError(
                "field(s) " + ", ".join(invalid) + " are not valid for "
                "location_type=" + repr(self.location_type)
            )
        return self


class FactionAttributes(BaseModel):
    """Organizations: who leads them and where they operate from."""

    model_config = ConfigDict(extra="forbid")

    faction_type: FactionType = "other"
    leader: str | None = Field(default=None, max_length=128)
    headquarters: str | None = Field(default=None, max_length=128)


class ItemAttributes(BaseModel):
    """Notable objects: category, rarity, current owner."""

    model_config = ConfigDict(extra="forbid")

    item_type: ItemType = "other"
    rarity: ItemRarity | None = None
    owner: str | None = Field(default=None, max_length=128)


class QuestAttributes(BaseModel):
    """Quest state machine as curated by the DM."""

    model_config = ConfigDict(extra="forbid")

    quest_status: QuestStatus = "open"
    giver: str | None = Field(default=None, max_length=128)
    reward: str | None = Field(default=None, max_length=255)


EventType = Literal[
    "battle", "negotiation", "discovery", "quest", "catastrophe", "political", "other"
]
EventStatus = Literal["ongoing", "resolved", "unknown"]


class EventAttributes(BaseModel):
    """Timeline event: what kind of event it is, its in-world date and who
    took part. The free-form narrative lives in content_json['summary'] /
    'facts'; these attributes are the structured, DM-curated metadata shown
    on the event page and mirrored on the timeline entry."""

    model_config = ConfigDict(extra="forbid")

    event_type: EventType = "other"
    # campaign-specific calendar string, e.g. "17 Ches 1492 DR"
    in_world_date: str | None = Field(default=None, max_length=64)
    participants: list[str] | None = None
    event_status: EventStatus = "unknown"


#: kind -> attribute schema (the six wiki page kinds)
ATTRIBUTE_MODELS: dict[str, type[BaseModel]] = {
    "character": CharacterAttributes,
    "location": LocationAttributes,
    "faction": FactionAttributes,
    "item": ItemAttributes,
    "quest": QuestAttributes,
    "event": EventAttributes,
}

ATTRIBUTES_KEY = "attributes"


def validate_attributes(kind: str, content_json: dict | None) -> dict:
    """Validate `content_json["attributes"]` against the kind's schema.

    Returns the content unchanged when valid; raises HTTPException(422) with
    a precise message otherwise. Kinds without a dedicated schema accept any
    attributes object (forward compatibility).
    """
    if not isinstance(content_json, dict) or ATTRIBUTES_KEY not in content_json:
        return content_json or {}
    attributes = content_json[ATTRIBUTES_KEY]
    if attributes is None:
        # explicit null == "no attributes": strip the key so pages never
        # store a pointless {"attributes": null}
        return {k: v for k, v in content_json.items() if k != ATTRIBUTES_KEY}
    model = ATTRIBUTE_MODELS.get(kind)
    if model is None:  # future kinds without a schema yet
        if not isinstance(attributes, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"attributes must be an object for kind '{kind}'",
            )
        return content_json
    try:
        model.model_validate(attributes)
    except Exception as exc:  # pydantic.ValidationError + non-dict input
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid attributes for kind '{kind}': {exc}",
        ) from exc
    return content_json
