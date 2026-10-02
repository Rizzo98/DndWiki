"""Sanitize the structured attributes of a page a NOTE proposed.

wiki-service is the source of truth for what a page may carry
(services/wiki-service/app/page_attributes.py): unknown attribute keys are
rejected, values are enum/range checked, and a location field that does not
belong to its location_type is refused. That validation happens on the WRITE,
and its internal apply endpoint is all-or-nothing: one bad attribute on one
proposed page fails the whole confirmed change set, so a single hallucinated
key would cost the DM every page in the proposal.

So the notes pipeline checks its own homework first. Everything the model
returns for 'attributes' passes through :func:`sanitize_attributes`, which
DROPS what the wiki would refuse instead of forwarding it:

- an unknown key is dropped (the model invented a field);
- a value outside its enum is dropped (the model picked "humanoid" for
  character_type);
- a string longer than the field's cap is truncated, not dropped (the text is
  real, it is just too long);
- a type-specific location field is dropped unless location_type was actually
  given and allows it - the same rule, and the same reason, as
  LocationAttributes._check_type_specific_fields: a 'population' on a dungeon
  is not a fact about a dungeon.

The rule the model is given (prompts.NOTE_PLAN_SYSTEM_PROMPT) is generated
FROM this table, so what it is asked for and what is accepted cannot drift
apart here. This module and page_attributes.py must be kept in step: a new
attribute added there is invisible to the notes pipeline until it is added
here.
"""

from __future__ import annotations

from typing import Any

# --- field specs -----------------------------------------------------------
# ("str", max_length) | ("enum", allowed) | ("list",) | ("float", low, high)
_STR = "str"
_ENUM = "enum"
_LIST = "list"
_FLOAT = "float"

#: Every attribute key, per kind, with the constraint the wiki enforces.
CHARACTER_ATTRIBUTES: dict[str, tuple] = {
    "character_type": (_ENUM, ("npc", "player")),
    "race": (_STR, 64),
    "class": (_STR, 64),
    "gender": (_STR, 32),
    "height": (_STR, 32),
    "weight": (_STR, 32),
    "age": (_STR, 32),
}

#: Fields every location may carry, whatever its type.
LOCATION_COMMON: dict[str, tuple] = {
    "location_type": (
        _ENUM,
        (
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
        ),
    ),
    "region": (_STR, 128),
    "latitude": (_FLOAT, -90, 90),
    "longitude": (_FLOAT, -180, 180),
    "founded": (_STR, 128),
}

#: location_type -> the type-specific fields it may carry (mirrors
#: page_attributes._LOCATION_TYPE_FIELDS).
LOCATION_BY_TYPE: dict[str, dict[str, tuple]] = {
    "city": {
        "population": (_STR, 64),
        "government": (_STR, 128),
        "ruler": (_STR, 128),
        "demographics": (_STR, 255),
        "economy": (_STR, 255),
        "defenses": (_STR, 255),
        "religion": (_STR, 255),
        "districts": (_LIST,),
        "notable_locations": (_LIST,),
    },
    "region": {
        "government": (_STR, 128),
        "ruler": (_STR, 128),
        "capital": (_STR, 128),
        "terrain": (_STR, 255),
        "climate": (_STR, 255),
        "notable_locations": (_LIST,),
    },
    "world": {
        "terrain": (_STR, 255),
        "climate": (_STR, 255),
        "planes": (_STR, 255),
        "pantheon": (_STR, 255),
        "notable_locations": (_LIST,),
    },
    "building": {"owner": (_STR, 128), "purpose": (_STR, 255)},
    "dungeon": {
        "entrance": (_STR, 255),
        "levels": (_STR, 64),
        "hazards": (_STR, 255),
    },
    "wilderness": {
        "terrain": (_STR, 255),
        "climate": (_STR, 255),
        "hazards": (_STR, 255),
        "flora_fauna": (_STR, 255),
        "notable_locations": (_LIST,),
    },
    "other": {},
}
# Villages are settlements and structures are buildings: the wiki gives each
# pair ONE field set (page_attributes._LOCATION_TYPE_FIELDS), so they share the
# entry here rather than repeating it.
LOCATION_BY_TYPE["town"] = LOCATION_BY_TYPE["city"]
LOCATION_BY_TYPE["village"] = LOCATION_BY_TYPE["city"]
LOCATION_BY_TYPE["continent"] = LOCATION_BY_TYPE["region"]
LOCATION_BY_TYPE["structure"] = LOCATION_BY_TYPE["building"]

FACTION_ATTRIBUTES: dict[str, tuple] = {
    "faction_type": (
        _ENUM,
        ("guild", "order", "government", "military", "criminal", "religious", "other"),
    ),
    "leader": (_STR, 128),
    "headquarters": (_STR, 128),
}

