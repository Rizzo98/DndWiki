"""Per-observation window building tests (pure, no audio)."""

import uuid

from app.observations import (
    Observation,
    build_observations,
    observation_key,
    observation_point_id,
    plan_observations,
)


def seg(start, end, speaker="SPEAKER_00", text="hi", **extra):
    return {"start": start, "end": end, "speaker": speaker, "text": text, **extra}


def test_one_observation_per_turn_with_its_segment_indices():
    observations = build_observations(
        [
            seg(0.0, 2.0, "SPEAKER_00", "one"),
            seg(2.2, 4.0, "SPEAKER_00", "two"),
            seg(4.5, 6.0, "SPEAKER_01", "three"),
        ]
    )
    assert [o.label for o in observations] == ["SPEAKER_00", "SPEAKER_01"]
    assert observations[0].segment_indices == (0, 1)
    assert observations[0].text == "one two"
    assert observations[0].start == 0.0 and observations[0].end == 4.0
    assert observations[1].segment_indices == (2,)


def test_observations_get_stable_keys_and_point_ids():
    observations = build_observations([seg(0.0, 2.0, "SPEAKER_03")])
    assert observations[0].key == observation_key("SPEAKER_03", 0)
    first = observation_point_id("sess", "SPEAKER_03", 0)
    second = observation_point_id("sess", "SPEAKER_03", 0)
    assert first == second
    assert uuid.UUID(first)  # a real, well-formed uuid
    assert first != observation_point_id("sess", "SPEAKER_03", 1)
    assert first != observation_point_id("other", "SPEAKER_03", 0)


def test_chunk_boundaries_split_observations_even_without_chunk_keys():
    segments = [seg(0.0, 2.0), seg(2.0, 4.0), seg(4.0, 6.0)]
    merged = build_observations(segments)
    declared = build_observations(segments, chunk_boundaries=[0, 2])
    assert len(merged) == 1
    assert len(declared) == 2
    assert declared[0].segment_indices == (0, 1)
    assert declared[1].segment_indices == (2,)


def test_observation_carries_mean_diarization_confidence():
    observations = build_observations(
        [
            seg(0.0, 2.0, speaker_confidence=0.9),
            seg(2.1, 4.0, speaker_confidence=0.5),
            seg(6.0, 8.0),  # no confidence reported at all
        ]
    )
    assert observations[0].diar_confidence == 0.7  # mean of 0.9 and 0.5
    assert observations[1].diar_confidence is None


def test_plan_marks_only_long_enough_turns_embeddable():
    batch = plan_observations(
        [
            seg(0.0, 0.4, "SPEAKER_00"),  # too short to embed
            seg(1.0, 4.0, "SPEAKER_01"),  # fine
            seg(5.0, 5.5, "SPEAKER_00"),  # too short
        ],
        min_sec=1.0,
    )
    assert [o.index for o in batch.observations] == [0, 1, 2]
    assert batch.embeddable == [1]
    assert batch.labels == ["SPEAKER_00", "SPEAKER_01"]


def test_plan_groups_observations_by_label():
    batch = plan_observations(
        [
            seg(0.0, 2.0, "SPEAKER_00"),
            seg(3.0, 5.0, "SPEAKER_01"),
            seg(6.0, 8.0, "SPEAKER_00"),
        ],
        min_sec=1.0,
    )
    groups = batch.by_label()
    assert len(groups["SPEAKER_00"]) == 2
    assert len(groups["SPEAKER_01"]) == 1


def test_empty_segments_produce_nothing():
    batch = plan_observations([], min_sec=1.0)
    assert batch.observations == [] and batch.embeddable == []


def test_observation_duration_property():
    obs = Observation(index=0, label="V", chunk=None, start=1.0, end=4.5)
    assert obs.duration == 3.5
