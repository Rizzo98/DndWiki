"""Tests for the note -> change-set planner (pure function, no DB, no LLM).

The planner is where an untrusted reading of the DM's notes meets the campaign
as it actually is, so these tests are about the two decisions it owns: what is
NEW and what is an UPDATE, and what of the model's answer is fit to write.
"""

from app.note_planner import build_change_set


def page(page_id: str, title: str, kind: str = "character", **extra) -> dict:
    """One existing campaign page, in the shape wiki-service lists them."""
    return {
        "id": page_id,
        "title": title,
        "kind": kind,
        "status": "published",
        "aliases": extra.pop("aliases", []),
        "content_json": extra.pop("content_json", {}),
        **extra,
    }


def proposal(**overrides) -> dict:
    base = {"pages": [], "relations": [], "skipped": []}
    base.update(overrides)
    return base


# --------------------------------------------------------------- create/update


def test_a_note_about_something_new_creates_a_page():
    result = build_change_set(
        proposal(
            pages=[
                {
                    "kind": "character",
                    "title": "Kaelor",
                    "content": {"physical_look": "Grey-eyed.", "facts": ["Carries the ash blade."]},
                }
            ]
        )
    )
    assert len(result["changes"]) == 1
    change = result["changes"][0]
    assert change["id"] == "c1"
    assert change["action"] == "create"
    assert change["kind"] == "character"
    assert change["page_id"] is None
    assert change["before"] is None
    assert change["after"]["title"] == "Kaelor"
    assert change["after"]["content_json"]["physical_look"] == "Grey-eyed."
    assert change["after"]["visibility"] == "public"
    assert change["dropped"] is False
    assert result["skipped"] == []


def test_a_note_about_a_page_the_campaign_has_updates_it():
    existing = [
        page("p1", "Kaelor", content_json={"summary": "A ranger of the north."}),
    ]
    result = build_change_set(
        proposal(
            pages=[
                {
                    "kind": "character",
                    "title": "Kaelor",
                    "content": {"summary": "He now leads the Ash Company."},
                }
            ]
        ),
        existing_pages=existing,
    )
    change = result["changes"][0]
    assert change["action"] == "update"
    assert change["page_id"] == "p1"
    # the page keeps the name the campaign reads
    assert change["title"] == "Kaelor"
    assert change["before"] == {"title": "Kaelor", "content_json": {"summary": "A ranger of the north."}}
    # what the page said is kept and what the note adds is appended
    merged = change["after"]["content_json"]["summary"]
    assert "A ranger of the north." in merged
    assert "He now leads the Ash Company." in merged


def test_a_note_that_says_nothing_new_proposes_nothing():
    """A re-description is context, not a change.

    Folding it in would produce a page rewritten with its own content: the DM
    would be asked to confirm a no-op, and applying it would write a version and
    re-index the page for nothing. It is reported instead, so the DM still sees
    that the note was read.
    """
    existing = [page("p1", "Kaelor", content_json={"summary": "A ranger of the north."})]
    result = build_change_set(
        proposal(
            pages=[
                {
                    "kind": "character",
                    "title": "Kaelor",
                    "content": {"summary": "A ranger of the north."},
                }
            ]
        ),
        existing_pages=existing,
    )
    assert result["changes"] == []
    assert result["skipped"] == [
        {"title": "Kaelor", "reason": "the page already says this; the note adds nothing new"}
    ]


def test_a_note_that_adds_something_is_still_an_update():
    """The guard above must not swallow a note that DOES add something."""
    existing = [page("p1", "Kaelor", content_json={"summary": "A ranger of the north."})]
    result = build_change_set(
        proposal(
            pages=[
                {
                    "kind": "character",
                    "title": "Kaelor",
                    "content": {"summary": "A ranger of the north.", "facts": ["He leads the Ash Company."]},
                }
            ]
        ),
        existing_pages=existing,
    )
    assert len(result["changes"]) == 1
    assert result["changes"][0]["action"] == "update"
    assert result["changes"][0]["after"]["content_json"]["facts"] == ["He leads the Ash Company."]


