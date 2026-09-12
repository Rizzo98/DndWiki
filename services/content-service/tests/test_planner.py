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


def test_plan_skips_entities_the_campaign_documents():
    existing = [
        {
            "id": PAGE_UUIDS["Moria"],
            "title": "Moria",
            "slug": "moria",
            "kind": "location",
            "status": "published",
            "aliases": ["Mines of Moria"],
        }
    ]
    plan = _plan(existing_pages=existing)
    assert "Moria" not in [c["title"] for c in plan["changes"]]
    # reported as context, never as a change
    assert plan["skipped"] == [
        {
            "title": "Moria",
            "kind": "location",
            "matched_title": "Moria",
            "reason": "already documented by this campaign",
        }
    ]


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
