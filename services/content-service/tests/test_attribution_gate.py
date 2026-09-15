"""The attribution gate: the safety property, asserted rather than hoped for.

docs/attribution-model.md S17 lists "wiki facts whose source_refs are
unresolved" with a target of ZERO and calls it a hard gate, assertable in a
test. This is that test.
"""

import pytest

from app.attribution import (
    apply_gate,
    artifact_statuses,
    confident_refs,
    gate_actor,
    gate_fact,
    gate_participants,
    is_raw_label,
    resolve_refs,
    strip_raw_labels_from_drafts,
)
from app.chunking import build_view_lines_from_artifact, chunk_artifact
from app.merger import is_generic_name


def artifact(statuses):
    return {
        "session_id": "s",
        "roster": [
            {"member_id": "m1", "player_name": "Alice", "character_name": "Aramil", "role": "player"},
            {"member_id": "m2", "player_name": "Bob", "character_name": "Thorin", "role": "player"},
            {"member_id": "m3", "player_name": "Dana", "character_name": None, "role": "dm"},
        ],
        "utterances": [
            {
                "id": ref,
                "start": 10.0 * i,
                "end": 10.0 * i + 4.0,
                "text": f"line {ref}",
                "status": status,
                "speaker": (
                    {"member_id": "m1", "character_name": "Aramil", "label": "Alice — Aramil"}
                    if status != "unresolved"
                    else None
                ),
            }
            for i, (ref, status) in enumerate(statuses.items())
        ],
    }


CONFIDENT = artifact(
    {
        "u_00001": "auto_high",
        "u_00002": "user_confirmed",
        "u_00003": "auto_low",
        "u_00004": "unresolved",
    }
)


# --- the raw-label guard ----------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["SPEAKER_00", "speaker_3", "Speaker A", "V3", "VOICE_2", "SPEAKER-01"],
)
def test_raw_labels_are_recognised(name):
    assert is_raw_label(name) is True


@pytest.mark.parametrize("name", ["Aramil", "Thorin Oakenshield", "Captain Marta", "Vex'a"])
def test_real_names_are_not_labels(name):
    assert is_raw_label(name) is False


def test_a_raw_label_is_never_a_valid_entity_name():
    """is_generic_name('SPEAKER_00', 'character') used to be False, so the model
    could and did emit such entities and the merger drafted pages for them."""
    assert is_generic_name("SPEAKER_00", "character") is True
    assert is_generic_name("SPEAKER_00", "location") is True
    assert is_generic_name("Aramil", "character") is False


def test_drafts_carrying_a_raw_label_are_dropped_at_the_boundary():
    drafts = [
        {"title": "SPEAKER_00", "content_json": {}},
        {"title": "Aramil", "content_json": {"aliases": ["SPEAKER_01", "Aramil the Bold"]}},
        {"title": "Thorin", "content_json": {"aliases": ["V2"]}},
    ]
    kept = strip_raw_labels_from_drafts(drafts)
    assert [d["title"] for d in kept] == ["Aramil", "Thorin"]
    assert kept[0]["content_json"]["aliases"] == ["Aramil the Bold"]
    assert "aliases" not in kept[1]["content_json"]


# --- the status gate --------------------------------------------------------


def test_only_three_statuses_may_back_a_character_fact():
    refs = confident_refs(CONFIDENT)
    assert refs == {"u_00001", "u_00002"}


def test_resolve_refs_splits_confident_from_unresolved():
    statuses = artifact_statuses(CONFIDENT)
    confident, unresolved = resolve_refs(
        ["u_00001", "u_00003", "u_00004", "u_99999"], statuses
    )
    assert confident == ["u_00001"]
    assert set(unresolved) == {"u_00003", "u_00004", "u_99999"}


def test_a_fact_must_rest_entirely_on_confident_lines():
    statuses = artifact_statuses(CONFIDENT)
    assert gate_fact("Aramil cast Fireball", ["u_00001"], statuses) is True
    # one uncertain line is enough to disqualify the fact: the claim is only as
    # good as its weakest source
    assert gate_fact("Aramil cast Fireball", ["u_00001", "u_00003"], statuses) is False
    assert gate_fact("Aramil cast Fireball", ["u_00004"], statuses) is False


def test_a_fact_with_no_reference_at_all_is_unverifiable():
    statuses = artifact_statuses(CONFIDENT)
    assert gate_fact("Aramil cast Fireball", [], statuses) is False


def test_without_an_artifact_the_gate_is_a_pass_through():
    """ATTRIBUTION_ENABLED=false must behave exactly as before: a real kill
    switch, not a half-on state."""
    assert gate_fact("anything", [], {}) is True
    assert gate_actor("Aramil", [], {}) == "Aramil"
    assert gate_participants(["Aramil"], [], {}) == ["Aramil"]


def test_even_without_an_artifact_a_raw_label_is_never_an_actor():
    assert gate_actor("SPEAKER_00", [], {}) == ""


# --- events -----------------------------------------------------------------


def test_an_event_actor_the_transcript_does_not_support_is_left_empty():
    statuses = artifact_statuses(CONFIDENT)
    assert gate_actor("Aramil", ["u_00001"], statuses) == "Aramil"
    assert gate_actor("Aramil", ["u_00004"], statuses) == ""