def test_the_name_the_notes_used_becomes_an_alias_of_the_page_it_matched():
    """Otherwise the next generation proposes a second page for the same thing."""
    existing = [page("p1", "Concaverde", kind="location", content_json={"summary": "A swamp."})]
    result = build_change_set(
        proposal(
            pages=[
                {
                    "kind": "location",
                    "title": "Coca Verde",
                    "also_known_as": ["The Green Hollow"],
                    "content": {"summary": "Where the hag lives."},
                }
            ]
        ),
        existing_pages=existing,
    )
    change = result["changes"][0]
    # "Coca Verde" is not the same name as "Concaverde" but it is near enough to
    # be the same entity, so the note folds into that page and the name the note
    # used becomes an alias of it - which is what stops the next generation from
    # proposing a third page for the same swamp.
    assert change["action"] == "update"
    assert change["title"] == "Concaverde"
    aliases = change["after"]["content_json"]["aliases"]
    assert "Coca Verde" in aliases
    assert "The Green Hollow" in aliases
    assert "Concaverde" not in aliases  # a page is never its own alias
    assert change["before"]["content_json"] == {"summary": "A swamp."}


def test_a_page_whose_name_only_contains_the_new_one_is_a_duplicate_hint():
    """A note about the hospital in Fatumastra is not Fatumastra."""
    existing = [page("p1", "Fatumastra", kind="location", content_json={"summary": "A city."})]
    result = build_change_set(
        proposal(
            pages=[
                {
                    "kind": "location",
                    "title": "Ospedale di Fatumastra",
                    "content": {"summary": "A hospital."},
                }
            ]
        ),
        existing_pages=existing,
    )
    assert result["changes"][0]["action"] == "create"
    duplicates = [r for r in result["relations"] if r["relation_type"] == "possible_duplicate"]
    assert len(duplicates) == 1
    assert duplicates[0]["from_title"] == "Ospedale di Fatumastra"
    assert duplicates[0]["to_title"] == "Fatumastra"


def test_an_archived_page_is_not_matched():
    existing = [page("p1", "Kaelor", status="archived")]
    result = build_change_set(
        proposal(pages=[{"kind": "character", "title": "Kaelor", "content": {"summary": "x"}}]),
        existing_pages=existing,
    )
    assert result["changes"][0]["action"] == "create"


def test_a_title_collision_across_kinds_does_not_fold_into_the_wrong_page():
    """A location sharing a character's name is a collision, not the entity.

    The note's page is still created - the DM asked for it by writing the note,
    and wiki-service gives it its own slug ('bree' exists, so this one is
    'bree-2') - and the pair is linked with a 'possible_duplicate' hint so the DM
    can merge them if they turn out to be one thing.

    The link lands even though the two titles are IDENTICAL, which is worth
    pinning down because it looks like it should not work: the apply resolves
    'from_title' against the pages this change set creates and takes 'to_page_id'
    as given, so the pair resolves to two different pages.
    """
    existing = [page("p1", "Bree", kind="character")]
    result = build_change_set(
        proposal(pages=[{"kind": "location", "title": "Bree", "content": {"summary": "A town."}}]),
        existing_pages=existing,
    )
    assert [c["action"] for c in result["changes"]] == ["create"]
    assert result["changes"][0]["page_id"] is None
    duplicates = [r for r in result["relations"] if r["relation_type"] == "possible_duplicate"]
    assert len(duplicates) == 1
    assert duplicates[0]["from_title"] == "Bree"
    assert duplicates[0]["to_title"] == "Bree"
    assert duplicates[0]["to_page_id"] == "p1"


# ------------------------------------------------------------- what is refused


def test_a_page_with_an_unknown_kind_is_skipped_not_written():
    result = build_change_set(
        proposal(pages=[{"kind": "deity", "title": "Bahamut", "content": {"summary": "x"}}])
    )
    assert result["changes"] == []
    assert "kind" in result["skipped"][0]["reason"]


def test_a_page_with_no_title_is_skipped():
    result = build_change_set(proposal(pages=[{"kind": "character", "content": {"summary": "x"}}]))
    assert result["changes"] == []
    assert result["skipped"]


