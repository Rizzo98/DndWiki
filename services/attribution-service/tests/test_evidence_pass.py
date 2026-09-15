"""Identity-evidence pass tests: the view, the schema and the tolerant parser."""

import json

from app.evidence import (
    EVIDENCE_SCHEMA,
    PROMPT_VERSION,
    EvidenceItem,
    EvidencePass,
    build_chunk_message,
    parse_evidence,
    render_view,
    roster_block,
    split_view,
    system_message,
)
from app.evidence_llm import chunk_lines, estimate_tokens


class FakeUtterance:
    def __init__(self, ref, start, text):
        self.ref = ref
        self.start = start
        self.text = text


def test_prompt_version_is_declared():
    assert PROMPT_VERSION.startswith("attr-ev-v")


def test_the_schema_is_small_and_enumerated():
    item = EVIDENCE_SCHEMA["properties"]["utterances"]["items"]
    assert "ref" in item["required"]
    assert set(item["properties"]["kind"]["enum"]) == {
        "action", "dialogue", "decision", "narration", "meta", "backchannel"
    }
    assert set(item["properties"]["voice_mode"]["enum"]) == {
        "pc_dialogue", "npc_dialogue", "narration", "ooc"
    }
    note_types = EVIDENCE_SCHEMA["properties"]["identity_notes"]["items"]["properties"]["type"]["enum"]
    assert set(note_types) == {"member_absent", "delegation", "voice_group_hint"}


def test_the_system_prompt_forbids_the_things_it_must():
    prompt = system_message()
    assert "Never assign, change or invent a speaker label" in prompt
    assert "NOT a person" in prompt
    assert "SPEAKER_00" in prompt  # named as a thing never to emit
    assert json.dumps(EVIDENCE_SCHEMA)[:40] in prompt


def test_the_roster_block_names_the_dm_and_the_abilities():
    lines = roster_block(
        [
            {"member_id": "m1", "player_name": "Alice", "character_name": "Aramil"},
            {"member_id": "m2", "player_name": "Dana", "role": "dm"},
        ],
        {"m1": ["class:wizard", "spell:fireball"]},
    )
    joined = "\n".join(lines)
    assert "Alice -> Aramil" in joined
    assert "Dungeon Master" in joined
    assert "spell:fireball" in joined


def test_the_view_carries_refs_voice_markers_and_known_names():
    view = render_view(
        [
            FakeUtterance("u_00001", 3661.0, "as you push it open"),
            FakeUtterance("u_00002", 3670.0, "I cast   fireball"),
        ],
        voice_of={"u_00001": "V1"},
        member_of={"u_00002": "Aramil"},
        roster=["Roster (player -> character):"],
    )
    assert "[u_00001 01:01:01 V1] as you push it open" in view
    assert "[u_00002 01:01:10 -] Aramil: I cast fireball" in view  # whitespace collapsed


def test_the_header_is_the_roster_and_the_rest_are_utterances():
    view = render_view(
        [FakeUtterance("u_00001", 1.0, "hello")],
        roster=["Roster (player -> character):", "  Alice -> Aramil"],
    )
    header, lines = split_view(view)
    assert header == "Roster (player -> character):\n  Alice -> Aramil"
    assert len(lines) == 1 and lines[0].startswith("[u_00001")


def test_every_chunk_carries_the_roster():
    """The roster is what 'claims' and 'addresses' are answered against.

    It used to be rendered into the view and then dropped on the way to the
    model (only lines starting with '[' survived), while the system prompt
    insisted no member outside the roster may ever be named - so the pass was
    asked to identify people it had never been shown, and a whole session
    produced 4 claims and 3 addresses.
    """
    header, _ = split_view(
        render_view(
            [FakeUtterance("u_00001", 1.0, "hello")],
            roster=["Roster (player -> character):", "  Alice -> Aramil"],
        )
    )
    message = build_chunk_message("view", window=0, total=3, header=header)
    assert "part 1/3" in message
    assert "Alice -> Aramil" in message


def test_the_chunk_message_asks_for_one_object_per_utterance():
    """Coverage, not a cap: an utterance the pass skips has no kind and no gist,
    so no question about it can ever be asked (272 of 419, silently)."""
    message = build_chunk_message("view", window=0, total=3, expected=12)
    assert "12 utterance(s), in the order given" in message
    assert "Every utterance gets an object" in message
    assert "At most" not in message
    assert "most identity evidence" not in message


