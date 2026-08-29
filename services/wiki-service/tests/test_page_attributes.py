"""Per-kind attribute schemas: validation on create/update paths."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app import services
from app.page_attributes import validate_attributes


async def test_character_physical_traits_stored(session_factory):
    """The static info table fields (race/gender/height/weight/age) are valid."""
    campaign = uuid4()
    async with session_factory() as db:
        page = await services.create_page(
            db,
            campaign_id=campaign,
            kind="character",
            title="Lyra",
            content_json={
                "attributes": {
                    "character_type": "player",
                    "race": "Half-Elf",
                    "gender": "Female",
                    "height": "1.72 m",
                    "weight": "62 kg",
                    "age": "27",
                }
            },
        )
        assert page.content_json["attributes"] == {
            "character_type": "player",
            "race": "Half-Elf",
            "gender": "Female",
            "height": "1.72 m",
            "weight": "62 kg",
            "age": "27",
        }


async def test_valid_attributes_stored(session_factory):
    campaign = uuid4()
    async with session_factory() as db:
        page = await services.create_page(
            db,
            campaign_id=campaign,
            kind="character",
            title="Aragorn",
            content_json={
                "summary": "A ranger.",
                "attributes": {"character_type": "player", "race": "Human", "class": "Ranger"},
            },
        )
        assert page.content_json["attributes"] == {
            "character_type": "player", "race": "Human", "class": "Ranger"
        }


async def test_location_geospatial_attributes(session_factory):
    campaign = uuid4()
    async with session_factory() as db:
        page = await services.create_page(
            db,
            campaign_id=campaign,
            kind="location",
            title="Fatumastra",
            content_json={
                "attributes": {
                    "location_type": "city",
                    "region": "Costa orientale",
                    "latitude": 40.5,
                    "longitude": 18.2,
                }
            },
        )
        assert page.content_json["attributes"]["location_type"] == "city"
        assert page.content_json["attributes"]["latitude"] == 40.5


async def test_location_city_specific_attributes(session_factory):
    """A city carries its settlement stats: population, government, ..."""
    campaign = uuid4()
    async with session_factory() as db:
        page = await services.create_page(
            db,
            campaign_id=campaign,
            kind="location",
            title="Fatumastra",
            content_json={
                "attributes": {
                    "location_type": "city",
                    "region": "Costa orientale",
                    "population": "12,000",
                    "government": "Council of Elders",
                    "ruler": "Burgomaster Aldo",
                    "demographics": "Mostly humans and half-elves",
                    "economy": "Trade and fishing",
                    "defenses": "Stone walls and a city watch",
                    "religion": "Temple of the Sea",
                    "districts": ["Porto", "Alto Quartiere"],
                    "notable_locations": ["Molo Vecchio", "Piazza del Mercato"],
                    "founded": "1290 DR",
                }
            },
        )
        attributes = page.content_json["attributes"]
        assert attributes["location_type"] == "city"
        assert attributes["population"] == "12,000"
        assert attributes["districts"] == ["Porto", "Alto Quartiere"]
        assert attributes["founded"] == "1290 DR"


async def test_location_world_and_dungeon_attributes(session_factory):
    """Worlds keep pantheon/planes; dungeons keep entrance/levels/hazards."""
    campaign = uuid4()
    async with session_factory() as db:
        world = await services.create_page(
            db,
            campaign_id=campaign,
            kind="location",
            title="Terra di Mezzo",
            content_json={
                "attributes": {
                    "location_type": "world",
                    "pantheon": "Dodici Dei",
                    "planes": "Piano Materiale",
                }
            },
        )
        assert world.content_json["attributes"]["pantheon"] == "Dodici Dei"

        dungeon = await services.create_page(
            db,
            campaign_id=campaign,
            kind="location",
            title="Minas Morgul",
            content_json={
                "attributes": {
                    "location_type": "dungeon",
                    "entrance": "West gate",
                    "levels": "3",
                    "hazards": "Pit traps",
                }
            },
        )
        assert dungeon.content_json["attributes"]["entrance"] == "West gate"


def test_location_type_specific_fields_rejected():
    """A field that belongs to another type is rejected (strict scoping)."""
    with pytest.raises(HTTPException) as exc:
        validate_attributes(
            "location",
            {"attributes": {"location_type": "region", "population": "12,000"}},
        )
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        validate_attributes(
            "location",
            {"attributes": {"location_type": "building", "pantheon": "Dodici Dei"}},
        )
    assert exc.value.status_code == 422


def test_location_new_types_accepted():
    """continent / world / structure are first-class location types."""
    from app.page_attributes import LocationAttributes

    for location_type in ("continent", "world", "structure"):
        attributes = LocationAttributes.model_validate({"location_type": location_type})
        assert attributes.location_type == location_type


async def test_missing_or_null_attributes_ok(session_factory):
    campaign = uuid4()
    async with session_factory() as db:
        page = await services.create_page(
            db, campaign_id=campaign, kind="item", title="Ring"
        )
        assert "attributes" not in page.content_json
        patched = await services.update_page(
            db, page.id, content_json={"attributes": None}
        )
        assert "attributes" not in patched.content_json


def test_unknown_attribute_key_rejected():
    with pytest.raises(HTTPException) as exc:
        validate_attributes("character", {"attributes": {"charecter_type": "npc"}})
    assert exc.value.status_code == 422


def test_bad_enum_value_rejected():
    with pytest.raises(HTTPException) as exc:
        validate_attributes("character", {"attributes": {"character_type": "sidekick"}})
    assert exc.value.status_code == 422


def test_out_of_range_coordinates_rejected():
    with pytest.raises(HTTPException) as exc:
        validate_attributes(
            "location", {"attributes": {"latitude": 123.0}}
        )
    assert exc.value.status_code == 422


# ------------------------------------------------------------- events


async def test_event_attributes_stored(session_factory):
    """Event pages carry structured timeline metadata (kind/date/participants)."""
    campaign = uuid4()
    async with session_factory() as db:
        page = await services.create_page(
            db,
            campaign_id=campaign,
            kind="event",
            title="The Siege of Fatumastra",
            content_json={
                "summary": "The city walls hold against the horde.",
                "attributes": {
                    "event_type": "battle",
                    "in_world_date": "17 Ches 1492 DR",
                    "participants": ["Lyra", "Captain Marta"],
                    "event_status": "resolved",
                },
            },
        )
        assert page.kind == "event"
        assert page.content_json["attributes"] == {
            "event_type": "battle",
            "in_world_date": "17 Ches 1492 DR",
            "participants": ["Lyra", "Captain Marta"],
            "event_status": "resolved",
        }


async def test_event_unknown_attribute_rejected(session_factory):
    campaign = uuid4()
    async with session_factory() as db:
        with pytest.raises(HTTPException):
            await services.create_page(
                db,
                campaign_id=campaign,
                kind="event",
                title="Bad Event",
                content_json={"attributes": {"event_typee": "battle"}},
            )


async def test_event_bad_enum_rejected(session_factory):
    campaign = uuid4()
    async with session_factory() as db:
        with pytest.raises(HTTPException):
            await services.create_page(
                db,
                campaign_id=campaign,
                kind="event",
                title="Bad Event",
                content_json={"attributes": {"event_type": "shopping"}},
            )


async def test_event_long_in_world_date_accepted(session_factory):
    """Verbose campaign-calendar dates (e.g. Italian ordinal phrasing)
    must not trip the 64-char limit: the schema allows 256 chars."""
    campaign = uuid4()
    long_date = ("33esimo giorno del primo mese dell'anno della "
                 "settima era sotto la luna cremisi")
    assert len(long_date) > 64
    async with session_factory() as db:
        page = await services.create_page(
            db,
            campaign_id=campaign,
            kind="event",
            title="Il Ritorno della Settima Era",
            content_json={
                "summary": "The seventh era dawns.",
                "attributes": {
                    "event_type": "discovery",
                    "in_world_date": long_date,
                },
            },
        )
        assert page.content_json["attributes"]["in_world_date"] == long_date


async def test_update_validates_against_page_kind(session_factory):
    campaign = uuid4()
    async with session_factory() as db:
        page = await services.create_page(
            db, campaign_id=campaign, kind="quest", title="The lost ring"
        )
        with pytest.raises(HTTPException) as exc:
            await services.update_page(
                db,
                page.id,
                content_json={"attributes": {"character_type": "npc"}},  # wrong schema
            )
        assert exc.value.status_code == 422
        updated = await services.update_page(
            db,
            page.id,
            content_json={"attributes": {"quest_status": "in_progress"}},
        )
        assert updated.content_json["attributes"]["quest_status"] == "in_progress"