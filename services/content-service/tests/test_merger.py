"""Tests for the cross-chunk merger + wiki draft builder."""

from app.merger import (
    POSSIBLE_DUPLICATE,
    build_event_drafts,
    build_page_drafts,
    exclude_character_names,
    is_filler_text,
    is_generic_name,
    map_location_type,
    match_existing_page,
    merge_extractions,
)


def _entity(name, **overrides):
    base = {"name": name, "aliases": [], "description": "d", "facts": [], "session_facts": [], "mentions": 1}
    base.update(overrides)
    return base


def test_merge_dedupes_entities_across_chunks():
    extractions = [
        {
            "language": "en",
            "session_summary": "short",
            "characters": [
                {
                    "name": "Aragorn",
                    "aliases": [],
                    "description": "A ranger.",
                    "physical_look": "Lean.",
                    "personality": "Quiet.",
                    "facts": ["Leads the party."],
                    "session_facts": [],
                    "mentions": 2,
                }
            ],
            "locations": [],
            "events": [],
            "timeline_entries": [],
        },
        {
            "language": "en",
            "session_summary": "A much longer and more detailed summary of the session.",
            "characters": [
                {
                    "name": "aragorn",  # case-insensitive duplicate
                    "aliases": ["Strider"],
                    "description": "A ranger of the north, heir to a lost throne.",
                    "physical_look": "Lean and weathered.",
                    "personality": "Quiet and watchful.",
                    "race": "Human",
                    "gender": "Male",
                    "height": "1.78 m",
                    "relationships": {
                        "member_of": ["Fellowship of the Ring"],
                        "allied_with": ["Gimli"],
                    },
                    "facts": ["Speaks the password.", "Leads the party."],
                    "session_facts": ["Sings at the door."],
                    "mentions": 5,
                }
            ],
            "locations": [],
            "events": [],
            "timeline_entries": [],
        },
    ]

    merged = merge_extractions(extractions)

    # deduped by normalized name
    assert len(merged["characters"]) == 1
    char = merged["characters"][0]
    assert char["name"] == "Aragorn"  # first-seen casing
    assert "Strider" in char["aliases"]
    # longest description wins
    assert char["description"] == "A ranger of the north, heir to a lost throne."
    # longest physical_look / personality win too (character page sections)
    assert char["physical_look"] == "Lean and weathered."
    assert char["personality"] == "Quiet and watchful."
    # v5 static info filled from the chunks that report it
    assert char["race"] == "Human"
    assert char["gender"] == "Male"
    assert char["height"] == "1.78 m"
    # durable relationships union across chunks, deduped per type
    assert char["relationships"] == {
        "member_of": ["Fellowship of the Ring"],
        "allied_with": ["Gimli"],
    }
    # facts deduped, order preserved
    assert char["facts"] == ["Leads the party.", "Speaks the password."]
    # session facts merged separately
    assert char["session_facts"] == ["Sings at the door."]
    # mentions summed
    assert char["mentions"] == 7
    # appeared in both chunks
    assert char["confidence"] == 1.0
    # longest summary wins
    assert merged["session_summary"] == "A much longer and more detailed summary of the session."
    # unanimous per-chunk language
    assert merged["language"] == "en"


def test_merge_language_majority_vote():
    extractions = [
        {"language": code, "session_summary": "s", "characters": [], "locations": [], "events": [], "timeline_entries": []}
        for code in ("it", "it", "en")
    ]
    assert merge_extractions(extractions)["language"] == "it"
    # no language reported at all -> empty string
    none_reported = merge_extractions(
        [{"session_summary": "s", "characters": [], "locations": [], "events": [], "timeline_entries": []}]
    )
    assert none_reported["language"] == ""


def test_merge_drops_generic_names():
    extractions = [
        {
            "language": "it",
            "session_summary": "s",
            "characters": [_entity("Guardia"), _entity("Aragorn")],
            "locations": [
                _entity("Città"),
                _entity("la città"),
                _entity("the city"),
                _entity("Old Town"),  # 'old' is not a generic location word
                _entity("Fatumastra"),
            ],
            "events": [],
            "timeline_entries": [],
        }
    ]
    merged = merge_extractions(extractions)
    assert [c["name"] for c in merged["characters"]] == ["Aragorn"]
    # same mention count -> alphabetical
    assert [loc["name"] for loc in merged["locations"]] == ["Fatumastra", "Old Town"]