ITEM_ATTRIBUTES: dict[str, tuple] = {
    "item_type": (
        _ENUM,
        ("weapon", "armor", "potion", "artifact", "wondrous", "trinket", "other"),
    ),
    "rarity": (
        _ENUM,
        ("common", "uncommon", "rare", "very_rare", "legendary", "artifact"),
    ),
    "owner": (_STR, 128),
}

QUEST_ATTRIBUTES: dict[str, tuple] = {
    "quest_status": (_ENUM, ("open", "in_progress", "completed", "failed")),
    "giver": (_STR, 128),
    "reward": (_STR, 255),
}

EVENT_ATTRIBUTES: dict[str, tuple] = {
    "event_type": (
        _ENUM,
        ("battle", "negotiation", "discovery", "quest", "catastrophe", "political", "other"),
    ),
    "in_world_date": (_STR, 256),
    "participants": (_LIST,),
    "event_status": (_ENUM, ("ongoing", "resolved", "unknown")),
}

#: kind -> its plain (non-location) field table.
PLAIN_ATTRIBUTES: dict[str, dict[str, tuple]] = {
    "character": CHARACTER_ATTRIBUTES,
    "faction": FACTION_ATTRIBUTES,
    "item": ITEM_ATTRIBUTES,
    "quest": QUEST_ATTRIBUTES,
    "event": EVENT_ATTRIBUTES,
}

#: The prose sections a note may fill, per kind: the same content_json keys the
#: session pipeline writes (docs/data-model.md), so a page drafted from notes
#: and a page drafted from a session are the same kind of page.
PROSE_FIELDS = ("summary", "history", "physical_look", "personality", "facts", "aliases")

#: Prose keys that hold a LIST of strings rather than one string.
_LIST_PROSE = ("facts", "aliases")


def attribute_catalog(kind: str) -> dict[str, tuple]:
    """The fields the model may fill for a kind (drives the prompt text)."""
    if kind == "location":
        catalog = dict(LOCATION_COMMON)
        for spec in LOCATION_BY_TYPE.values():
            catalog.update(spec)
        return catalog
    return dict(PLAIN_ATTRIBUTES.get(kind, {}))


def _clean_scalar(spec: tuple, value: Any) -> Any:
    """One attribute value, or None when the wiki would refuse it."""
    form = spec[0]
    if form == _ENUM:
        allowed = spec[1]
        if isinstance(value, str) and value in allowed:
            return value
        return None
    if form == _STR:
        if not isinstance(value, str):
            return None
        text = value.strip()
        return text[: spec[1]] or None
    if form == _LIST:
        if not isinstance(value, list):
            return None
        items = [v.strip() for v in value if isinstance(v, str) and v.strip()]
        return items or None
    if form == _FLOAT:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if spec[1] <= number <= spec[2] else None
    return None


def sanitize_attributes(kind: str, raw: Any) -> dict[str, Any]:
    """The attributes of a proposed page, with everything invalid dropped.

    Returns {} when nothing survives - the page is then written without an
    'attributes' key at all, which is a valid page.
    """
    if not isinstance(raw, dict) or not raw:
        return {}

    if kind == "location":
        location_type = raw.get("location_type")
        allowed = dict(LOCATION_COMMON)
        if isinstance(location_type, str) and location_type in LOCATION_BY_TYPE:
            allowed.update(LOCATION_BY_TYPE[location_type])
        # Without a usable location_type the wiki falls back to "other", which
        # accepts ONLY the common fields - so a stray 'population' would fail
        # the write. Drop it here, exactly as the validator would.
        else:
            raw = {k: v for k, v in raw.items() if k in LOCATION_COMMON}
            location_type = None
    else:
        allowed = PLAIN_ATTRIBUTES.get(kind, {})

    cleaned: dict[str, Any] = {}
    for key, spec in allowed.items():
        if key not in raw:
            continue
        value = _clean_scalar(spec, raw[key])
        if value is not None:
            cleaned[key] = value
    if kind == "location" and location_type is None:
        # keep the explicit "other" so the page's type is stated, not inferred
        cleaned.pop("location_type", None)
    return cleaned


def sanitize_content_json(kind: str, raw: Any) -> dict[str, Any]:
    """A proposed page body: the known prose sections + sanitized attributes.

    Anything else the model returned is dropped rather than stored: content_json
    is what the wiki RENDERS, and a key it does not know would either be ignored
    or, for 'attributes', rejected outright.
    """
    body = raw if isinstance(raw, dict) else {}
    content: dict[str, Any] = {}

    for field in PROSE_FIELDS:
        value = body.get(field)
        if value is None:
            continue
        if field in _LIST_PROSE:
            if isinstance(value, str):
                value = [value] if value.strip() else []
            elif isinstance(value, list):
                value = [v for v in value if isinstance(v, str) and v.strip()]
            else:
                continue
            if value:
                content[field] = value
        elif isinstance(value, str) and value.strip():
            content[field] = value.strip()

    attributes = sanitize_attributes(kind, body.get("attributes"))
    if attributes:
        content["attributes"] = attributes
    return content
