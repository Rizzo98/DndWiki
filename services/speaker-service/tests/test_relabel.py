"""Pure re-clustering tests (no torch/Qdrant): turns, UPGMA, relabel."""

import pytest

from app.relabel import (
    centroid,
    relabel_segments,
    speaker_turns,
    upgma_cluster,
)


def seg(start, end, speaker="Speaker A", chunk=0):
    return {"start": start, "end": end, "speaker": speaker, "chunk": chunk}


# --- speaker_turns ----------------------------------------------------------


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


def test_turns_break_on_chunk_boundary():
    # same label, gap 0, but the source chunk changed -> hard boundary
    turns = speaker_turns(
        [seg(299.0, 300.0), seg(300.0, 301.0, chunk=1)]
    )
    assert len(turns) == 2
    assert turns[0].chunk == 0 and turns[1].chunk == 1


def test_turns_skip_invalid_segments():
    turns = speaker_turns(
        [
            {"start": 0.0, "end": 5.0, "speaker": None, "chunk": 0},
            {"start": 0.0, "end": 0.0, "speaker": "Speaker A", "chunk": 0},
            {"start": 3.0, "end": 1.0, "speaker": "Speaker A", "chunk": 0},
            seg(4.0, 6.0),
        ]
    )
    assert [(t.label, t.start, t.end) for t in turns] == [("Speaker A", 4.0, 6.0)]


def test_turns_empty():
    assert speaker_turns([]) == []


# --- upgma_cluster ----------------------------------------------------------


def _v(a, b):
    return [a, b]


def test_upgma_k_merges_to_target():
    # A-ish and B-ish pairs; k=2 forces exactly two clusters
    vectors = [_v(1.0, 0.0), _v(0.99, 0.01), _v(0.0, 1.0), _v(0.01, 0.99)]
    assert upgma_cluster(vectors, k=2) == [0, 0, 1, 1]


def test_upgma_threshold_keeps_distinct():
    # orthogonal -> cosine distance 1.0 > 0.4 -> stay separate
    assert upgma_cluster([_v(1.0, 0.0), _v(0.0, 1.0)], threshold=0.4) == [0, 1]


def test_upgma_threshold_merges_identical():
    assert upgma_cluster([_v(1.0, 0.0), _v(1.0, 0.0)], threshold=0.4) == [0, 0]


def test_upgma_singleton_and_empty():
    assert upgma_cluster([], threshold=0.4) == []
    assert upgma_cluster([_v(1.0, 0.0)], threshold=0.4) == [0]


def test_upgma_requires_k_or_threshold():
    with pytest.raises(ValueError, match="k or threshold"):
        upgma_cluster([_v(1.0, 0.0), _v(0.0, 1.0)])


# --- relabel_segments -------------------------------------------------------


def test_relabel_merges_same_voice_across_chunks():
    # same voice, but raw labels differ across chunks (the bug this fixes)
    segments = [
        {"start": 0.0, "end": 3.0, "speaker": "Speaker A", "text": "hi", "chunk": 0},
        {"start": 300.0, "end": 303.0, "speaker": "Speaker B", "text": "hi again", "chunk": 1},
    ]
    turns = speaker_turns(segments)
    result = relabel_segments(
        segments,
        turns,
        [_v(1.0, 0.0), _v(1.0, 0.0)],  # identical embeddings
        threshold=0.4,
    )
    assert result.labels == ["SPEAKER_00"]
    assert result.labels_by_index == ["SPEAKER_00", "SPEAKER_00"]
    assert [s["speaker"] for s in result.segments] == ["SPEAKER_00", "SPEAKER_00"]
    assert result.stats["n_clusters"] == 1