def test_is_generic_name():
    assert is_generic_name("Città", "location")
    assert is_generic_name("the city", "location")
    assert is_generic_name("La Città", "location")
    assert is_generic_name("l'osteria", "location")
    assert is_generic_name("The Guard", "character")
    assert not is_generic_name("Città di Fatumastra", "location")
    assert not is_generic_name("Old Town", "location")
    assert not is_generic_name("Fatumastra", "location")
    assert not is_generic_name("Aragorn", "character")
    # v6: generic nouns / "main street"-style phrases without an anchor
    assert is_generic_name("Ospedale", "location")
    assert is_generic_name("Strada Maestra", "location")
    assert is_generic_name("main street", "location")
    assert not is_generic_name("Ospedale di Fatumastra", "location")
    assert not is_generic_name("Strada Maestra di Fatumastra", "location")


def test_merge_events_stay_in_the_summary():
    """Events are merged for the SESSION page only - never wiki drafts."""
    extractions = [
        {
            "language": "en",
            "session_summary": "s",
            "characters": [],
            "locations": [],
            "events": [
                {
                    "title": "Battle of the Gate",
                    "description": "The orcs attack.",
                    "participants": ["Aragorn"],
                }
            ],
            "timeline_entries": [],
        },
        {
            "language": "en",
            "session_summary": "s",
            "characters": [],
            "locations": [],
            "events": [
                {
                    "title": "battle of the gate",
                    "description": "The orcs attack in waves, then retreat.",
                    "participants": ["Aragorn", "Gimli"],
                }
            ],
            "timeline_entries": [],
        },
    ]
    merged = merge_extractions(extractions)
    assert len(merged["events"]) == 1
    event = merged["events"][0]
    assert event["title"] == "Battle of the Gate"
    assert event["participants"] == ["Aragorn", "Gimli"]
    assert event["confidence"] == 1.0

    drafts, relations, _ = build_page_drafts(merged, "c", "s")
    assert drafts == []  # no event pages anymore
    assert relations == []


def test_merge_timeline_dedupes_and_sorts():
    extractions = [
        {
            "session_summary": "s",
            "characters": [],
            "locations": [],
            "events": [],
            "timeline_entries": [
                {"time": "00:10:00", "summary": "The battle ends.", "characters": []},
                {"time": "00:02:00", "summary": "The gate opens.", "characters": ["Aragorn"]},
                {"time": "00:02:00", "summary": "The gate opens.", "characters": ["Aragorn"]},
            ],
        }
    ]
    merged = merge_extractions(extractions)
    times = [e["time"] for e in merged["timeline_entries"]]
    assert times == ["00:02:00", "00:10:00"]  # sorted, deduped


def test_merge_overall_confidence():
    merged = merge_extractions(
        [
            {
                "session_summary": "s",
                "characters": [
                    {"name": "Arwen", "description": "d", "mentions": 1}
                ],
                "locations": [],
                "events": [],
                "timeline_entries": [],
            },
            {
                "session_summary": "s",
                "characters": [
                    {"name": "Arwen", "description": "d", "mentions": 1},
                    {"name": "Beregond", "description": "d", "mentions": 1},
                ],
                "locations": [],
                "events": [],
                "timeline_entries": [],
            },
        ]
    )
    # Arwen in both chunks (1.0), Beregond in one (0.5) -> mean 0.75
    assert merged["confidence"] == 0.75


# ------------------------------------------------------- category attributes


def test_map_location_type():
    assert map_location_type("City") == "city"
    assert map_location_type("città") == "city"
    assert map_location_type("paese") == "village"
    assert map_location_type("osteria") == "building"
    assert map_location_type("miniera") == "dungeon"
    assert map_location_type("foresta") == "wilderness"
    # v8: world / continent / structure are first-class location types
    assert map_location_type("mondo") == "world"
    assert map_location_type("World") == "world"
    assert map_location_type("continente") == "continent"
    assert map_location_type("struttura") == "structure"
    assert map_location_type("metropoli") == "city"
    assert map_location_type("") is None
    assert map_location_type("qualcosa di strano") is None  # omitted, not guessed


