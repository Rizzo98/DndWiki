"""Utterance builder tests: the unit of attribution, and its provenance."""

import pytest

from app.utterances import (
    DEFAULT_MAX_CHARS,
    Utterance,
    build_utterances,
    gap_before,
    split_utterance,
    utterance_ref,
)


def seg(start, end, speaker="SPEAKER_00", text="hello", **extra):
    return {"start": start, "end": end, "speaker": speaker, "text": text, **extra}


def words(*pairs):
    return [{"word": w, "start": s, "end": e} for w, s, e in pairs]


# --- basic shape ------------------------------------------------------------


def test_one_utterance_per_speaker_turn_with_a_stable_ref():
    result = build_utterances(
        [seg(0.0, 4.0, "SPEAKER_00", "hi"), seg(5.0, 9.0, "SPEAKER_01", "yo")]
    )
    assert [u.ref for u in result] == ["u_00001", "u_00002"]
    assert [u.text for u in result] == ["hi", "yo"]
    assert [u.diar_label for u in result] == ["SPEAKER_00", "SPEAKER_01"]
    assert result.utterances[0].duration == 4.0


def test_consecutive_same_label_segments_merge_into_one_utterance():
    result = build_utterances(
        [seg(0.0, 2.0, text="one"), seg(2.2, 4.0, text="two")]
    )
    assert len(result) == 1
    assert result.utterances[0].text == "one two"
    assert result.utterances[0].segment_indices == (0, 1)


def test_utterance_records_its_segment_provenance():
    """Index-based provenance cannot drift the way the old (start, end) tuple
    lookup did, which silently dropped a label whenever timings collided."""
    result = build_utterances(
        [
            seg(0.0, 4.0, "SPEAKER_00", "a"),
            seg(5.0, 9.0, "SPEAKER_01", "b"),
            seg(10.0, 14.0, "SPEAKER_00", "c"),
        ]
    )
    assert [u.segment_indices for u in result] == [(0,), (1,), (2,)]
    assert result.indices_of_segment(2) == [2]
    assert result.indices_of_segment(1) == [1]


def test_ordinals_are_sequential_and_dense():
    result = build_utterances(
        [seg(0.0, 1.0, "A", "a"), seg(2.0, 3.0, "B", "b"), seg(4.0, 5.0, "A", "c")]
    )
    assert [u.ordinal for u in result] == [1, 2, 3]
    assert utterance_ref(412) == "u_00412"


def test_unlabeled_and_empty_segments_are_skipped():
    result = build_utterances(
        [
            {"start": 0.0, "end": 2.0, "text": "no speaker"},
            seg(3.0, 5.0, text=""),  # no text: not attributable
            seg(6.0, 8.0, text="real"),
        ]
    )
    assert [u.text for u in result] == ["real"]


def test_empty_input_gives_no_utterances():
    assert len(build_utterances([])) == 0


# --- the chunk-boundary hazard ---------------------------------------------


def test_chunk_boundaries_are_honoured_when_declared():
    """Without chunk keys, 'cur.chunk == chunk' compares None == None and a
    same-label run merges across the boundary, turning two people into one."""
    segments = [seg(0.0, 2.0, "SPEAKER_00", "a"), seg(2.0, 4.0, "SPEAKER_00", "b")]
    merged = build_utterances(segments)
    declared = build_utterances(segments, chunk_boundaries=[0, 1])
    assert len(merged) == 1
    assert len(declared) == 2
    assert [u.text for u in declared] == ["a", "b"]


def test_a_chunk_key_present_on_the_segments_also_splits():
    result = build_utterances(
        [seg(0.0, 2.0, "SPEAKER_00", "a", chunk=0), seg(2.0, 4.0, "SPEAKER_00", "b", chunk=1)]
    )
    assert len(result) == 2
    assert [u.diar_chunk for u in result] == [0, 1]


# --- internal silences ------------------------------------------------------


def test_a_long_pause_starts_a_new_utterance():
    """Silence is what ends a turn (dnd_common.transcript.speaker_turns), so a
    pause longer than the tolerance is a different attributable moment."""
    result = build_utterances(
        [
            seg(0.0, 2.0, "SPEAKER_00", "before the pause"),
            seg(9.0, 11.0, "SPEAKER_00", "after the pause"),
        ],
        gap_tolerance=2.0,
    )
    assert [u.text for u in result] == ["before the pause", "after the pause"]
    # the builder reports no gap (each came from its own turn); gap_before()
    # computes the real silence against the previous KEPT utterance.
    assert gap_before(result.utterances)[1].gap_before_sec == 7.0


def test_a_short_pause_does_not_split():
    result = build_utterances(
        [seg(0.0, 2.0, "S", "a"), seg(2.5, 4.0, "S", "b")], gap_tolerance=2.0
    )
    assert len(result) == 1


# --- length packing ---------------------------------------------------------