def test_relabel_keeps_distinct_speakers():
    segments = [
        {"start": 0.0, "end": 3.0, "speaker": "Speaker A", "text": "hi", "chunk": 0},
        {"start": 4.0, "end": 7.0, "speaker": "Speaker B", "text": "yo", "chunk": 0},
    ]
    turns = speaker_turns(segments)
    result = relabel_segments(
        segments, turns, [_v(1.0, 0.0), _v(0.0, 1.0)], threshold=0.4
    )
    assert result.labels == ["SPEAKER_00", "SPEAKER_01"]
    assert result.labels_by_index == ["SPEAKER_00", "SPEAKER_01"]


def test_relabel_k_mode_uses_speaker_count():
    # 3 turns, 2 voices; k=2 forces two clusters regardless of threshold
    segments = [
        {"start": 0.0, "end": 2.0, "speaker": "Speaker A", "text": "a", "chunk": 0},
        {"start": 3.0, "end": 5.0, "speaker": "Speaker B", "text": "b", "chunk": 0},
        {"start": 6.0, "end": 8.0, "speaker": "Speaker A", "text": "a2", "chunk": 1},
    ]
    turns = speaker_turns(segments)
    result = relabel_segments(
        segments,
        turns,
        [_v(1.0, 0.0), _v(0.0, 1.0), _v(1.0, 0.0)],
        k=2,
    )
    assert result.stats["n_clusters"] == 2
    assert result.labels_by_index == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]


def test_relabel_anchor_snapping():
    segments = [
        {"start": 0.0, "end": 3.0, "speaker": "Speaker A", "text": "a", "chunk": 0},
        {"start": 4.0, "end": 7.0, "speaker": "Speaker B", "text": "b", "chunk": 0},
    ]
    turns = speaker_turns(segments)
    anchors = [
        {"user_id": "user-a", "embedding": _v(1.0, 0.0)},
        {"user_id": "user-b", "embedding": _v(0.0, 1.0)},
    ]
    result = relabel_segments(
        segments,
        turns,
        [_v(1.0, 0.0), _v(0.0, 1.0)],
        threshold=0.4,
        anchors=anchors,
        anchor_threshold=0.6,
    )
    assert result.anchors == {
        "SPEAKER_00": ("user-a", 1.0),
        "SPEAKER_01": ("user-b", 1.0),
    }
    assert result.stats["n_anchored"] == 2


def test_relabel_short_unembedded_turn_inherits():
    segments = [
        {"start": 0.0, "end": 5.0, "speaker": "Speaker A", "text": "long", "chunk": 0},
        {"start": 6.0, "end": 6.5, "speaker": "Speaker B", "text": "hi", "chunk": 0},
    ]
    turns = speaker_turns(segments)
    result = relabel_segments(
        segments, turns, [_v(1.0, 0.0), None], threshold=0.4
    )
    # only the first turn is embedded; the short turn inherits its label
    assert result.labels_by_index == ["SPEAKER_00", "SPEAKER_00"]


def test_relabel_no_embedded_turns_leaves_labels_untouched():
    segments = [
        {"start": 0.0, "end": 3.0, "speaker": "Speaker A", "text": "a", "chunk": 0},
        {"start": 4.0, "end": 7.0, "speaker": "Speaker B", "text": "b", "chunk": 0},
    ]
    turns = speaker_turns(segments)
    result = relabel_segments(segments, turns, [None, None], threshold=0.4)
    assert result.labels == []
    assert result.labels_by_index == [None, None]
    assert [s["speaker"] for s in result.segments] == ["Speaker A", "Speaker B"]


def test_relabel_rejects_misaligned_embeddings():
    turns = speaker_turns([seg(0.0, 2.0)])
    with pytest.raises(ValueError, match="aligned"):
        relabel_segments([seg(0.0, 2.0)], turns, [], threshold=0.4)


def test_centroid_unit_norm():
    c = centroid([_v(1.0, 0.0), _v(0.0, 1.0)])
    assert round(c[0], 6) == round(c[1], 6)  # unit-norm mean of orthogonal unit vectors
    assert abs((c[0] ** 2 + c[1] ** 2) - 1.0) < 1e-6
