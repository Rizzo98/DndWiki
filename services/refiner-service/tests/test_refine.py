"""Pure refinement logic tests (no litellm): windows, parsing, splitting, apply."""

import pytest
from dnd_common.transcript import Turn, speaker_turns

from app.prompts import (
    PROMPT_VERSION,
    REFINE_SCHEMA,
    TEXT_ONLY_SCHEMA,
    TEXT_ONLY_SYSTEM_PROMPT,
    build_schema,
    build_window_message,
    system_prompt,
)
from app.refine import (
    RefineError,
    apply_refined,
    build_cast_block,
    build_context_blocks,
    canonicalize,
    keep_labels,
    parse_decisions,
    rewrite_transcript,
    split_text,
    turn_view,
    turn_windows,
)


def seg(start, end, speaker="SPEAKER_00", chunk=0, text="hello world"):
    return {"start": start, "end": end, "speaker": speaker, "chunk": chunk, "text": text}


# --- turn_windows ------------------------------------------------------------


def test_windows_single_when_short():
    assert turn_windows(50, 100, 15) == [(0, 50, 0)]


def test_windows_slide_with_overlap():
    windows = turn_windows(220, 100, 15)
    assert windows[0] == (0, 100, 0)
    assert windows[1] == (85, 185, 15)
    assert windows[2] == (170, 220, 15)  # last window clamps to the end


def test_windows_zero_turns():
    assert turn_windows(0, 100, 15) == []


def test_windows_misconfigured_overlap_terminates():
    # overlap >= window: step clamps to 1 so the loop still terminates and
    # covers every turn index at least once.
    windows = turn_windows(10, 5, 10)
    assert windows[0] == (0, 5, 0)
    assert windows[-1][1] == 10
    covered = {i for start, end, _ in windows for i in range(start, end)}
    assert covered == set(range(10))


# --- split_text --------------------------------------------------------------


def test_split_text_proportional_and_conserves_words():
    pieces = split_text("a b c d e f g h i j", [5, 5])
    assert pieces == ["a b c d e", "f g h i j"]


def test_split_text_single_chunk():
    assert split_text("hello world", [10]) == ["hello world"]


def test_split_text_empty():
    assert split_text("", [2, 3]) == ["", ""]


def test_split_text_fewer_words_than_chunks():
    pieces = split_text("a b", [1, 1, 1])
    assert pieces.count("") >= 1
    assert " ".join(x for x in pieces if x) == "a b"


def test_split_text_skewed_weights():
    pieces = split_text("one two three four five six", [1, 9])
    assert " ".join(pieces).split() == ["one", "two", "three", "four", "five", "six"]


# --- parse_decisions ---------------------------------------------------------


def test_parse_decisions_ok():
    raw = {
        "turns": [
            {"index": 0, "speaker": "SPEAKER_01", "text": "Hi"},
            {"index": 2, "speaker": "Speaker B", "text": "Bye"},
        ]
    }
    assert parse_decisions(raw) == {
        0: ("SPEAKER_01", "Hi"),
        2: ("Speaker B", "Bye"),
    }


def test_parse_decisions_rejects_non_object():
    with pytest.raises(RefineError):
        parse_decisions([1, 2])
    with pytest.raises(RefineError):
        parse_decisions({"turns": "nope"})
    with pytest.raises(RefineError):
        parse_decisions(None)


def test_parse_decisions_skips_garbage_items():
    raw = {
        "turns": [
            {"index": "x", "speaker": "A", "text": "t"},
            {"index": 1, "speaker": "B", "text": "u"},
            "junk",
            None,
            {"index": 1, "speaker": "C", "text": "v"},  # duplicate index: last wins
        ]
    }
    assert parse_decisions(raw) == {1: ("C", "v")}


# --- canonicalize ------------------------------------------------------------


def turn(label, start=0.0, end=1.0, chunk=0, indices=None):
    return Turn(label=label, start=start, end=end, chunk=chunk, indices=indices or [0])


def test_canonicalize_keeps_wellformed_and_first_seen():
    turns = [turn("A"), turn("B", 1.0, 2.0), turn("A", 2.0, 3.0)]
    decisions = {
        0: ("SPEAKER_02", "x"),
        1: ("B", "y"),
        2: ("SPEAKER_02", "z"),
    }
    out = canonicalize(turns, decisions)
    assert out[0] == ("SPEAKER_02", "x")  # well-formed label passes through
    assert out[1] == ("SPEAKER_00", "y")  # non-canonical string -> first-seen
    assert out[2] == ("SPEAKER_02", "z")  # same string -> same canonical label