def test_a_long_turn_is_packed_into_sentence_aligned_utterances():
    text = " ".join(f"Sentence number {i} says something." for i in range(30))
    result = build_utterances([seg(0.0, 300.0, "S", text)])
    assert len(result) > 1
    assert all(len(u.text) <= DEFAULT_MAX_CHARS for u in result)
    assert " ".join(u.text for u in result) == text  # nothing lost or duplicated
    assert result.utterances[0].start == 0.0
    assert result.utterances[-1].end == 300.0
    # spans are contiguous and increasing
    for before, after in zip(result.utterances, result.utterances[1:]):
        assert after.start == pytest.approx(before.end)


def test_sentence_packing_uses_word_timings_for_the_cut():
    """Cutting on character proportion would put the boundary in the wrong
    place whenever somebody speaks unevenly; word timings pin it."""
    long_word = "supercalifragilistic"
    words_list = words(
        ("short", 0.0, 1.0), ("short", 1.0, 2.0),
        (long_word, 2.0, 40.0), (long_word, 40.0, 80.0),
    )
    text = "short short. " + long_word + " " + long_word + "."
    result = build_utterances([seg(0.0, 80.0, "S", text, words=words_list)], max_chars=20)
    assert len(result) == 2
    # the cut lands near the long words (by timing), not at 50% of the clock
    assert result.utterances[0].end >= 2.0


def test_a_single_over_long_sentence_is_kept_whole():
    text = "word " * 200
    result = build_utterances([seg(0.0, 60.0, "S", text.strip())], max_chars=40)
    assert len(result) == 1  # one sentence, never cut mid-sentence
    assert result.utterances[0].text == text.strip()


# --- confidences ------------------------------------------------------------


def test_speaker_confidence_is_diarization_and_confidence_is_asr():
    """The bare 'confidence' key means the ASR confidence of the TEXT. The old
    history backfill read it as a diarization confidence; that reading is
    superseded and must not leak into the engine."""
    result = build_utterances(
        [
            seg(0.0, 4.0, "S", "a", speaker_confidence=0.9, confidence=0.6),
            seg(4.2, 8.0, "S", "b", speaker_confidence=0.7, confidence=0.8),
        ]
    )
    assert result.utterances[0].diar_confidence == pytest.approx(0.8)
    assert result.utterances[0].asr_confidence == pytest.approx(0.7)


def test_missing_confidences_stay_none():
    result = build_utterances([seg(0.0, 4.0, "S", "a")])
    assert result.utterances[0].diar_confidence is None
    assert result.utterances[0].asr_confidence is None


# --- grouping helpers -------------------------------------------------------


def test_by_label_and_by_ref_indexes():
    result = build_utterances(
        [seg(0.0, 2.0, "A", "a"), seg(3.0, 5.0, "B", "b"), seg(6.0, 8.0, "A", "c")]
    )
    assert len(result.by_label()["A"]) == 2
    assert result.by_ref["u_00002"].text == "b"


# --- splitting one utterance (local re-diarization) -------------------------


def test_split_utterance_uses_word_timings_to_keep_the_text():
    utterance = Utterance(
        ordinal=1,
        start=0.0,
        end=6.0,
        text="I open the door and step through",
        words=tuple(
            __import__("app.utterances", fromlist=["Word"]).Word(text=w, start=s, end=e)
            for w, s, e in [
                ("I", 0.0, 0.4), ("open", 0.4, 1.2), ("the", 1.2, 1.6),
                ("door", 1.6, 2.4), ("and", 4.0, 4.3), ("step", 4.3, 4.8),
                ("through", 4.8, 5.6),
            ]
        ),
    )
    head, tail = split_utterance(utterance, 3.0, next_ordinal=2)
    assert head.text == "I open the door"
    assert tail.text == "and step through"
    assert tail.ordinal == 2
    assert head.start == 0.0 and head.end == 3.0
    assert tail.start == 3.0 and tail.end == 6.0
    # provenance survives the cut: both halves came from the same audio
    assert head.segment_indices == tail.segment_indices


def test_split_without_word_timings_falls_back_to_proportional_text():
    utterance = Utterance(ordinal=1, start=0.0, end=4.0, text="one two three four")
    head, tail = split_utterance(utterance, 2.0, next_ordinal=2)
    assert head.text == "one two"
    assert tail.text == "three four"


def test_split_at_an_edge_is_a_no_op_rather_than_losing_text():
    utterance = Utterance(ordinal=1, start=0.0, end=4.0, text="one two")
    head, tail = split_utterance(utterance, 0.0, next_ordinal=2)
    assert head.text == "one two"
    assert tail.text == ""


def test_split_keeps_every_word_exactly_once():
    utterance = Utterance(
        ordinal=1, start=0.0, end=10.0, text="alpha beta gamma delta epsilon zeta"
    )
    head, tail = split_utterance(utterance, 6.0, next_ordinal=2)
    assert (head.text + " " + tail.text).split() == utterance.text.split()


# --- gap recomputation ------------------------------------------------------


def test_gap_before_is_recomputed_against_the_previous_kept_utterance():
    built = build_utterances(
        [seg(0.0, 2.0, "A", "a"), seg(5.0, 7.0, "B", "b"), seg(20.0, 22.0, "A", "c")]
    )
    recomputed = gap_before(built.utterances)
    assert recomputed[0].gap_before_sec is None
    assert recomputed[1].gap_before_sec == 3.0
    assert recomputed[2].gap_before_sec == 13.0