def test_an_empty_proposal_is_a_legitimate_answer():
    """Notes that are not about a page yield no change, not a failure."""
    result = build_change_set(proposal())
    assert result == {"changes": [], "relations": [], "skipped": []}


def test_a_garbage_answer_yields_no_change_rather_than_an_error():
    assert build_change_set(None)["changes"] == []
    assert build_change_set("nonsense")["changes"] == []
    assert build_change_set({"pages": "not a list"})["changes"] == []


def test_the_model_cannot_smuggle_prose_sections_the_wiki_does_not_have():
    result = build_change_set(
        proposal(
            pages=[
                {
                    "kind": "character",
                    "title": "Kaelor",
                    "content": {"summary": "ok", "secret_dm_notes": "nope", "image_uri": "evil/key"},
                }
            ]
        )
    )
    content = result["changes"][0]["after"]["content_json"]
    assert "secret_dm_notes" not in content
    assert "image_uri" not in content


def test_two_entries_for_one_page_are_folded_into_one_change():
    result = build_change_set(
        proposal(
            pages=[
                {"kind": "character", "title": "Kaelor", "content": {"summary": "Part one."}},
                {"kind": "character", "title": "kaelor", "content": {"facts": ["Part two."]}},
            ]
        )
    )
    assert len(result["changes"]) == 1
    content = result["changes"][0]["after"]["content_json"]
    assert content["summary"] == "Part one."
    assert content["facts"] == ["Part two."]


# ------------------------------------------------------------------ attributes


def test_unknown_attribute_keys_are_dropped():
    """The wiki rejects an unknown attribute, and a rejection fails the apply."""
    result = build_change_set(
        proposal(
            pages=[
                {
                    "kind": "character",
                    "title": "Kaelor",
                    "content": {"attributes": {"race": "Half-elf", "charecter_type": "npc"}},
                }
            ]
        )
    )
    attributes = result["changes"][0]["after"]["content_json"]["attributes"]
    assert attributes == {"race": "Half-elf"}


def test_an_out_of_enum_attribute_value_is_dropped_not_forwarded():
    result = build_change_set(
        proposal(
            pages=[
                {
                    "kind": "character",
                    "title": "Kaelor",
                    "content": {"attributes": {"character_type": "sidekick", "race": "Elf"}},
                }
            ]
        )
    )
    attributes = result["changes"][0]["after"]["content_json"]["attributes"]
    assert attributes == {"race": "Elf"}


def test_an_over_long_attribute_is_truncated_rather_than_dropped():
    result = build_change_set(
        proposal(
            pages=[
                {
                    "kind": "character",
                    "title": "Kaelor",
                    "content": {"attributes": {"race": "x" * 200}},
                }
            ]
        )
    )
    assert len(result["changes"][0]["after"]["content_json"]["attributes"]["race"]) == 64


def test_a_location_field_without_its_location_type_is_dropped():
    """'other' accepts only the common fields, so a stray population would 422."""
    result = build_change_set(
        proposal(
            pages=[
                {
                    "kind": "location",
                    "title": "Somewhere",
                    "content": {"attributes": {"population": "12,000", "region": "The North"}},
                }
            ]
        )
    )
    attributes = result["changes"][0]["after"]["content_json"]["attributes"]
    assert "population" not in attributes
    assert attributes == {"region": "The North"}


def test_a_settlement_keeps_the_fields_its_location_type_allows():
    result = build_change_set(
        proposal(
            pages=[
                {
                    "kind": "location",
                    "title": "Bree",
                    "content": {
                        "attributes": {
                            "location_type": "city",
                            "population": "12,000",
                            "districts": ["North Ward"],
                            "entrance": "a gate",  # a dungeon field: refused
                        }
                    },
                }
            ]
        )
    )
    attributes = result["changes"][0]["after"]["content_json"]["attributes"]
    assert attributes["population"] == "12,000"
    assert attributes["districts"] == ["North Ward"]
    assert "entrance" not in attributes


def test_a_page_with_no_usable_attributes_is_written_without_the_key():
    result = build_change_set(
        proposal(
            pages=[{"kind": "item", "title": "Ash Blade", "content": {"attributes": {"nope": 1}}}]
        )
    )
    assert "attributes" not in result["changes"][0]["after"]["content_json"]


