"""The summary revision patch: merge rules and refusal rules.

Phase 1b used to ask the model for the COMPLETE corrected extraction. On a real
session that answer is ~8k output tokens against a 4096 completion cap, so the
provider cut it off mid-JSON; json_repair closed the truncated tail into a
syntactically valid partial extraction, the old worker guard restored the
categories the cut had removed from the PREVIOUS revision, and the DM's
correction ended up in the summary lines while the events and the timeline
entries kept the wrong text (this is the bug these tests pin down).

The revision is a patch now: the summary in full plus the items the correction
touches, addressed by the id every item was given. Everything else keeps the
value the DM reviewed, and a patch that does not fit is an error - never a
half-applied revision, never a silently dropped correction.
"""

from __future__ import annotations

import copy

import pytest
from conftest import make_extraction

from app.revision import (
    SummaryRevisionError,
    apply_summary_revision,
    describe_revision,
    indexed_for_prompt,
    item_id,
)


def test_the_prompt_view_carries_an_id_per_item():
    current = make_extraction()
    indexed = indexed_for_prompt(current)
    assert indexed["characters"][0]["id"] == "c0"
    assert indexed["locations"][0]["id"] == "l0"
    assert indexed["events"][0]["id"] == "e0"
    assert indexed["timeline_entries"][0]["id"] == "t0"
    # the item keeps every field it had, and the extraction itself is untouched
    assert indexed["characters"][0]["name"] == "Aragorn"
    assert "id" not in current["characters"][0]
    assert "id" not in current["characters"][0]
    assert indexed["language"] == "en"
    assert item_id("characters", 3) == "c3"


def test_the_prompt_view_shows_the_narrative_as_blocks():
    """One version of the story in front of the model: the blocks it has to
    answer with, not the joined text plus the blocks."""
    current = make_extraction()
    current["summary_blocks"] = [{"location": "Moria", "text": "The party arrives."}]
    indexed = indexed_for_prompt(current)
    assert indexed["session_summary"] == [{"location": "Moria", "text": "The party arrives."}]
    assert "summary_blocks" not in indexed
    # a row without blocks (an old summary) is shown as the story it is
    legacy = indexed_for_prompt(make_extraction())
    assert legacy["session_summary"] == [
        {"location": "", "text": "The party reaches the gates of Moria."}
    ]


def test_a_correction_reaches_the_events_and_the_timeline():
    """The reported bug: the summary was corrected and the event was not."""
    current = make_extraction()
    merged = apply_summary_revision(
        current,
        {
            "session_summary": "Boromir was going to the city center.",
            "updates": {
                "characters": {"c0": {"name": "Boromir"}},
                "events": {
                    "e0": {
                        "description": "Boromir passes the gate.",
                        "participants": ["Boromir"],
                    }
                },
                "timeline_entries": {"t0": {"summary": "Boromir opens the gate."}},
            },
        },
    )
    assert merged["session_summary"] == "Boromir was going to the city center."
    assert merged["characters"][0]["name"] == "Boromir"
    assert merged["events"][0]["description"] == "Boromir passes the gate."
    assert merged["events"][0]["participants"] == ["Boromir"]
    assert merged["timeline_entries"][0]["summary"] == "Boromir opens the gate."


def test_everything_the_patch_does_not_mention_keeps_its_value():
    """Nothing may be lost or reset by a revision: the fields the patch does
    not name - confidences and merger bookkeeping included - survive."""
    current = make_extraction()
    current["characters"][0]["confidence"] = 0.75
    current["characters"][0]["mentions"] = 7
    current["events"][0]["confidence"] = 0.5
    merged = apply_summary_revision(
        current,
        {
            "session_summary": "Rewritten.",
            "updates": {"events": {"e0": {"description": "New description."}}},
        },
    )
    assert merged["events"][0]["confidence"] == 0.5
    assert merged["events"][0]["title"] == "Entering Moria"
    assert merged["events"][0]["participants"] == ["Aragorn"]
    assert merged["characters"][0]["confidence"] == 0.75
    assert merged["characters"][0]["mentions"] == 7
    assert merged["characters"][0]["description"] == "A ranger of the north."
    assert merged["locations"][0]["name"] == "Moria"
    assert merged["timeline_entries"][0]["time"] == "00:00:12"
    # the categories the patch did not touch are the same objects, not [] —
    # the old "restore an empty category from the previous revision" guard is
    # exactly what published stale events under a corrected summary
    assert merged["locations"] == current["locations"]
    assert merged["language"] == "en"


def test_the_merge_never_mutates_the_extraction_it_came_from():
    current = make_extraction()
    before = copy.deepcopy(current)
    apply_summary_revision(
        current,
        {
            "session_summary": "Rewritten.",
            "updates": {"characters": {"c0": {"description": "Rewritten too."}}},
            "removals": {"events": ["e0"]},
        },
    )
    assert current == before


def test_an_item_may_be_addressed_by_its_name_title_or_time():
    """A model that writes "Entering Moria" instead of "e0" still means the
    same item - and the answer stays applicable."""
    current = make_extraction()
    merged = apply_summary_revision(
        current,
        {
            "session_summary": "Rewritten.",
            "updates": {
                "events": {"entering moria": {"description": "Fixed."}},
                "characters": {"Aragorn": {"name": "Strider"}},
                "timeline_entries": {"00:00:12": {"summary": "Fixed too."}},
            },
        },
    )
    assert merged["events"][0]["description"] == "Fixed."
    assert merged["characters"][0]["name"] == "Strider"
    assert merged["timeline_entries"][0]["summary"] == "Fixed too."