def test_unresolved_participants_are_dropped_from_the_list():
    """Keeping a guessed name on an event page is how a summary becomes fiction."""
    statuses = artifact_statuses(CONFIDENT)
    assert gate_participants(["Aramil", "Thorin"], ["u_00001"], statuses) == [
        "Aramil",
        "Thorin",
    ]
    assert gate_participants(["Aramil", "Thorin"], ["u_00004"], statuses) == []
    assert gate_participants(["SPEAKER_00", "Aramil"], ["u_00001"], statuses) == ["Aramil"]


# --- the whole extraction ---------------------------------------------------


def merged_extraction():
    return {
        "language": "en",
        "session_summary": "The party opened the eastern door.",
        "characters": [
            {
                "name": "Aramil",
                "facts": ["Cast Fireball on the goblins", "Hates the sea"],
                "source_refs": ["u_00001"],
            },
            {
                "name": "SPEAKER_03",
                "facts": ["Said something nobody could place"],
                "source_refs": ["u_00004"],
            },
            {
                "name": "Bob",
                "facts": ["Rolled a natural 20"],
                "source_refs": ["u_00001"],
            },
            {
                "name": "Ghost",
                "facts": ["Walked through a wall"],
                "source_refs": ["u_00004"],
            },
        ],
        "locations": [
            {"name": "Fatumastra", "description": "A port city", "source_refs": ["u_00001"]}
        ],
        "events": [
            {
                "title": "The Eastern Door",
                "description": "The party forced it open.",
                "actor": "Aramil",
                "participants": ["Aramil", "Thorin"],
                "source_refs": ["u_00001"],
            },
            {
                "title": "The Silent Hour",
                "description": "Nobody could place the voice.",
                "actor": "Thorin",
                "participants": ["Thorin"],
                "source_refs": ["u_00004"],
            },
        ],
        "timeline_entries": [
            {"time": "00:10", "summary": "the door", "characters": ["Aramil"], "source_refs": ["u_00001"]},
            {"time": "00:40", "summary": "the voice", "characters": ["Thorin"], "source_refs": ["u_00004"]},
        ],
    }


def test_the_gate_keeps_what_the_attribution_supports():
    gated, report = apply_gate(merged_extraction(), CONFIDENT)
    assert report["enabled"] is True
    names = [c["name"] for c in gated["characters"]]
    assert "Aramil" in names
    assert "SPEAKER_03" not in names  # a raw label is never a character
    assert "Bob" not in names  # a PLAYER's real name is never a character page
    assert "Ghost" not in names  # nothing verifiable survived
    assert set(report["raw_label_entities"]) == {"SPEAKER_03"}
    assert set(report["player_named_characters"]) == {"Bob"}


def test_the_gate_clears_an_unsupported_actor_and_its_participants():
    gated, report = apply_gate(merged_extraction(), CONFIDENT)
    by_title = {event["title"]: event for event in gated["events"]}
    assert by_title["The Eastern Door"]["actor"] == "Aramil"
    assert by_title["The Eastern Door"]["participants"] == ["Aramil", "Thorin"]
    assert by_title["The Silent Hour"]["actor"] == ""
    assert by_title["The Silent Hour"]["participants"] == []
    assert report["cleared_actors"] == 1


def test_the_gate_drops_timeline_characters_it_cannot_support():
    gated, _ = apply_gate(merged_extraction(), CONFIDENT)
    entries = gated["timeline_entries"]
    assert entries[0]["characters"] == ["Aramil"]
    assert entries[1]["characters"] == []


def test_the_gate_report_is_auditable():
    _, report = apply_gate(merged_extraction(), CONFIDENT)
    assert set(report) == {
        "enabled",
        "dropped_characters",
        "dropped_facts",
        "dropped_participants",
        "cleared_actors",
        "raw_label_entities",
        "player_named_characters",
    }


def test_the_gate_is_a_pass_through_without_an_artifact():
    merged, report = apply_gate(merged_extraction(), None)
    assert report["enabled"] is False
    # the raw label is still dropped: that guard does not depend on attribution
    assert "SPEAKER_03" not in [c["name"] for c in merged["characters"]]
    assert "Aramil" in [c["name"] for c in merged["characters"]]


def test_the_summary_survives_at_party_level():
    """Unresolved content may still be described, at party level: the summary is
    the one place where "the party forced the eastern door" is the right answer."""
    gated, _ = apply_gate(merged_extraction(), CONFIDENT)
    assert gated["session_summary"] == "The party opened the eastern door."


# --- the view ---------------------------------------------------------------


def test_the_view_marks_certainty_in_the_text_itself():
    lines = build_view_lines_from_artifact(CONFIDENT)
    assert lines[0].startswith("[u_00001 00:00:00] Aramil: ")
    assert "Aramil?" in lines[2]  # auto_low
    assert "(unattributed)" in lines[3]
    assert all("SPEAKER" not in line for line in lines)


def test_the_view_carries_the_reference_the_extraction_must_echo():
    lines = build_view_lines_from_artifact(CONFIDENT)
    for line in lines:
        assert line.startswith("[u_")


def test_chunking_the_artifact_preserves_every_line():
    chunks = chunk_artifact(CONFIDENT, max_tokens=1000, overlap=0.0)
    flat = [line for chunk in chunks for line in chunk]
    assert len(flat) == len(CONFIDENT["utterances"])
