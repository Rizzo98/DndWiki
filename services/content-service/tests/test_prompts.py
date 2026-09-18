"""Tests for the versioned prompt + extraction schema."""

import json

from app.prompts import (
    EXTRACTION_SCHEMA,
    PROMPT_VERSION,
    SUMMARY_REVISION_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_chunk_message,
    build_summary_revision_message,
)


def test_prompt_version_is_v15():
    # v12 adds source_refs to every extracted item and an explicit actor to
    # every event, which is what lets the attribution gate VERIFY a fact
    # instead of trusting it (docs/attribution-model.md S14.3).
    # v13 reads the '[Stretches]' note: in a stretch where the record puts
    # exactly one party member present, a beat about a party member is about THAT
    # member, and an unnamed actor ("un personaggio") is never acceptable.
    # v14 makes the summary revision answer with a PATCH (the complete summary
    # plus the items the correction touches) instead of echoing the whole
    # extraction back: the echo did not fit the completion cap, so its tail -
    # the events among it - was silently replaced by the previous revision's.
    # v15 turns the summary into a NARRATIVE in scene blocks and the review into
    # highlighting portions of it: independent one-beat lines read as if the
    # creature of one line were not the character named in the next.
    assert PROMPT_VERSION == "v15"


def test_the_stretches_note_is_explained_to_the_model():
    assert "[Stretches]" in SYSTEM_PROMPT
    assert "un personaggio" in SYSTEM_PROMPT


def test_every_extractable_item_carries_source_refs():
    schema = EXTRACTION_SCHEMA["properties"]
    for kind in ("characters", "locations", "events", "timeline_entries"):
        item = schema[kind]["items"]["properties"]
        assert "source_refs" in item, kind
        assert item["source_refs"]["type"] == "array"


def test_events_carry_an_explicit_actor_that_may_be_empty():
    actor = EXTRACTION_SCHEMA["properties"]["events"]["items"]["properties"]["actor"]
    assert actor["type"] == "string"
    assert "EMPTY" in actor["description"]


def test_the_prompt_explains_the_three_line_forms():
    """A '?' line and an '(unattributed)' line mean different things, and the
    model must be told so: they are the whole point of the attributed view."""
    assert "Aramil?" in SYSTEM_PROMPT
    assert "(unattributed)" in SYSTEM_PROMPT
    assert "NEVER attribute" in SYSTEM_PROMPT
    assert "source_refs" in SYSTEM_PROMPT
    assert "SPEAKER_00" in SYSTEM_PROMPT  # ... and never write one as a name


def test_summary_is_a_list_of_lines():
    # v11: the summary is the reviewable layer - one beat per line, so the DM
    # can select and correct single lines on the session page
    assert "one beat per line" in SYSTEM_PROMPT
    description = EXTRACTION_SCHEMA["properties"]["session_summary"]["description"]
    assert "lines" in description


def test_schema_requires_all_categories():
    required = set(EXTRACTION_SCHEMA["required"])
    assert {
        "language",
        "session_summary",
        "characters",
        "locations",
        "events",
        "timeline_entries",
    } <= required


def test_entities_carry_both_fact_kinds():
    for kind in ("characters", "locations"):
        properties = EXTRACTION_SCHEMA["properties"][kind]["items"]["properties"]
        assert "facts" in properties
        assert "session_facts" in properties


def test_characters_carry_party_hint():
    # player vs NPC: the model flags party members; the merger also matches
    # against the campaign's member character names deterministically
    properties = EXTRACTION_SCHEMA["properties"]["characters"]["items"]["properties"]
    assert "is_party" in properties


def test_characters_carry_static_info_and_relationships():
    # v5: the page's static info table is auto-filled from the session, and
    # durable relationships map to wiki relations (membership/alliance/lead)
    properties = EXTRACTION_SCHEMA["properties"]["characters"]["items"]["properties"]
    for field in ("race", "class", "gender", "height", "weight", "age"):
        assert field in properties
    assert "relationships" in properties
    rel_types = properties["relationships"]["properties"]
    assert {"member_of", "allied_with", "led_by", "owner"} <= set(rel_types)


def test_system_prompt_keeps_relationships_persistent():
    # a relationship must be a tie that survives the session - never an event
    assert "PERSISTENT" in SYSTEM_PROMPT
    assert "session_facts" in SYSTEM_PROMPT
    assert "relationship" in SYSTEM_PROMPT.lower()


def test_locations_carry_geospatial_hints():
    properties = EXTRACTION_SCHEMA["properties"]["locations"]["items"]["properties"]
    assert "place_type" in properties
    assert "part_of" in properties


def test_locations_carry_type_specific_detail_fields():
    # v8: the location page renders structured sections from these fields —
    # settlements (population/government/...), regions (capital/terrain/...),
    # worlds (pantheon/planes), buildings (owner/purpose), dungeons
    # (entrance/levels/hazards), wilderness (terrain/climate/hazards/...)
    properties = EXTRACTION_SCHEMA["properties"]["locations"]["items"]["properties"]
    for field in (
        "history", "founded", "population", "government", "ruler",
        "demographics", "economy", "defenses", "religion", "districts",
        "notable_locations", "capital", "terrain", "climate", "pantheon",
        "planes", "owner", "purpose", "entrance", "levels", "hazards",
        "flora_fauna",
    ):
        assert field in properties, field