# --- chunking ---------------------------------------------------------------


def test_a_chunk_never_asks_about_more_utterances_than_the_cap():
    """The item cap and the prompt have to agree, or the model drops rows."""
    lines = [f"[u_{i:05d} 00:00:01 V1] some words here" for i in range(250)]
    chunks = chunk_lines(lines, max_tokens=10_000, max_items=40)
    assert [len(chunk) for chunk in chunks] == [40] * 6 + [10]


def test_chunking_never_loses_or_duplicates_an_utterance():
    lines = [f"[u_{i:05d} 00:00:01 V1] {'x' * (i % 97)}" for i in range(500)]
    chunks = chunk_lines(lines, max_tokens=800, max_items=40)
    flat = [line for chunk in chunks for line in chunk]
    assert flat == lines
    assert all(len(chunk) <= 40 for chunk in chunks)
    assert all(
        sum(estimate_tokens(line) for line in chunk) <= 800 or len(chunk) == 1
        for chunk in chunks
    )


def test_the_token_bound_still_applies_when_it_is_the_tighter_one():
    # ~305 tokens a line: two fit in a 800-token chunk, three do not.
    lines = [f"[u_{i:05d} 00:00:01 V1] {'x' * 1200}" for i in range(6)]
    chunks = chunk_lines(lines, max_tokens=800, max_items=40)
    assert [len(chunk) for chunk in chunks] == [2, 2, 2]


def test_no_utterances_is_no_chunks():
    assert chunk_lines([], max_tokens=8000, max_items=40) == []


# --- parsing ----------------------------------------------------------------


def test_parse_evidence_reads_a_well_formed_response():
    parsed = parse_evidence(
        {
            "language": "it",
            "utterances": [
                {
                    "ref": "u_00001",
                    "kind": "action",
                    "voice_mode": "pc_dialogue",
                    "gist": "casts Fireball",
                    "capability_requirements": ["spell:fireball"],
                    "claims": [{"type": "self_character", "value": "Aramil", "strength": 0.8}],
                    "addresses": [{"name": "Thorin", "target": "next_turn"}],
                    "stakes": 0.9,
                }
            ],
            "identity_notes": [
                {"type": "member_absent", "member": "Keth", "from": "u_1", "to": "u_9"}
            ],
        }
    )
    assert parsed.language == "it"
    item = parsed.item("u_00001")
    assert item.kind == "action" and item.voice_mode == "pc_dialogue"
    assert item.capability_requirements == ("spell:fireball",)
    assert item.claims[0]["value"] == "Aramil"
    assert item.addresses[0]["target"] == "next_turn"
    assert item.stakes == 0.9
    assert parsed.notes[0].type == "member_absent"
    assert parsed.notes[0].member == "Keth"


def test_parse_evidence_drops_what_it_cannot_use():
    parsed = parse_evidence(
        {
            "utterances": [
                {"ref": "u_1", "kind": "nonsense", "voice_mode": "shouting", "stakes": 9.0},
                {"ref": ""},
                {"no_ref": True},
                "not a dict",
                {"ref": "u_2", "claims": [{"type": "self_character"}], "addresses": [{}]},
            ],
            "identity_notes": [{"type": "nonsense"}, "not a dict"],
        }
    )
    assert set(parsed.items) == {"u_1", "u_2"}
    assert parsed.item("u_1").kind is None  # an unknown enum is no opinion
    assert parsed.item("u_1").voice_mode is None
    assert parsed.item("u_1").stakes == 1.0  # clamped, not trusted
    assert parsed.item("u_2").claims == ()  # a claim with no value is not a claim
    assert parsed.notes == []


def test_parse_evidence_of_garbage_is_empty_not_fatal():
    assert parse_evidence(None).items == {}
    assert parse_evidence("nonsense").items == {}
    assert parse_evidence({}).items == {}


def test_a_missing_item_is_an_empty_item():
    pass_ = EvidencePass()
    item = pass_.item("u_99999")
    assert isinstance(item, EvidenceItem)
    assert item.kind is None and item.stakes == 0.5


def test_stakes_are_clamped_into_range():
    parsed = parse_evidence(
        {"utterances": [{"ref": "u_1", "stakes": -5}, {"ref": "u_2", "stakes": 2}]}
    )
    assert parsed.item("u_1").stakes == 0.0
    assert parsed.item("u_2").stakes == 1.0
