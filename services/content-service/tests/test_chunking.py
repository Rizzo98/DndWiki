"""Tests for transcript chunking."""

from app.chunking import (
    build_view_lines,
    chunk_lines,
    chunk_transcript,
    estimate_tokens,
    format_timestamp,
)


def test_estimate_tokens():
    assert estimate_tokens("hello world") >= 1
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 400) == 100


def test_format_timestamp():
    assert format_timestamp(0) == "00:00:00"
    assert format_timestamp(3724) == "01:02:04"
    assert format_timestamp(59.9) == "00:00:59"
    assert format_timestamp(-5) == "00:00:00"


def test_build_view_lines_names_and_skips_empty():
    segments = [
        {"start": 0.0, "speaker": "SPEAKER_00", "text": "Hello."},
        {"start": 3.0, "speaker": "SPEAKER_01", "text": "  Hi.  "},
        {"start": 6.0, "speaker": None, "text": "Overlap."},
        {"start": 9.0, "speaker": "SPEAKER_00", "text": ""},
    ]
    lines = build_view_lines(segments, {"SPEAKER_00": "Aragorn"})
    assert lines == [
        "[00:00:00] Aragorn: Hello.",
        "[00:00:03] SPEAKER_01: Hi.",
        "[00:00:06] UNKNOWN: Overlap.",
    ]


def test_chunk_lines_overlap_and_boundaries():
    lines = [f"line-{i}" for i in range(10)]
    chunks = chunk_lines(lines, max_tokens=20, overlap=0.1)
    # each line ~7 chars -> 2 tokens; 20 tokens -> ~10 lines per chunk => 1 chunk
    assert len(chunks) == 1

    # tiny budget forces multiple chunks; overlap brings the boundary line back
    chunks = chunk_lines(lines, max_tokens=8, overlap=0.5)
    assert len(chunks) >= 2
    assert all(chunks)  # no empty chunks
    # all original lines are covered by at least one chunk
    covered = [line for chunk in chunks for line in chunk]
    assert set(covered) == set(lines)
    # with 50% overlap, chunk 2 starts halfway back through chunk 1
    assert chunks[1][0] == chunks[0][4]


def test_chunk_lines_keeps_oversized_line_whole():
    lines = ["short", "x" * 2000, "short"]
    chunks = chunk_lines(lines, max_tokens=10, overlap=0.1)
    assert chunks[1] == ["x" * 2000]  # kept whole, not split
    assert "".join(l for c in chunks for l in c).count("x") == 2000


def test_chunk_lines_empty():
    assert chunk_lines([], 100, 0.1) == []


def test_chunk_transcript_end_to_end():
    segments = [
        {"start": i * 10.0, "speaker": "SPEAKER_00", "text": "word " * 40}
        for i in range(20)
    ]
    chunks = chunk_transcript(
        segments, {"SPEAKER_00": "Aragorn"}, max_tokens=60, overlap=0.1
    )
    assert len(chunks) > 1
    assert "[00:00:00] Aragorn:" in chunks[0][0]