def test_canonicalize_falls_back_to_raw_label():
    turns = [turn("Speaker A")]
    out = canonicalize(turns, {})
    assert out[0][0] == "SPEAKER_00"  # raw label canonicalized, original text kept
    assert out[0][1] == ""


def test_canonicalize_normalizes_zero_padding():
    turns = [turn("A"), turn("B", 1.0, 2.0)]
    out = canonicalize(turns, {0: ("SPEAKER_7", "x")})
    assert out[0] == ("SPEAKER_07", "x")


# --- build_context_blocks ----------------------------------------------------


def test_context_blocks_first_appearance_order():
    segments = [seg(0, 1, "A", 0, "first words"), seg(1, 2, "B", 0, "second words")]
    turns = speaker_turns(segments)
    decisions = {1: ("SPEAKER_01", "second words"), 0: ("SPEAKER_00", "first words")}
    blocks = build_context_blocks(turns, segments, decisions)
    assert blocks == ['SPEAKER_00: "first words"', 'SPEAKER_01: "second words"']


# --- build_cast_block ----------------------------------------------------------


def test_build_cast_block_formats_members():
    members = [
        {
            "role": "dm",
            "player_name": "Gandalf",
            "character_name": "Dungeon Master",
            "character_description": "The narrator and keeper of the world",
        },
        {
            "role": "player",
            "player_name": "Alice",
            "character_name": "Rowan",
            "character_description": "Tall half-elf rogue with a silver braid",
        },
        {
            "role": "player",
            "player_name": "Bob",
            "character_name": "Cedric",
            "character_description": "Stocky dwarf cleric with a braided beard",
        },
    ]
    lines = build_cast_block(members)
    assert lines[0].startswith("- Dungeon Master (Gandalf) (dm, narrator) — The narrator")
    assert "Rowan (Alice) — Tall half-elf rogue with a silver braid" in lines[1]
    assert "Cedric (Bob) — Stocky dwarf cleric with a braided beard" in lines[2]


def test_build_cast_block_skips_members_without_character():
    members = [
        {"role": "player", "player_name": "Bob", "character_name": "", "character_description": "x"},
        {"role": "player", "player_name": "Carol", "character_name": "   ", "character_description": "y"},
        {"role": "player", "player_name": "Alice", "character_name": "Rowan", "character_description": "z"},
    ]
    assert build_cast_block(members) == ["- Rowan (Alice) — z"]


def test_build_cast_block_missing_description_keeps_names():
    members = [
        {"role": "player", "player_name": "Alice", "character_name": "Rowan", "character_description": None},
    ]
    assert build_cast_block(members) == ["- Rowan (Alice)"]


def test_build_cast_block_empty_and_long_description():
    assert build_cast_block([]) == []
    long = "word " * 300
    lines = build_cast_block(
        [{"role": "player", "player_name": "A", "character_name": "B", "character_description": long}]
    )
    assert len(lines) == 1
    assert len(lines[0]) < len(long) + 40  # truncated


# --- turn_view ---------------------------------------------------------------


def test_turn_view_joins_text_and_keeps_metadata():
    segments = [seg(0, 1, "A", 0, "hi"), seg(1, 2, "A", 0, "there")]
    turns = speaker_turns(segments)
    view = turn_view(turns, segments, 0)
    assert view["index"] == 0
    assert view["text"] == "hi there"
    assert view["start"] == 0.0
    assert view["chunk"] == 0
    assert view["speaker"] == "A"
    assert "confidence" not in view  # no ASR confidence data


def test_turn_view_surfaces_optional_confidence():
    # Segments carry the ASR per-utterance probability (AssemblyAI); the
    # turn view exposes the mean plus a per-sentence breakdown.
    segments = [
        {"start": 0.0, "end": 2.0, "speaker": "A", "chunk": 0, "text": "first part", "confidence": 0.98},
        {"start": 2.5, "end": 4.0, "speaker": "A", "chunk": 0, "text": "second part", "confidence": 0.55},
    ]
    turns = speaker_turns(segments)
    assert len(turns) == 1
    view = turn_view(turns, segments, 0)
    assert view["text"] == "first part second part"
    assert view["confidence"] == 0.765  # mean(0.98, 0.55)
    assert view["sentences"] == [
        {"text": "first part", "confidence": 0.98},
        {"text": "second part", "confidence": 0.55},
    ]