def test_additions_and_removals():
    current = make_extraction()
    merged = apply_summary_revision(
        current,
        {
            "session_summary": "Rewritten.",
            "additions": {
                "characters": [{"name": "Gimli", "description": "A dwarf."}],
                "events": [
                    {"title": "The Bridge", "description": "It breaks."}
                ],
            },
            "removals": {"timeline_entries": ["t0"], "locations": ["l0"]},
        },
    )
    assert [c["name"] for c in merged["characters"]] == ["Aragorn", "Gimli"]
    assert [e["title"] for e in merged["events"]] == ["Entering Moria", "The Bridge"]
    assert merged["locations"] == []
    assert merged["timeline_entries"] == []


def test_an_added_item_must_carry_its_identity():
    current = make_extraction()
    with pytest.raises(SummaryRevisionError, match="carries no name"):
        apply_summary_revision(
            current,
            {"session_summary": "x", "additions": {"characters": [{"description": "?"}]}},
        )


def test_a_patch_without_a_summary_is_refused():
    """Every revision returns the complete summary: a patch without one would
    silently keep whatever the DM is looking at."""
    with pytest.raises(SummaryRevisionError, match="no 'session_summary'"):
        apply_summary_revision(make_extraction(), {"updates": {}})
    with pytest.raises(SummaryRevisionError, match="no 'session_summary'"):
        apply_summary_revision(make_extraction(), {"session_summary": "   "})


def test_the_old_full_extraction_echo_is_refused():
    """The expensive answer of the old contract is not a patch: accepting it
    key by key is how a correction disappeared without a trace."""
    with pytest.raises(SummaryRevisionError, match="unexpected keys"):
        apply_summary_revision(make_extraction(), make_extraction())


def test_a_patch_pointing_at_a_missing_item_is_refused():
    current = make_extraction()
    with pytest.raises(SummaryRevisionError, match="e4"):
        apply_summary_revision(
            current,
            {"session_summary": "x", "updates": {"events": {"e4": {"description": "?"}}}},
        )
    with pytest.raises(SummaryRevisionError, match="does not have"):
        apply_summary_revision(
            current,
            {"session_summary": "x", "removals": {"characters": ["Nobody"]}},
        )


def test_a_patch_with_an_unknown_category_is_refused():
    with pytest.raises(SummaryRevisionError, match="categories this session does not have"):
        apply_summary_revision(
            make_extraction(),
            {"session_summary": "x", "updates": {"npcs": {"n0": {"description": "?"}}}},
        )


def test_a_field_block_that_is_not_an_object_is_refused():
    with pytest.raises(SummaryRevisionError, match="not an object of fields"):
        apply_summary_revision(
            make_extraction(),
            {"session_summary": "x", "updates": {"events": {"e0": "rewritten!"}}},
        )


def test_an_empty_update_is_refused():
    with pytest.raises(SummaryRevisionError, match="changes no field"):
        apply_summary_revision(
            make_extraction(),
            {"session_summary": "x", "updates": {"events": {"e0": {}}}},
        )


def test_an_echoed_non_patch_key_is_tolerated():
    """The model may repeat the language or a confidence from what it was
    shown: those are not part of the patch and change nothing."""
    current = make_extraction()
    merged = apply_summary_revision(
        current,
        {
            "language": "en",
            "confidence": 0.4,
            "session_summary": "Rewritten.",
        },
    )
    assert merged["session_summary"] == "Rewritten."
    assert merged["language"] == "en"


def test_the_patch_stores_the_narrative_as_text_and_as_blocks():
    """The DM reviews text and the page labels blocks: both come out of the
    same answer, so they can never disagree."""
    merged = apply_summary_revision(
        make_extraction(),
        {
            "session_summary": [
                {"location": "Locanda del Fumo Aspro", "text": "La sessione si apre."},
                {"location": "", "text": "Poi il gruppo esce."},
            ]
        },
    )
    assert merged["summary_blocks"] == [
        {"location": "Locanda del Fumo Aspro", "text": "La sessione si apre."},
        {"location": "", "text": "Poi il gruppo esce."},
    ]
    assert merged["session_summary"] == "La sessione si apre.\n\nPoi il gruppo esce."


def test_the_patch_accepts_a_narrative_that_is_plain_prose():
    """A model that answers with prose still gives the DM a story: the
    paragraphs become blocks with no place label."""
    merged = apply_summary_revision(
        make_extraction(), {"session_summary": "- one\n\n  two  \nthree"}
    )
    assert merged["summary_blocks"] == [
        {"location": "", "text": "one"},
        {"location": "", "text": "two three"},
    ]


def test_describe_revision_names_what_moved():
    """The worker logs this: a revision that changed nothing must be visible
    instead of looking like a successful correction."""
    current = make_extraction()
    same = apply_summary_revision(
        current, {"session_summary": current["session_summary"]}
    )
    assert describe_revision(current, same) == "nothing changed"

    changed = apply_summary_revision(
        current,
        {
            "session_summary": "Rewritten.",
            "updates": {"events": {"e0": {"description": "Fixed."}}},
            "removals": {"timeline_entries": ["t0"]},
        },
    )
    described = describe_revision(current, changed)
    assert "summary rewritten" in described
    assert "events changed: Entering Moria" in described
    assert "timeline_entries removed: 00:00:12" in described
    assert "characters" not in described


def test_describe_revision_reports_a_label_change():
    """A correction that only moves a scene to another place still has to be
    visible in the log: the narrative text may not change at all."""
    current = make_extraction()
    current["summary_blocks"] = [{"location": "Fatumastra", "text": current["session_summary"]}]
    relabelled = apply_summary_revision(
        current,
        {"session_summary": [{"location": "Ospedale di Fatumastra", "text": current["session_summary"]}]},
    )
    assert describe_revision(current, relabelled) == "summary labels changed"