# -------------------------------------------------------------------- timeline


def test_an_event_page_backs_a_timeline_entry():
    result = build_change_set(
        proposal(
            pages=[
                {
                    "kind": "event",
                    "title": "The Siege of Bree",
                    "content": {
                        "summary": "The town held.",
                        "attributes": {"event_type": "battle", "in_world_date": "17 Ches 1492"},
                    },
                }
            ]
        )
    )
    timeline = result["changes"][0]["timeline"]
    assert timeline == {"summary": "The town held.", "in_world_date": "17 Ches 1492"}


def test_a_non_event_page_does_not_back_a_timeline_entry():
    result = build_change_set(
        proposal(pages=[{"kind": "character", "title": "Kaelor", "content": {"summary": "x"}}])
    )
    assert result["changes"][0]["timeline"] is None


# -------------------------------------------------------------------- relations


def test_a_relation_between_two_proposed_pages_is_kept():
    result = build_change_set(
        proposal(
            pages=[
                {"kind": "character", "title": "Kaelor", "content": {"summary": "x"}},
                {"kind": "faction", "title": "Ash Company", "content": {"summary": "y"}},
            ],
            relations=[
                {"from_title": "Kaelor", "to_title": "Ash Company", "relation_type": "member_of"}
            ],
        )
    )
    assert result["relations"] == [
        {
            "id": "r1",
            "from_title": "Kaelor",
            "to_title": "Ash Company",
            "to_page_id": None,
            "relation_type": "member_of",
            "dropped": False,
        }
    ]


def test_a_relation_to_a_page_the_campaign_already_has_resolves_to_its_id():
    existing = [page("p9", "The North", kind="location")]
    result = build_change_set(
        proposal(
            pages=[{"kind": "character", "title": "Kaelor", "content": {"summary": "x"}}],
            relations=[{"from_title": "Kaelor", "to_title": "The North", "relation_type": "appears_in"}],
        ),
        existing_pages=existing,
    )
    assert result["relations"][0]["to_page_id"] == "p9"


def test_a_relation_whose_target_does_not_exist_is_dropped():
    """wiki-service would drop it silently; proposing it would be a lie."""
    result = build_change_set(
        proposal(
            pages=[{"kind": "character", "title": "Kaelor", "content": {"summary": "x"}}],
            relations=[{"from_title": "Kaelor", "to_title": "Nowhere", "relation_type": "member_of"}],
        )
    )
    assert result["relations"] == []


def test_a_relation_from_a_page_the_proposal_does_not_write_is_dropped():
    existing = [page("p1", "Kaelor", "character"), page("p2", "Bree", "location")]
    result = build_change_set(
        proposal(
            relations=[{"from_title": "Kaelor", "to_title": "Bree", "relation_type": "appears_in"}]
        ),
        existing_pages=existing,
    )
    assert result["relations"] == []


def test_a_self_relation_is_dropped():
    result = build_change_set(
        proposal(
            pages=[{"kind": "character", "title": "Kaelor", "content": {"summary": "x"}}],
            relations=[{"from_title": "Kaelor", "to_title": "Kaelor", "relation_type": "allied_with"}],
        )
    )
    assert result["relations"] == []


def test_an_unknown_relation_type_falls_back_to_a_neutral_one():
    result = build_change_set(
        proposal(
            pages=[
                {"kind": "character", "title": "Kaelor", "content": {"summary": "x"}},
                {"kind": "faction", "title": "Ash Company", "content": {"summary": "y"}},
            ],
            relations=[
                {"from_title": "Kaelor", "to_title": "Ash Company", "relation_type": "sworn_brothers_of"}
            ],
        )
    )
    assert result["relations"][0]["relation_type"] == "related_to"


# ----------------------------------------------------------------------- skipped


def test_the_models_own_skipped_entries_are_carried_through():
    result = build_change_set(
        proposal(skipped=[{"title": "the ambush idea", "reason": "not a page yet"}])
    )
    assert result["skipped"] == [{"title": "the ambush idea", "reason": "not a page yet"}]