def test_system_prompt_scopes_location_detail_fields():
    # detail fields must be filled only when the chunk states them; facts
    # that fit a field go there, not into 'facts'
    assert "population" in SYSTEM_PROMPT
    assert "never guess" in SYSTEM_PROMPT
    assert "history" in SYSTEM_PROMPT


def test_events_carry_date_and_type():
    # v9: world events become wiki pages + timeline entries; they carry the
    # campaign date when stated and a coarse event type
    properties = EXTRACTION_SCHEMA["properties"]["events"]["items"]["properties"]
    assert "in_world_date" in properties
    assert "event_type" in properties


def test_system_prompt_limits_events_to_world_significant():
    # the timeline must stay meaningful: routine session activity is excluded
    assert "WORLD-SIGNIFICANT" in SYSTEM_PROMPT
    assert "When in doubt" in SYSTEM_PROMPT
    assert "timeline" in SYSTEM_PROMPT.lower()
    # concrete exclusion examples for the model
    assert "shopping" in SYSTEM_PROMPT
    assert "travel" in SYSTEM_PROMPT
    # included kinds spelled out
    assert "sieges" in SYSTEM_PROMPT
    assert "betrayals" in SYSTEM_PROMPT


def test_schema_is_serializable_and_valid_json():
    # the system prompt embeds the schema as JSON; it must round-trip
    payload = json.loads(json.dumps(EXTRACTION_SCHEMA))
    assert payload["type"] == "object"


def test_system_prompt_contains_schema():
    assert "session_summary" in SYSTEM_PROMPT
    assert "timeline_entries" in SYSTEM_PROMPT


def test_system_prompt_asks_for_transcript_language():
    # language consistency: output in the players' language, keep names as-is
    assert "language" in SYSTEM_PROMPT
    assert "Never translate proper names" in SYSTEM_PROMPT
    assert "same" in SYSTEM_PROMPT  # 'that same language'


def test_system_prompt_rejects_generic_entity_names():
    # proper-name rule with concrete English + Italian examples
    assert "PROPER NAME" in SYSTEM_PROMPT
    assert "città" in SYSTEM_PROMPT.lower()
    assert "the inn" in SYSTEM_PROMPT


def test_system_prompt_explains_facts_vs_session_facts():
    assert "DURABLE" in SYSTEM_PROMPT
    assert "ONE-SHOT" in SYSTEM_PROMPT
    assert "session_facts" in SYSTEM_PROMPT


def test_system_prompt_explains_player_characters():
    # wiki convention: players appear under their CHARACTER name only
    assert "Party (player characters)" in SYSTEM_PROMPT
    assert "character name" in SYSTEM_PROMPT.lower()


def test_build_chunk_message_has_context():
    message = build_chunk_message("[00:00:00] Aragorn: hi", 0, 3)
    assert "chunk 1 of 3" in message
    assert "Aragorn: hi" in message


def test_build_chunk_message_lists_out_of_world_speakers():
    message = build_chunk_message("[00:00:00] Aragorn: hi", 0, 3, out_of_world=["Dungeon Master"])
    assert "Out-of-world speakers" in message
    assert "Dungeon Master" in message
    assert "never characters" in message
    # no out_of_world -> no narrator note
    assert "Out-of-world" not in build_chunk_message("view", 0, 1)


def test_system_prompt_is_cross_session():
    # the wiki must read like a standalone, cross-session entry: no chunk /
    # session / fragment references, no filler, no dangling references
    assert "CROSS-SESSION" in SYSTEM_PROMPT
    assert "in questo frammento" in SYSTEM_PROMPT
    assert "fragment" in SYSTEM_PROMPT.lower()


def test_system_prompt_excludes_the_narrator():
    # the DM narrates the world but is not part of it -> never a character
    assert "Dungeon Master" in SYSTEM_PROMPT
    assert "never" in SYSTEM_PROMPT.lower()


def test_system_prompt_personality_is_durable():
    # personality must be durable traits, never a scene reaction
    assert "DURABLE" in SYSTEM_PROMPT
    assert "creatura" in SYSTEM_PROMPT.lower()


def test_system_prompt_ownership_relations():
    # ownership is a durable relationship: "È il proprietario della Locanda
    # del Fumo Aspro" -> owner ["Locanda del Fumo Aspro"]
    assert "owner" in SYSTEM_PROMPT
    assert "Locanda del Fumo Aspro" in SYSTEM_PROMPT


def test_system_prompt_cross_references_are_linkable():
    # named entities must be referenced by their exact proper name so the
    # renderer can turn them into links
    assert "linkable" in SYSTEM_PROMPT.lower()
    assert "proper name" in SYSTEM_PROMPT.lower()