def test_turn_view_confidence_with_partial_data():
    # Only segments that carry a numeric confidence contribute; a missing
    # value is surfaced as None in the sentence breakdown.
    segments = [
        {"start": 0.0, "end": 1.0, "speaker": "A", "chunk": 0, "text": "sure", "confidence": 0.9},
        {"start": 1.5, "end": 2.5, "speaker": "A", "chunk": 0, "text": "unsure"},
    ]
    turns = speaker_turns(segments)
    view = turn_view(turns, segments, 0)
    assert view["confidence"] == 0.9
    assert view["sentences"] == [
        {"text": "sure", "confidence": 0.9},
        {"text": "unsure", "confidence": None},
    ]


# --- apply_refined -----------------------------------------------------------


def test_apply_refined_relabels_and_splits_text():
    segments = [
        seg(0.0, 2.0, "A", 0, "first part"),
        seg(2.0, 4.0, "A", 0, "second part"),
        seg(4.0, 6.0, "B", 0, "other"),
    ]
    turns = speaker_turns(segments)
    assert len(turns) == 2
    decisions = {0: ("SPEAKER_03", "hello brave world"), 1: ("SPEAKER_03", "hi")}
    out = apply_refined(segments, turns, decisions)
    assert out[0]["speaker"] == "SPEAKER_03"
    assert out[1]["speaker"] == "SPEAKER_03"
    assert out[2]["speaker"] == "SPEAKER_03"
    # timings preserved
    assert out[0]["start"] == 0.0 and out[0]["end"] == 2.0 and out[0]["chunk"] == 0
    # the corrected turn text is re-split across the turn's segments
    assert " ".join(out[0]["text"].split() + out[1]["text"].split()) == "hello brave world"


def test_apply_refined_empty_text_keeps_original():
    segments = [seg(0.0, 2.0, "A", 0, "original words")]
    turns = speaker_turns(segments)
    out = apply_refined(segments, turns, {0: ("SPEAKER_05", "")})
    assert out[0]["speaker"] == "SPEAKER_05"
    assert out[0]["text"] == "original words"


def test_apply_refined_untouched_segments_kept():
    segments = [
        {"start": 0.0, "end": 2.0},  # no speaker: not part of any turn
        seg(3.0, 5.0, "A", 0, "say it"),
    ]
    turns = speaker_turns(segments)
    out = apply_refined(segments, turns, {0: ("SPEAKER_00", "changed")})
    assert out[0] == {"start": 0.0, "end": 2.0}  # untouched
    assert out[1]["speaker"] == "SPEAKER_00"
    assert out[1]["text"] == "changed"


# --- rewrite_transcript ------------------------------------------------------


