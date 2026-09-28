"""Tests for the change-set planner (the 'git status' of a session)."""

from conftest import CAMPAIGN_ID, PAGE_UUIDS, SESSION_ID, make_extraction

from app.planner import build_change_set


def _plan(existing_pages=None, extraction=None, party=None):
    return build_change_set(
        extraction or make_extraction(),
        CAMPAIGN_ID,
        SESSION_ID,
        existing_pages=existing_pages,
        party_characters=party or [],
    )


def test_plan_proposes_creates_with_payloads():
    plan = _plan()
    assert [c["action"] for c in plan["changes"]] == ["create", "create", "create"]
    assert [c["title"] for c in plan["changes"]] == ["Aragorn", "Moria", "Entering Moria"]
    assert [c["kind"] for c in plan["changes"]] == ["character", "location", "event"]
    # deterministic ids: the DM edits single entries and sends them back
    assert [c["id"] for c in plan["changes"]] == ["c1", "c2", "c3"]

    character = plan["changes"][0]
    assert character["page_id"] is None
    assert character["before"] is None
    assert character["dropped"] is False
    assert character["after"]["title"] == "Aragorn"
    assert character["after"]["content_json"]["facts"] == ["Speaks the password."]
    assert character["after"]["visibility"] == "public"

    # the world event carries the timeline entry it backs
    event = plan["changes"][2]
    assert event["timeline"] == {
        "summary": "The party passes the gate.",
        "in_world_date": None,
    }
    # non-event changes never carry one
    assert character["timeline"] is None


def test_plan_proposes_updates_for_documented_events():
    existing = [
        {
            "id": PAGE_UUIDS["Entering Moria"],
            "title": "Entering Moria",
            "slug": "entering-moria",
            "kind": "event",
            "status": "published",
            "aliases": [],
            "content_json": {"summary": "The party passes the gate."},
        }
    ]
    plan = _plan(existing_pages=existing)
    kinds = [(c["action"], c["title"]) for c in plan["changes"]]
    assert kinds == [
        ("create", "Aragorn"),
        ("create", "Moria"),
        ("update", "Entering Moria"),
    ]
    update = plan["changes"][2]
    assert update["page_id"] == PAGE_UUIDS["Entering Moria"]
    # the DM sees the CURRENT page next to the proposed one (the diff)
    assert update["before"]["content_json"] == {"summary": "The party passes the gate."}
    assert update["after"]["content_json"]["attributes"]["participants"] == ["Aragorn"]


def test_plan_updates_a_page_the_campaign_documents():
    """An entity the campaign already has is not re-created and not skipped:
    the session is proposed as an UPDATE of its page, carrying the page's
    current content so the DM reviews a diff and not a new page."""
    existing = [
        {
            "id": PAGE_UUIDS["Moria"],
            "title": "Moria",
            "slug": "moria",
            "kind": "location",
            "status": "published",
            "aliases": ["Mines of Moria"],
            "content_json": {"summary": "An ancient dwarven mine."},
        }
    ]
    plan = _plan(existing_pages=existing)
    assert [(c["action"], c["title"]) for c in plan["changes"]] == [
        ("create", "Aragorn"),
        ("update", "Moria"),
        ("create", "Entering Moria"),
    ]
    update = plan["changes"][1]
    # the PAGE's own kind, not 'event': it decides how the review renders it
    assert update["kind"] == "location"
    assert update["page_id"] == PAGE_UUIDS["Moria"]
    assert update["timeline"] is None  # only event pages back a timeline entry
    # the DM diffs against what the page says today
    assert update["before"] == {
        "title": "Moria",
        "content_json": {"summary": "An ancient dwarven mine."},
    }
    content = update["after"]["content_json"]
    assert content["summary"] == "An ancient dwarven mine."  # kept, not replaced
    assert content["session_references"] == [
        {"session_id": SESSION_ID, "facts": ["Its west-door opened to the password."]}
    ]
    assert plan["skipped"] == []


def test_plan_reports_a_match_that_adds_nothing_as_context():
    """A page that already says everything the session adds is context, not a
    change: the DM is not asked to confirm a page rewritten with its own
    content."""
    existing = [
        {
            "id": PAGE_UUIDS["Moria"],
            "title": "Moria",
            "slug": "moria",
            "kind": "location",
            "status": "published",
            "aliases": ["Mines of Moria"],
            "content_json": {
                "summary": "An ancient dwarven mine.",
                "language": "en",
                "attributes": {"location_type": "dungeon"},
                "session_references": [
                    {"session_id": SESSION_ID, "facts": ["Its west-door opened to the password."]}
                ],
            },
        }
    ]
    plan = _plan(existing_pages=existing)
    assert [c["title"] for c in plan["changes"]] == ["Aragorn", "Entering Moria"]
    assert plan["skipped"] == [
        {
            "title": "Moria",
            "kind": "location",
            "matched_title": "Moria",
            "reason": "already documented by this campaign, and adds nothing new",
        }
    ]


def test_plan_updates_the_page_a_misspelled_name_belongs_to():
    """The pipeline hears the same place spelled differently: that is the SAME
    page, so the plan proposes updating it (under its own title) instead of
    creating a second page with a 'possible_duplicate' link."""
    existing = [
        {
            "id": PAGE_UUIDS["Moria"],
            "title": "Concaverde",
            "slug": "concaverde",
            "kind": "location",
            "status": "published",
            "aliases": [],
            "content_json": {
                "summary": "A region of swamps.",
                "attributes": {"location_type": "region", "terrain": "paludi"},
            },
        }
    ]
    extraction = make_extraction()
    extraction["locations"] = [
        {
            "name": "Coca Verde",  # how the table actually said it
            "aliases": [],
            "description": "Where the party got lost.",
            "facts": [],
            "session_facts": [],
            "mentions": 1,
        }
    ]
    plan = _plan(existing_pages=existing, extraction=extraction)
    updates = [c for c in plan["changes"] if c["action"] == "update"]
    assert [(c["title"], c["kind"]) for c in updates] == [("Concaverde", "location")]
    assert updates[0]["page_id"] == PAGE_UUIDS["Moria"]
    assert "Coca Verde" not in [c["title"] for c in plan["changes"]]
    # the mis-heard name becomes an alias of the page it belongs to, and the
    # page's own words stay where they are
    content = updates[0]["after"]["content_json"]
    assert content["aliases"] == ["Coca Verde"]
    assert content["summary"] == "A region of swamps.\n\nWhere the party got lost."
    assert updates[0]["before"]["content_json"]["summary"] == "A region of swamps."
    assert plan["relations"] == []  # no 'possible_duplicate' link to itself


def test_plan_collects_relations_and_marks_party_characters():
    extraction = make_extraction()
    extraction["characters"][0]["relationships"] = {"member_of": ["Fellowship"]}
    plan = _plan(
        existing_pages=[
            {
                "id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
                "title": "Fellowship",
                "slug": "fellowship",
                "kind": "faction",
                "status": "published",
                "aliases": [],
            }
        ],
        extraction=extraction,
        party=["Aragorn"],
    )
    assert plan["relations"] == [
        {
            "id": "r1",
            "from_title": "Aragorn",
            "to_title": "Fellowship",
            "to_page_id": None,
            "relation_type": "member_of",
            "dropped": False,
        }
    ]
    character = plan["changes"][0]
    assert character["after"]["content_json"]["attributes"]["character_type"] == "player"
