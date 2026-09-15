"""Shared speaker-turn grouping tests (moved from speaker-service relabel.py)."""

from dnd_common.transcript import GAP_TOLERANCE_SEC, Turn, speaker_turns


def seg(start, end, speaker="Speaker A", chunk=0):
    return {"start": start, "end": end, "speaker": speaker, "chunk": chunk}


def test_turns_group_consecutive_same_label():
    turns = speaker_turns(
        [
            seg(0.0, 2.0),
            seg(2.5, 4.0),  # 0.5 s gap -> same turn
            seg(5.0, 7.0, "Speaker B"),  # label change
            seg(7.5, 9.0),  # back to Speaker A
        ]
    )
    assert [(t.label, t.start, t.end) for t in turns] == [
        ("Speaker A", 0.0, 4.0),
        ("Speaker B", 5.0, 7.0),
        ("Speaker A", 7.5, 9.0),
    ]
    assert [t.indices for t in turns] == [[0, 1], [2], [3]]


def test_turns_break_on_long_gap():
    turns = speaker_turns([seg(0.0, 2.0), seg(10.0, 12.0)])
    assert len(turns) == 2


def test_turns_break_on_chunk_change():
    # Same raw label across a chunk boundary is NOT one turn: labels restart
    # per chunk, so identity cannot be assumed.
    turns = speaker_turns([seg(0.0, 2.0, chunk=0), seg(2.5, 4.0, chunk=1)])
    assert len(turns) == 2
    assert [t.chunk for t in turns] == [0, 1]


def test_turns_skip_unlabeled_and_empty():
    turns = speaker_turns(
        [
            {"start": 0.0, "end": 2.0},  # no speaker
            seg(3.0, 3.0),  # zero duration
            seg(4.0, 6.0),
        ]
    )
    assert len(turns) == 1
    assert turns[0].indices == [2]


def test_turn_is_dataclass():
    t = Turn(label="A", start=0.0, end=1.0, chunk=0, indices=[0])
    assert t.label == "A" and t.indices == [0]


def test_gap_tolerance_constant_positive():
    assert GAP_TOLERANCE_SEC > 0


# --- declared chunk boundaries (the None == None hazard) --------------------


def test_turns_merge_across_absent_chunk_keys_when_nothing_is_declared():
    # Segments assembled from a single-pass diarizer carry no chunk key at all;
    # grouping the whole file is correct there.
    turns = speaker_turns(
        [
            {"start": 0.0, "end": 2.0, "speaker": "Speaker A"},
            {"start": 2.5, "end": 4.0, "speaker": "Speaker A"},
        ]
    )
    assert len(turns) == 1


def test_turns_break_on_declared_chunk_boundaries_without_chunk_keys():
    # The caller knows the segments came from two per-chunk responses but the
    # segments carry no chunk key: without the declaration cur.chunk == chunk
    # compares None == None and the two people merge into one turn.
    segments = [
        {"start": 0.0, "end": 2.0, "speaker": "Speaker A"},
        {"start": 2.0, "end": 4.0, "speaker": "Speaker A"},
        {"start": 4.0, "end": 6.0, "speaker": "Speaker A"},
    ]
    merged = speaker_turns(segments)
    declared = speaker_turns(segments, chunk_boundaries=[0, 2])
    assert len(merged) == 1
    assert len(declared) == 2
    assert declared[0].indices == [0, 1]
    assert declared[1].indices == [2]


def test_declared_boundary_at_index_zero_is_ignored():
    turns = speaker_turns(
        [seg(0.0, 2.0), seg(2.5, 4.0)], chunk_boundaries=[0]
    )
    assert len(turns) == 1