def test_system_prompt_requires_unique_location_names():
    # generic nouns alone ("Ospedale", "Strada Maestra") must be composed
    # with their named anchor or dropped
    assert "Ospedale di Fatumastra" in SYSTEM_PROMPT
    assert "Strada Maestra" in SYSTEM_PROMPT
    assert "PROPER" in SYSTEM_PROMPT

def test_revision_prompt_applies_corrections_everywhere():
    # the DM's correction must not survive only in the summary text: it has
    # to reach the entities, the events and the timeline entries too
    assert "EVERYWHERE" in SUMMARY_REVISION_SYSTEM_PROMPT
    assert "timeline entries" in SUMMARY_REVISION_SYSTEM_PROMPT
    assert "session_summary" in SUMMARY_REVISION_SYSTEM_PROMPT
    # the corrected wording comes from the DM and must not be narrated
    assert "Do not describe the correction itself" in SUMMARY_REVISION_SYSTEM_PROMPT
    # confidence numbers belong to the extraction and are never rewritten
    assert "confidence" in SUMMARY_REVISION_SYSTEM_PROMPT
    assert "EXTRACTION_SCHEMA" not in SUMMARY_REVISION_SYSTEM_PROMPT


def test_the_revision_prompt_reviews_portions_of_a_narrative():
    """v15: the DM highlights an arbitrary passage of the prose, so the prompt
    speaks of passages (never of lines) and always returns the whole story."""
    assert "PASSAGE of the narrative" in SUMMARY_REVISION_SYSTEM_PROMPT
    assert "with the mouse" in SUMMARY_REVISION_SYSTEM_PROMPT
    assert "WHOLE narrative, block by block" in SUMMARY_REVISION_SYSTEM_PROMPT
    assert '"location"' in SUMMARY_REVISION_SYSTEM_PROMPT
    # a wrong place is a correction too, and it lands on the block's label
    assert 'fix the "location" label' in SUMMARY_REVISION_SYSTEM_PROMPT
    assert "one beat per line" not in SUMMARY_REVISION_SYSTEM_PROMPT.lower()


def test_revision_prompt_asks_for_a_patch_not_for_the_extraction():
    """v14: echoing the whole extraction did not fit the completion cap, and
    its truncated tail was silently restored from the previous revision. The
    prompt therefore asks for the summary plus ONLY the items that change."""
    assert "PATCH" in SUMMARY_REVISION_SYSTEM_PROMPT
    for section in ("updates", "additions", "removals"):
        assert '"' + section + '"' in SUMMARY_REVISION_SYSTEM_PROMPT
    for kind in ("characters", "locations", "events", "timeline_entries"):
        assert kind in SUMMARY_REVISION_SYSTEM_PROMPT
    # the ids are the addressing scheme, and an unknown one is fatal
    assert '"c3"' in SUMMARY_REVISION_SYSTEM_PROMPT
    assert '"e1"' in SUMMARY_REVISION_SYSTEM_PROMPT
    assert "ids of the current extraction" in SUMMARY_REVISION_SYSTEM_PROMPT
    # NOT asked to repeat what it does not change
    assert "must not appear in" in SUMMARY_REVISION_SYSTEM_PROMPT


def test_build_summary_revision_message_lists_targets_and_instruction():
    current = {
        "language": "en",
        "session_summary": "Character A was going to the city center.",
        "characters": [],
        "locations": [],
        "events": [],
        "timeline_entries": [],
    }
    message = build_summary_revision_message(
        current,
        [{"targets": ["Character A was going to the city center."],
          "instruction": "It wasn't Character A, it was Character B"}],
    )
    assert "CURRENT EXTRACTION (JSON)" in message
    assert "Character A was going to the city center." in message
    assert "It wasn't Character A, it was Character B" in message
    assert "Passage(s) of the summary concerned" in message
    # the message tells the model how its answer is addressed
    assert "every item carrying its 'id'" in message


def test_build_summary_revision_message_accepts_dm_edited_text():
    # the client may send the narrative exactly as displayed by the DM
    current = {
        "session_summary": "old narrative",
        "summary_blocks": [{"location": "Locanda", "text": "old narrative"}],
        "characters": [],
        "locations": [],
        "events": [],
        "timeline_entries": [],
    }
    message = build_summary_revision_message(
        current,
        [{"targets": [], "instruction": "shorten it"}],
        summary_text_override="hand edited line one",
    )
    assert "hand edited line one" in message
    assert "old narrative" not in message
    # the blocks are rebuilt from the text the client sent, so the payload the
    # model sees never disagrees with itself
    assert "Locanda" not in message
    # no targets -> the request is about the whole summary
    assert "the whole session summary" in message


def test_build_summary_revision_message_survives_empty_edits():
    message = build_summary_revision_message({"session_summary": "x"})
    # the ask is the PATCH: the whole narrative plus only what changes
    assert "Return the patch JSON object described in your instructions" in message
    assert "whole narrative in 'session_summary'" in message