def test_build_page_drafts():
    merged = merge_extractions([make_extraction()])
    drafts, relations, duplicates = build_page_drafts(
        merged, "22222222-2222-2222-2222-222222222222", "11111111-1111-1111-1111-111111111111"
    )

    kinds = [d["kind"] for d in drafts]
    assert kinds == ["character", "location"]  # no session_note / event pages

    by_kind = {d["kind"]: d for d in drafts}
    character = by_kind["character"]
    assert character["title"] == "Aragorn"
    assert character["status"] == "pending_review"
    assert character["source_session_id"] == "11111111-1111-1111-1111-111111111111"
    # characters have no Summary block: appearance + temperament carry the page
    assert "summary" not in character["content_json"]
    assert character["content_json"]["physical_look"] == "Tall and weathered, with grey eyes."
    assert character["content_json"]["personality"] == "Wary of strangers, loyal to the party."
    assert character["content_json"]["language"] == "en"
    assert "Strider" in character["content_json"]["aliases"]
    # durable facts stay on their own; session specifics cite the session
    assert character["content_json"]["facts"] == ["Speaks the password."]
    assert character["content_json"]["session_references"] == [
        {"session_id": "11111111-1111-1111-1111-111111111111",
         "facts": ["Sings 'friend' at the west-gate door."]}
    ]
    # no party info -> plain NPC; static info auto-filled from the session
    assert character["content_json"]["attributes"] == {
        "character_type": "npc", "race": "Human", "gender": "Male"
    }
    assert character["confidence"] == 1.0

    location = by_kind["location"]
    assert location["title"] == "Moria"
    assert location["content_json"]["session_references"] == [
        {"session_id": "11111111-1111-1111-1111-111111111111",
         "facts": ["Its west-door opened to the password."]}
    ]
    # place_type 'mine' maps to dungeon; nothing else known -> just that field
    assert location["content_json"]["attributes"] == {"location_type": "dungeon"}

    assert duplicates == []
    assert relations == []  # appears_in is gone with the event pages


def test_build_page_drafts_tags_party_characters_by_name():
    merged = merge_extractions([make_extraction()])
    drafts, _, _ = build_page_drafts(
        merged, "c", "s", party_characters=["Aragorn", "Gimli"]
    )
    character = next(d for d in drafts if d["kind"] == "character")
    assert character["content_json"]["attributes"]["character_type"] == "player"
    assert character["content_json"]["attributes"]["race"] == "Human"
    location = next(d for d in drafts if d["kind"] == "location")
    assert "attributes" not in location["content_json"] or \
        "character_type" not in location["content_json"].get("attributes", {})


def test_build_page_drafts_tags_party_by_alias():
    """The page may be titled by the character name while the member data
    carries an alias (or vice versa): matching goes through aliases too."""
    merged = merge_extractions([make_extraction()])
    drafts, _, _ = build_page_drafts(merged, "c", "s", party_characters=["Strider"])
    character = next(d for d in drafts if d["kind"] == "character")
    assert character["content_json"]["attributes"]["character_type"] == "player"


def test_build_page_drafts_tags_party_by_model_hint():
    extraction = make_extraction()
    extraction["characters"][0]["is_party"] = True
    merged = merge_extractions([extraction])
    assert merged["characters"][0]["is_party"] is True
    drafts, _, _ = build_page_drafts(merged, "c", "s")
    character = next(d for d in drafts if d["kind"] == "character")
    assert character["content_json"]["attributes"]["character_type"] == "player"


