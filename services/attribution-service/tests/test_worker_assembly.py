"""Worker assembly tests: the pass must be handed real inputs, not stubs.

These exist because the first version of the worker ran the engine with NO voice
evidence and NO evidence pass and still "succeeded" - it produced a plan where
everything was unresolved. A pipeline that degrades gracefully is only safe if
something asserts that it is not degrading for no reason.
"""

from __future__ import annotations

import random

# --- fixtures ---------------------------------------------------------------


def unit_vector(seed: int, dim: int = 12) -> tuple[float, ...]:
    rng = random.Random(seed)
    values = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    norm = sum(v * v for v in values) ** 0.5
    return tuple(v / norm for v in values)


ALICE_VOICE = unit_vector(1)
BOB_VOICE = unit_vector(2)


def build_segments() -> list[dict]:
    return [
        {"start": 0.0, "end": 6.0, "speaker": "SPEAKER_00", "text": "you see three goblins"},
        {"start": 7.0, "end": 12.0, "speaker": "SPEAKER_01", "text": "I cast fireball"},
        {"start": 13.0, "end": 18.0, "speaker": "SPEAKER_02", "text": "Thorin, what do you do?"},
        {"start": 19.0, "end": 24.0, "speaker": "SPEAKER_03", "text": "I rage and attack"},
    ]


def roster() -> list[dict]:
    return [
        {"member_id": "member-dm", "user_id": "user-dana", "player_name": "Dana", "role": "dm"},
        {
            "member_id": "member-alice",
            "user_id": "user-alice",
            "player_name": "Alice",
            "character_name": "Aramil",
            "role": "player",
        },
        {
            "member_id": "member-bob",
            "user_id": "user-bob",
            "player_name": "Bob",
            "character_name": "Thorin",
            "role": "player",
        },
    ]


def voice_records() -> list[dict]:
    """What speaker-service returns for a well-identified session."""
    return [
        {
            "observation_id": "SPEAKER_00#0",
            "label": "SPEAKER_00",
            "start": 0.0,
            "end": 6.0,
            "quality": 0.9,
            "vector": list(ALICE_VOICE),
            "nearest_centroids": [{"member_id": "member-dm", "cosine": 0.88}],
        },
        {
            "observation_id": "SPEAKER_01#1",
            "label": "SPEAKER_01",
            "start": 7.0,
            "end": 12.0,
            "quality": 0.9,
            "vector": list(ALICE_VOICE),
            "nearest_centroids": [
                {"member_id": "member-alice", "cosine": 0.91},
                {"member_id": "member-bob", "cosine": 0.30},
            ],
        },
        {
            "observation_id": "SPEAKER_02#2",
            "label": "SPEAKER_02",
            "start": 13.0,
            "end": 18.0,
            "quality": 0.9,
            "vector": list(ALICE_VOICE),
            "nearest_centroids": [{"member_id": "member-dm", "cosine": 0.86}],
        },
        {
            "observation_id": "SPEAKER_03#3",
            "label": "SPEAKER_03",
            "start": 19.0,
            "end": 24.0,
            "quality": 0.9,
            "vector": list(BOB_VOICE),
            "nearest_centroids": [{"member_id": "member-bob", "cosine": 0.90}],
        },
    ]

from app.capabilities import CapabilityStore
from app.clients.base import ServiceError
from app.workers import compute


class FakeStorage:
    def __init__(self, segments=None):
        self.segments = segments if segments is not None else build_segments()
        self.written: dict[str, dict] = {}

    async def read_json(self, key, bucket=None):
        if key.endswith("diarization.json"):
            return {"segments": self.segments, "language": "en"}
        return None

    async def put_json(self, key, obj, bucket=None):
        self.written[key] = obj


def test_a_pass_with_questions_announces_the_review():
    event = compute.review_event(
        session_id="s",
        campaign_id="c",
        coverage=0.4,
        unresolved=3,
        questions_planned=2,
        run_id="r",
    )
    assert event.type == "attribution.review.ready"
    assert event.payload["questions_planned"] == 2


def test_a_pass_with_nothing_to_ask_still_moves_the_session_on():
    """'attribution_ready' is not a resting status: no event of its own can
    leave it, so a pass with no questions has to announce that the review is
    over. Otherwise the session sat there until the DM opened the page and
    pressed Finish on a review with no question in it."""
    event = compute.review_event(
        session_id="s",
        campaign_id="c",
        coverage=0.4,
        unresolved=3,
        questions_planned=0,
        run_id="r",
    )
    assert event.type == "attribution.review.completed"
    assert event.payload["outcome"] == "nothing_to_ask"
    assert event.payload["session_id"] == "s"
    assert event.payload["review_run_id"] == "r"


def test_observations_are_matched_to_the_utterances_they_cover():
    from app.utterances import build_utterances

    utterances = build_utterances(build_segments()).utterances
    records = voice_records()
    attached = compute.observations_from_records(records, utterances)
    assert attached, "the voice evidence must reach the utterances"
    for ref, observation in attached.items():
        assert observation.vector, f"{ref} has no vector: purity and kNN would be inert"
        assert observation.nearest


def test_an_utterance_outside_every_observation_gets_no_voice_evidence():
    from app.utterances import build_utterances, gap_before

    # keep only the FIRST observation: the later utterances are then uncovered
    utterances = gap_before(build_utterances(build_segments()).utterances)
    attached = compute.observations_from_records(voice_records()[:1], utterances)
    assert len(attached) < len(utterances)
    assert attached


def test_a_legacy_user_keyed_print_still_resolves_to_a_member():
    assert compute._candidate_key({"user_id": "u1"}) == "user:u1"
    assert compute._candidate_key({"member_id": "m1"}) == "member:m1"
    assert compute._candidate_key({"member_id": "m1", "user_id": "u1"}) == "member:m1"
    assert compute._candidate_key({}) is None


def test_user_to_member_maps_linked_members():
    mapping = compute.user_to_member(roster())
    assert mapping["user-alice"] == "member-alice"


def test_the_roster_lines_carry_what_each_member_can_do():
    capabilities = CapabilityStore()
    from app.capabilities import from_dm_sheet

    for capability in from_dm_sheet({"member-alice": ["spell:fireball"]}):
        capabilities.add(capability)
    lines = compute._roster_lines(roster(), capabilities)
    joined = "\n".join(lines)
    assert "Alice" in joined and "Aramil" in joined
    assert "spell:fireball" in joined


async def test_an_unreachable_observation_store_degrades_instead_of_failing(monkeypatch):
    class Down:
        async def voice_observations(self, session_id):
            raise ServiceError("speaker-service down")

    async def boom(self, session_id):
        raise ServiceError("speaker-service down")

    monkeypatch.setattr(compute.SpeakerServiceClient, "voice_observations", boom)
    records = await compute._fetch_observations("s", settings())
    assert records == []


async def test_the_evidence_pass_is_skipped_cleanly_when_the_llm_is_unavailable(monkeypatch):
    async def boom(self, lines, *, window, total):
        raise RuntimeError("llm down")

    monkeypatch.setattr(compute.EvidenceLLM, "extract", boom)
    from app.utterances import build_utterances

    pass_ = await compute._run_evidence(
        build_utterances(build_segments()).utterances, roster(), CapabilityStore(), settings()
    )
    assert pass_.items == {}  # empty, not an exception


def settings():
    from app.core.config import ServiceSettings

    return ServiceSettings()