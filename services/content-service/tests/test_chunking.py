"""Tests for transcript chunking."""

from itertools import pairwise

from app.chunking import (
    LINES_PER_BEAT,
    MAX_BEATS,
    MIN_BEATS,
    beat_budget,
    build_view_lines,
    chunk_lines,
    chunk_ranges,
    chunk_transcript,
    estimate_tokens,
    format_timestamp,
    line_marker,
    owned_parts,
    owned_ranges,
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



# --------------------------------------------------------------------------
# owned ranges: which part of the session each chunk NARRATES
# --------------------------------------------------------------------------
#
# The overlap exists so an entity near a boundary is seen twice. Applied to the
# story it does the opposite of what it is for: two writers, each shown half of
# one scene, describe that scene twice in different words, and merge_summary_lines
# de-duplicates on identical text - so BOTH lines survive and the composer is
# handed two versions of one moment.


def test_chunk_ranges_slices_are_what_chunk_lines_returns():
    lines = [f"line-{i}" for i in range(10)]
    ranges = chunk_ranges(lines, max_tokens=8, overlap=0.5)
    assert [lines[start:end] for start, end in ranges] == chunk_lines(lines, 8, 0.5)


def test_chunk_ranges_is_empty_without_lines():
    assert chunk_ranges([], 100, 0.1) == []
    assert owned_ranges([]) == []


def test_owned_ranges_tile_the_session_exactly():
    """No gap, no overlap, every line narrated by exactly one chunk."""
    lines = [f"line-{i}" for i in range(40)]
    ranges = chunk_ranges(lines, max_tokens=8, overlap=0.5)
    owned = owned_ranges(ranges)
    assert len(owned) == len(ranges)
    assert owned[0] == ranges[0]
    for (_, previous_end), (start, end) in pairwise(owned):
        assert start == previous_end, "the ranges must meet, not overlap or skip"
        assert end > start, "an owned range must not be empty"
    covered = [index for start, end in owned for index in range(start, end)]
    assert covered == list(range(len(lines)))


def test_owned_ranges_start_where_the_previous_chunk_stopped():
    """The repeated overlap line belongs to the chunk that saw it first."""
    lines = [f"line-{i}" for i in range(10)]
    ranges = chunk_ranges(lines, max_tokens=8, overlap=0.5)
    owned = owned_ranges(ranges)
    assert ranges[1][0] < owned[1][0] == ranges[0][1]


def test_a_single_chunk_owns_everything_it_sees():
    lines = [f"line-{i}" for i in range(4)]
    ranges = chunk_ranges(lines, max_tokens=100, overlap=0.1)
    assert owned_ranges(ranges) == ranges == [(0, 4)]


def test_owned_ranges_advance_for_one_line_chunks():
    """An oversized line is a chunk of its own; the ranges must still advance."""
    lines = ["x" * 2000, "y" * 2000, "z" * 2000]
    ranges = chunk_ranges(lines, max_tokens=10, overlap=0.5)
    assert [end - start for start, end in owned_ranges(ranges)] == [1, 1, 1]


def test_line_marker_names_the_line_either_way_a_view_marks_it():
    assert line_marker("[u_00436 00:36:58] Hann Caleto: ...") == "u_00436"
    assert line_marker("[00:36:58] Hann Caleto: ...") == "00:36:58"
    assert line_marker("no marker here") == ""

# --------------------------------------------------------------------------
# the beat budget: how much of the story one chunk owes
# --------------------------------------------------------------------------
#
# The partition (v16) alone was not enough, and this session showed why: every
# chunk was still asked for "1-3 SHORT lines" however much of the session it
# owned, so a chunk whose part ended with a scene left that scene OUT rather than
# telling it twice. Two runs out of two dropped the girl carrying lunch to her
# uncle - the scene this whole fixture exists for.


def _numbered(count: int) -> list[str]:
    return [f"[u_{index:05d} 00:00:00] A: line" for index in range(1, count + 1)]


def test_owned_parts_carry_the_span_and_the_size_of_each_part():
    lines = _numbered(10)
    ranges = chunk_ranges(lines, max_tokens=8, overlap=0.5)
    owned = owned_ranges(ranges)
    parts = owned_parts(lines, ranges)
    assert len(parts) == len(ranges)
    for part, (start, end) in zip(parts, owned):
        assert part.start == line_marker(lines[start])
        assert part.end == line_marker(lines[end - 1])
        assert part.lines == end - start
    # the spans are a partition: the composer relies on two chunks never sharing
    # one, because that is what makes "different spans" mean "could not see each
    # other" rather than "alphabetically different"
    assert parts[0].end != parts[1].start


def test_owned_parts_is_empty_without_ranges():
    assert owned_parts([], []) == []


def test_the_beat_budget_grows_with_the_part_and_stays_in_range():
    small_low, small_high = beat_budget(54)  # ~4 minutes of play
    big_low, big_high = beat_budget(164)  # ~13 minutes
    assert small_low < big_low
    assert MIN_BEATS <= small_low <= small_high <= MAX_BEATS
    assert MIN_BEATS <= big_low <= big_high <= MAX_BEATS


def test_the_beat_budget_never_asks_for_fewer_than_the_floor():
    """A short part still gets beats: a part nobody narrates is a lost scene."""
    assert beat_budget(1)[0] == MIN_BEATS
    assert beat_budget(0)[0] == MIN_BEATS


def test_the_compression_setting_actually_changes_the_budget():
    """The one lever on how much material the composer is handed. It was wired as a
    parameter that the body ignored - the signature took 'lines_per_beat' and the
    arithmetic kept dividing by the module constant - so an A/B on it measured
    nothing at all (21 beats against 22) while looking like a result."""
    assert beat_budget(164) == beat_budget(164, lines_per_beat=LINES_PER_BEAT)
    assert beat_budget(164, lines_per_beat=50)[0] < beat_budget(164, lines_per_beat=25)[0]
    # half the beats for the same part, floor and ceiling still respected
    assert beat_budget(164, lines_per_beat=1000) == (MIN_BEATS, MIN_BEATS + 1)


def test_the_beat_budget_caps_a_very_long_part():
    assert beat_budget(100_000)[1] == MAX_BEATS


def test_the_budget_of_a_real_session_beats_the_flat_three_per_chunk():
    """The five owned parts of the fixture session, in transcript lines."""
    owned = (164, 135, 136, 160, 54)
    assert sum(beat_budget(lines)[0] for lines in owned) >= 20
    assert sum(beat_budget(lines)[1] for lines in owned) <= 40
    # what the pipeline used to ask for: at most 3 per chunk, 15 for the session
    assert sum(beat_budget(lines)[0] for lines in owned) > 3 * len(owned) - 3