def test_build_page_drafts_location_region_attribute():
    extraction = make_extraction()
    extraction["locations"][0]["place_type"] = "Città"  # unmapped casing/alias net
    extraction["locations"][0]["part_of"] = "Terra di Mezzo"
    extraction["locations"][0]["name"] = "Fatumastra"
    merged = merge_extractions([extraction])
    drafts, _, _ = build_page_drafts(merged, "c", "s")
    location = next(d for d in drafts if d["kind"] == "location")
    assert location["content_json"]["attributes"] == {
        "location_type": "city",
        "region": "Terra di Mezzo",
    }


def test_merge_keeps_location_detail_fields_across_chunks():
    """v8: type-specific fields merge like the other scalars (longest wins)
    and proper-name lists union across chunks."""
    extractions = [
        {
            "language": "it",
            "session_summary": "s",
            "characters": [],
            "locations": [
                {
                    "name": "Fatumastra",
                    "aliases": [],
                    "description": "La città portuale.",
                    "place_type": "città",
                    "population": "12.000 abitanti",
                    "districts": ["Porto", "Alto Quartiere"],
                    "notable_locations": ["Molo Vecchio"],
                    "history": "Fondata dai primi coloni.",
                    "facts": [],
                    "session_facts": [],
                    "mentions": 2,
                }
            ],
            "events": [],
            "timeline_entries": [],
        },
        {
            "language": "it",
            "session_summary": "s",
            "characters": [],
            "locations": [
                {
                    "name": "Fatumastra",
                    "aliases": [],
                    "description": "La città portuale della costa.",
                    "place_type": "città",
                    "population": "circa 12.000 abitanti",
                    "districts": ["Porto"],
                    "notable_locations": ["Molo Vecchio", "Piazza del Mercato"],
                    "history": "Fondata dai primi coloni giunti via mare.",
                    "facts": [],
                    "session_facts": [],
                    "mentions": 3,
                }
            ],
            "events": [],
            "timeline_entries": [],
        },
    ]
    merged = merge_extractions(extractions)
    location = merged["locations"][0]
    assert location["population"] == "circa 12.000 abitanti"  # longest wins
    assert location["history"] == "Fondata dai primi coloni giunti via mare."
    assert location["districts"] == ["Porto", "Alto Quartiere"]  # union, deduped
    assert location["notable_locations"] == ["Molo Vecchio", "Piazza del Mercato"]
    assert location["place_type"] == "città"


def test_build_page_drafts_location_type_specific_attributes():
    """A city draft carries its settlement stats; a region draft keeps only
    region-valid fields (population on a region must never be emitted)."""
    city = _entity(
        "Fatumastra",
        place_type="city",
        part_of="Costa orientale",
        population="12,000",
        government="Council of Elders",
        ruler="Burgomaster Aldo",
        districts=["Porto", "Alto Quartiere"],
        notable_locations=["Molo Vecchio"],
        founded="1290 DR",
    )
    region = _entity(
        "Costa orientale",
        place_type="region",
        population="12,000",  # not a region field -> must be dropped
        capital="Fatumastra",
        terrain="Colline e costa",
        climate="Temperato",
        notable_locations=["Fatumastra"],
    )
    merged = {
        "language": "it",
        "characters": [],
        "locations": [city, region],
        "events": [],
        "timeline_entries": [],
    }
    drafts, _, _ = build_page_drafts(merged, "c", "s")
    by_title = {d["title"]: d for d in drafts if d["kind"] == "location"}

    city_attrs = by_title["Fatumastra"]["content_json"]["attributes"]
    assert city_attrs == {
        "location_type": "city",
        "region": "Costa orientale",
        "founded": "1290 DR",
        "population": "12,000",
        "government": "Council of Elders",
        "ruler": "Burgomaster Aldo",
        "districts": ["Porto", "Alto Quartiere"],
        "notable_locations": ["Molo Vecchio"],
    }

    region_attrs = by_title["Costa orientale"]["content_json"]["attributes"]
    assert region_attrs == {
        "location_type": "region",
        "capital": "Fatumastra",
        "terrain": "Colline e costa",
        "climate": "Temperato",
        "notable_locations": ["Fatumastra"],
    }
    assert "population" not in region_attrs


