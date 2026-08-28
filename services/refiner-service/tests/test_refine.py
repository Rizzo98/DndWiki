"""Pure refinement logic tests (no litellm): windows, parsing, splitting, apply."""

import pytest
from dnd_common.transcript import Turn, speaker_turns

from app.refine import (
    RefineError,
    apply_refined,
    build_context_blocks,
    canonicalize,
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