def test_rewrite_transcript_maps_by_span():
    transcript = {
        "session_id": "s1",
        "segments": [
            {"start": 0.0, "end": 2.0, "speaker": "A", "text": "old", "words": []},
            {"start": 2.0, "end": 4.0, "speaker": "B", "text": "old2"},
        ],
    }
    refined = [
        {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00", "text": "new"},
        {"start": 2.0, "end": 4.0, "speaker": "SPEAKER_01", "text": "new2"},
    ]
    out = rewrite_transcript(transcript, refined)
    assert out["segments"][0]["speaker"] == "SPEAKER_00"
    assert out["segments"][0]["text"] == "new"
    assert out["segments"][0]["words"] == []  # other keys preserved
    assert out["segments"][1]["speaker"] == "SPEAKER_01"


def test_rewrite_transcript_leaves_unmatched_spans():
    transcript = {"segments": [{"start": 9.0, "end": 10.0, "speaker": "X", "text": "t"}]}
    out = rewrite_transcript(transcript, [])
    assert out["segments"] == [{"start": 9.0, "end": 10.0, "speaker": "X", "text": "t"}]


# --- text-only mode (REFINER_SPEAKERS=false) --------------------------------


def turns_of(segments):
    return speaker_turns(segments)


def test_prompt_version_is_declared_once():
    assert PROMPT_VERSION == "v4"


def test_text_only_schema_has_no_speaker_field():
    item = TEXT_ONLY_SCHEMA["properties"]["turns"]["items"]
    assert "speaker" not in item["properties"]
    assert item["required"] == ["index", "text"]
    speaker_item = REFINE_SCHEMA["properties"]["turns"]["items"]
    assert speaker_item["properties"]["speaker"] == {"type": "string"}
    assert "speaker" in speaker_item["required"]


def test_build_schema_modes_share_the_index_contract():
    for schema in (REFINE_SCHEMA, TEXT_ONLY_SCHEMA):
        item = schema["properties"]["turns"]["items"]
        assert item["properties"]["index"] == {"type": "integer"}
        assert item["properties"]["text"] == {"type": "string"}
        assert "index" in item["required"]
    assert build_schema(include_speakers=False) == TEXT_ONLY_SCHEMA


def test_system_prompt_selects_the_mode():
    assert system_prompt(include_speakers=True) != TEXT_ONLY_SYSTEM_PROMPT
    assert system_prompt(include_speakers=False) == TEXT_ONLY_SYSTEM_PROMPT
    # the text-only prompt must forbid the one thing the engine depends on
    assert "DO NOT change, add or remove speaker information" in TEXT_ONLY_SYSTEM_PROMPT
    assert "{schema}" in TEXT_ONLY_SYSTEM_PROMPT


def test_turn_view_hides_the_label_in_text_only_mode():
    segments = [seg(0.0, 4.0)]
    turns = turns_of(segments)
    assert turn_view(turns, segments, 0)["speaker"] == "SPEAKER_00"
    assert "speaker" not in turn_view(turns, segments, 0, include_speakers=False)
    # everything else survives: the model still needs time and text
    hidden = turn_view(turns, segments, 0, include_speakers=False)
    assert hidden["index"] == 0 and hidden["text"] == "hello world"


def test_window_message_drops_every_speaker_instruction():
    message = build_window_message(
        [{"index": 0, "text": "hi"}],
        context_blocks=["SPEAKER_00: \"hi\""],
        fixed_count=3,
        window_index=0,
        total_windows=1,
        cast_lines=["- Aramil (Alice)"],
        member_count=5,
        include_speakers=False,
    )
    assert "SPEAKER_00" not in message
    assert "canonical SPEAKER_XX" not in message
    assert "already finalized" not in message
    assert "People at the table" not in message
    assert "Correct the text only" in message
    assert "with no speaker field" in message
    assert "- Aramil (Alice)" in message  # the cast still fixes misheard names


def test_window_message_keeps_speaker_instructions_in_speaker_mode():
    message = build_window_message(
        [{"index": 0, "speaker": "SPEAKER_00", "text": "hi"}],
        context_blocks=[],
        fixed_count=0,
        window_index=0,
        total_windows=1,
        member_count=5,
    )
    assert "canonical SPEAKER_XX" in message
    assert "People at the table" in message


def test_parse_decisions_in_text_only_mode_ignores_a_stray_speaker():
    raw = {"turns": [{"index": 0, "text": "fixed", "speaker": "SPEAKER_09"}]}
    assert parse_decisions(raw) == {0: ("SPEAKER_09", "fixed")}
    assert parse_decisions(raw, include_speakers=False) == {0: ("", "fixed")}


def test_keep_labels_restores_the_diarizer_label_verbatim():
    segments = [
        seg(0.0, 4.0, speaker="Speaker A", text="one"),
        seg(5.0, 9.0, speaker="Speaker B", text="two"),
    ]
    turns = turns_of(segments)
    decisions = parse_decisions(
        {"turns": [{"index": 0, "text": "one fixed"}]}, include_speakers=False
    )
    kept = keep_labels(turns, decisions)
    assert kept[0] == ("Speaker A", "one fixed")
    # a turn the model dropped keeps BOTH its label and its wording
    assert kept[1] == ("Speaker B", "")


def test_text_only_round_trip_leaves_labels_untouched():
    """The end-to-end guarantee of the text-only mode: text changes, labels do
    not. This is what makes the engine's labels measurements."""
    segments = [
        seg(0.0, 4.0, speaker="Speaker A", text="i cast firebal"),
        seg(5.0, 9.0, speaker="Speaker B", text="ok"),
    ]
    turns = turns_of(segments)
    raw = {"turns": [{"index": 0, "text": "I cast Fireball"}, {"index": 1, "text": "OK"}]}
    decisions = keep_labels(turns, parse_decisions(raw, include_speakers=False))
    refined = apply_refined(segments, turns, decisions)
    assert [s["speaker"] for s in refined] == ["Speaker A", "Speaker B"]
    assert refined[0]["text"] == "I cast Fireball"


def test_canonicalize_would_have_renamed_them_which_is_why_it_is_skipped():
    """Guards the reason keep_labels exists: canonicalize renumbers a
    non-canonical label, so running it in text-only mode would quietly rewrite
    the diarizer's output."""
    segments = [seg(0.0, 4.0, speaker="Speaker A"), seg(5.0, 9.0, speaker="Zed")]
    turns = turns_of(segments)
    renamed = canonicalize(turns, {})
    assert [renamed[i][0] for i in range(2)] == ["SPEAKER_00", "SPEAKER_01"]
    assert [keep_labels(turns, {})[i][0] for i in range(2)] == ["Speaker A", "Zed"]