def test_build_page_drafts_location_world_and_dungeon_attributes():
    """Worlds keep pantheon/planes; dungeons keep entrance/levels/hazards."""
    world = _entity("Terra di Mezzo", place_type="mondo", pantheon="Dodici Dei", planes="Piano Materiale")
    dungeon = _entity("Minas Morgul", place_type="dungeon", entrance="Porta ovest", levels="3", hazards="Trappole")
    merged = {
        "language": "it",
        "characters": [],
        "locations": [world, dungeon],
        "events": [],
        "timeline_entries": [],
    }
    drafts, _, _ = build_page_drafts(merged, "c", "s")
    by_title = {d["title"]: d for d in drafts if d["kind"] == "location"}
    assert by_title["Terra di Mezzo"]["content_json"]["attributes"] == {
        "location_type": "world", "pantheon": "Dodici Dei", "planes": "Piano Materiale"
    }
    assert by_title["Minas Morgul"]["content_json"]["attributes"] == {
        "location_type": "dungeon", "entrance": "Porta ovest", "levels": "3", "hazards": "Trappole"
    }


def test_build_page_drafts_location_history_section():
    """The narrative 'history' lands on the page as a cross-type section."""
    extraction = make_extraction()
    extraction["locations"][0]["history"] = "Fondata dai primi coloni."
    merged = merge_extractions([extraction])
    drafts, _, _ = build_page_drafts(merged, "c", "s")
    location = next(d for d in drafts if d["kind"] == "location")
    assert location["content_json"]["history"] == "Fondata dai primi coloni."


def test_build_page_drafts_unknown_place_type_omitted():
    extraction = make_extraction()
    extraction["locations"][0]["place_type"] = "qualcosa di strano"
    merged = merge_extractions([extraction])
    drafts, _, _ = build_page_drafts(merged, "c", "s")
    location = next(d for d in drafts if d["kind"] == "location")
    assert location["content_json"].get("attributes") in (None, {"region": ""})
    assert "location_type" not in (location["content_json"].get("attributes") or {})


def test_build_page_drafts_exact_match_skips_duplicate():
    """An entity the wiki already documents is not re-drafted; its new facts
    remain visible on the persisted session summary (session page)."""
    merged = merge_extractions([make_extraction()])
    existing = [
        {
            "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "title": "Moria",
            "slug": "moria",
            "kind": "location",
            "status": "published",
            "aliases": ["Mines of Moria"],
        }
    ]
    drafts, relations, duplicates = build_page_drafts(
        merged, "22222222-2222-2222-2222-222222222222", "11111111-1111-1111-1111-111111111111",
        existing_pages=existing,
    )

    kinds = sorted(d["kind"] for d in drafts)
    assert kinds == ["character"]  # no Moria draft, no session note

    assert duplicates == [
        {
            "title": "Moria",
            "kind": "location",
            "matched_page_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "matched_title": "Moria",
        }
    ]
    assert relations == []


def test_build_page_drafts_alias_match_skips_duplicate():
    merged = merge_extractions([make_extraction()])
    existing = [
        {
            "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "title": "Strider",  # page titled by the alias
            "slug": "strider",
            "kind": "character",
            "status": "pending_review",
            "aliases": [],
        }
    ]
    drafts, _, duplicates = build_page_drafts(merged, "c", "s", existing_pages=existing)
    assert all(d["title"] != "Aragorn" for d in drafts)
    assert duplicates[0]["title"] == "Aragorn"
    assert duplicates[0]["matched_page_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def test_build_page_drafts_near_match_gets_possible_duplicate_relation():
    extraction = make_extraction()
    extraction["locations"][0]["name"] = "Città di Fatumastra"
    merged = merge_extractions([extraction])
    existing = [
        {
            "id": "ffffeeee-eeee-eeee-eeee-eeeeeeeeeee1",
            "title": "Fatumastra",
            "slug": "fatumastra",
            "kind": "location",
            "status": "published",
            "aliases": [],
        }
    ]
    drafts, relations, duplicates = build_page_drafts(merged, "c", "s", existing_pages=existing)

    # near match: draft IS created (DM decides), plus a relation proposal
    assert any(d["title"] == "Città di Fatumastra" and d["kind"] == "location" for d in drafts)
    assert duplicates == []
    duplicate_relations = [r for r in relations if r["relation_type"] == POSSIBLE_DUPLICATE]
    assert duplicate_relations == [
        {
            "from_title": "Città di Fatumastra",
            "to_page_id": "ffffeeee-eeee-eeee-eeee-eeeeeeeeeee1",
            "relation_type": POSSIBLE_DUPLICATE,
        }
    ]


def test_build_page_drafts_proposes_durable_relationships():
    """Extracted persistent ties become relation proposals (from/to by title);
    self-references and junk types are dropped."""
    extraction = make_extraction()
    extraction["characters"] = [
        {
            "name": "Aragorn",
            "aliases": [],
            "description": "A ranger.",
            "facts": [],
            "session_facts": [],
            "mentions": 2,
            "relationships": {
                "member_of": ["Fellowship of the Ring", "Aragorn"],  # self -> dropped
                "allied_with": ["Gimli"],
                "owner": ["Locanda del Fumo Aspro"],
                "met_today": ["Gimli"],  # not a persistent type -> dropped
            },
        },
        {
            "name": "Gimli",
            "aliases": [],
            "description": "A dwarf.",
            "facts": [],
            "session_facts": [],
            "mentions": 1,
            "relationships": {"led_by": ["Aragorn"]},
        },
    ]
    extraction["locations"] = []
    merged = merge_extractions([extraction])
    drafts, relations, _ = build_page_drafts(merged, "c", "s")

    assert [d["title"] for d in drafts] == ["Aragorn", "Gimli"]
    durable = [r for r in relations if r["relation_type"] != POSSIBLE_DUPLICATE]
    assert durable == [
        {
            "from_title": "Aragorn",
            "to_title": "Fellowship of the Ring",
            "relation_type": "member_of",
        },
        {
            "from_title": "Aragorn",
            "to_title": "Gimli",
            "relation_type": "allied_with",
        },
        {
            "from_title": "Aragorn",
            "to_title": "Locanda del Fumo Aspro",
            "relation_type": "owner",
        },
        {
            "from_title": "Gimli",
            "to_title": "Aragorn",
            "relation_type": "led_by",
        },
    ]


def test_match_existing_page_prefers_exact_over_near():
    pages = [
        {"id": "1", "title": "Bree", "slug": "bree", "kind": "location", "status": "published"},
        {"id": "2", "title": "Bree-under-Bree", "slug": "x", "kind": "location", "status": "published"},
    ]
    exact, near = match_existing_page("the Bree", [], pages)
    assert exact is not None and exact["id"] == "1"
    assert near is None


def make_extraction() -> dict:
    return {
        "language": "en",
        "session_summary": "The party reaches the gates of Moria.",
        "characters": [
            {
                "name": "Aragorn",
                "aliases": ["Strider"],
                "description": "A ranger of the north.",
                "physical_look": "Tall and weathered, with grey eyes.",
                "personality": "Wary of strangers, loyal to the party.",
                "race": "Human",
                "gender": "Male",
                "facts": ["Speaks the password."],
                "session_facts": ["Sings 'friend' at the west-gate door."],
                "mentions": 3,
            }
        ],
        "locations": [
            {
                "name": "Moria",
                "aliases": [],
                "description": "An ancient dwarven mine.",
                "facts": [],
                "session_facts": ["Its west-door opened to the password."],
                "place_type": "mine",
                "part_of": "",
                "mentions": 2,
            }
        ],
        "events": [
            {
                "title": "Entering Moria",
                "description": "The party passes the gate.",
                "participants": ["Aragorn"],
            }
        ],
        "timeline_entries": [
            {"time": "00:00:12", "summary": "The gate opens.", "characters": ["Aragorn"]}
        ],
    }

# ------------------------------------------------------- v6 quality nets


def test_merge_renames_generic_location_with_named_anchor():
    """'Ospedale' + part_of 'Fatumastra' -> unique proper name 'Ospedale di
    Fatumastra'; generic aliases are dropped so dedupe cannot collide."""
    extraction = {
        "language": "it",
        "session_summary": "s",
        "characters": [],
        "locations": [
            {
                "name": "Ospedale",
                "aliases": ["l'ospedale"],
                "description": "L'ospedale principale della città.",
                "facts": [],
                "session_facts": [],
                "place_type": "building",
                "part_of": "Fatumastra",
                "mentions": 3,
            }
        ],
        "events": [],
        "timeline_entries": [],
    }
    merged = merge_extractions([extraction])
    assert [loc["name"] for loc in merged["locations"]] == ["Ospedale di Fatumastra"]
    assert merged["locations"][0]["aliases"] == []  # generic alias dropped
    drafts, _, _ = build_page_drafts(merged, "c", "s")
    assert drafts[0]["title"] == "Ospedale di Fatumastra"
    assert drafts[0]["content_json"]["attributes"] == {
        "location_type": "building",
        "region": "Fatumastra",
    }


def test_merge_renames_generic_location_english():
    extraction = {
        "language": "en",
        "session_summary": "s",
        "characters": [],
        "locations": [
            {"name": "Hospital", "aliases": [], "description": "d", "facts": [],
             "session_facts": [], "place_type": "building", "part_of": "Fatumastra",
             "mentions": 2}
        ],
        "events": [],
        "timeline_entries": [],
    }
    merged = merge_extractions([extraction])
    assert merged["locations"][0]["name"] == "Hospital of Fatumastra"


def test_merge_drops_generic_location_without_anchor():
    """No named anchor -> the generic name stays ambiguous; no page."""
    extraction = {
        "language": "it",
        "session_summary": "s",
        "characters": [],
        "locations": [
            {"name": "Ospedale", "aliases": [], "description": "d", "facts": [],
             "session_facts": [], "place_type": "building", "part_of": "", "mentions": 5},
            {"name": "Strada Maestra", "aliases": [], "description": "d", "facts": [],
             "session_facts": [], "place_type": "building", "part_of": "la città", "mentions": 4},
            {"name": "Fatumastra", "aliases": [], "description": "d", "facts": [],
             "session_facts": [], "place_type": "city", "part_of": "", "mentions": 3},
        ],
        "events": [],
        "timeline_entries": [],
    }
    merged = merge_extractions([extraction])
    assert [loc["name"] for loc in merged["locations"]] == ["Fatumastra"]


def test_merge_drops_narrator_characters():
    """The DM / narrator is out of the world: never a character page."""
    extraction = {
        "language": "en",
        "session_summary": "s",
        "characters": [
            _entity("Dungeon Master"),
            _entity("DM"),
            _entity("Il Narratore"),
            _entity("Master"),
            _entity("Aragorn"),
        ],
        "locations": [],
        "events": [],
        "timeline_entries": [],
    }
    merged = merge_extractions([extraction])
    assert [c["name"] for c in merged["characters"]] == ["Aragorn"]


def test_merge_blank_filler_text():
    """Chunk-referencing text and bare filler never reach the page body."""
    extraction = {
        "language": "it",
        "session_summary": "s",
        "characters": [
            {
                "name": "Aragorn",
                "aliases": [],
                "description": "Membro del gruppo, non interviene in questo frammento.",
                "physical_look": "Non appare in questo frammento.",
                "personality": "Distinto e affascinato dalla creatura.",
                "facts": ["Guida il gruppo.", "Non interviene."],
                "session_facts": ["Non appare in questa sessione."],
                "mentions": 2,
            }
        ],
        "locations": [],
        "events": [],
        "timeline_entries": [],
    }
    merged = merge_extractions([extraction])
    char = merged["characters"][0]
    assert char["description"] == ""
    assert char["physical_look"] == ""
    # vague but not chunk-referencing -> kept for the DM to review
    assert char["personality"] == "Distinto e affascinato dalla creatura."
    assert char["facts"] == ["Guida il gruppo."]
    assert char["session_facts"] == []


def test_filler_exact_match_only():
    # "non interviene" inside a longer statement is real content, not padding
    assert not is_filler_text("Non interviene mai nelle discussioni.")
    assert is_filler_text("Non interviene.")
    assert is_filler_text("Non interviene in questo frammento.")
    assert is_filler_text("Membro del gruppo, non interviene in questo frammento.")


def test_exclude_character_names():
    merged = {
        "language": "en",
        "characters": [{"name": "Aragorn"}, {"name": "Dungeon Master"}],
        "locations": [],
    }
    out = exclude_character_names(merged, ["Dungeon Master"])
    assert [c["name"] for c in out["characters"]] == ["Aragorn"]
    # nothing matched -> the same object comes back untouched
    assert exclude_character_names(merged, ["Nobody"]) is merged


# ------------------------------------------------------------- event drafts


def _merged_with_event(**overrides):
    merged = {
        "language": "en",
        "events": [
            {
                "title": "The Siege of Fatumastra",
                "description": "The horde breaks against the city walls.",
                "participants": ["Lyra", "Captain Marta"],
                "in_world_date": "17 Ches 1492 DR",
                "event_type": "battle",
                "confidence": 0.9,
            }
        ],
    }
    merged.update(overrides)
    return merged


def test_build_event_drafts_new_event_gets_page_and_timeline():
    drafts, updates, timeline_events, duplicates = build_event_drafts(
        _merged_with_event(), "campaign-1", "session-1", existing_pages=[]
    )
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["kind"] == "event"
    assert draft["title"] == "The Siege of Fatumastra"
    assert draft["status"] == "pending_review"
    assert draft["source_session_id"] == "session-1"
    assert draft["content_json"]["summary"] == "The horde breaks against the city walls."
    assert draft["content_json"]["language"] == "en"
    assert draft["content_json"]["attributes"] == {
        "event_type": "battle",
        "in_world_date": "17 Ches 1492 DR",
        "participants": ["Lyra", "Captain Marta"],
    }
    assert draft["content_json"]["session_references"] == [
        {"session_id": "session-1", "facts": ["The horde breaks against the city walls."]}
    ]

    assert updates == []
    assert len(timeline_events) == 1
    entry = timeline_events[0]
    assert entry["title"] == "The Siege of Fatumastra"
    assert entry["page_id"] is None  # resolved from the draft after creation
    assert entry["in_world_date"] == "17 Ches 1492 DR"
    assert entry["summary"] == "The horde breaks against the city walls."
    assert duplicates == []


def test_build_event_drafts_updates_existing_event_page():
    existing = [
        {
            "id": "event-page-1",
            "title": "The Siege of Fatumastra",
            "kind": "event",
            "status": "published",
            "content_json": {
                "summary": "The horde breaks against the city walls.",
                "attributes": {
                    "participants": ["Lyra"],
                    # DM-curated metadata must survive the auto-merge
                    "event_status": "resolved",
                },
            },
        }
    ]
    drafts, updates, timeline_events, duplicates = build_event_drafts(
        _merged_with_event(), "campaign-1", "session-1", existing_pages=existing
    )
    assert drafts == []
    assert duplicates == []
    assert len(updates) == 1
    update = updates[0]
    assert update["page_id"] == "event-page-1"
    # participants merged, DM state preserved, nothing lost
    attrs = update["content_json"]["attributes"]
    assert attrs["participants"] == ["Lyra", "Captain Marta"]
    assert attrs["event_status"] == "resolved"
    assert attrs["event_type"] == "battle"
    assert attrs["in_world_date"] == "17 Ches 1492 DR"
    assert update["content_json"]["summary"] == "The horde breaks against the city walls."
    assert len(update["content_json"]["session_references"]) == 1

    assert len(timeline_events) == 1
    assert timeline_events[0]["page_id"] == "event-page-1"


def test_build_event_drafts_title_collision_with_non_event_page():
    existing = [
        {
            "id": "location-1",
            "title": "The Siege of Fatumastra",
            "kind": "location",
            "status": "published",
        }
    ]
    drafts, updates, timeline_events, duplicates = build_event_drafts(
        _merged_with_event(), "campaign-1", "session-1", existing_pages=existing
    )
    assert drafts == []
    assert updates == []
    assert timeline_events == []
    assert len(duplicates) == 1
    assert duplicates[0]["matched_page_id"] == "location-1"