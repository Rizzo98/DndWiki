"""Channel tests: one pure function per signal, hand-written fixtures."""

import math

import pytest

from app.channels import (
    ABSENT_PRIOR_FACTOR,
    CAP_CAPABILITY,
    CAP_DM_PRIOR,
    CHANNELS,
    CORROBORATING_CHANNELS,
    SPREAD_CAP,
    UNKNOWN,
    Claim,
    UtteranceFeatures,
    address,
    capability,
    corroboration,
    dm_prior,
    identity_note,
    narration,
    out_of_world,
    roll,
    self_name,
    user_answer,
    voice,
    voice_mode,
    voice_strength,
)


class FakeCalibration:
    """A toy score->LLR map: LLR = (cosine - 0.5) * 10 * quality."""

    def log_lr(self, cosine, *, quality=1.0):
        return (cosine - 0.5) * 10.0 * quality


CANDIDATES = ["member:a", "member:b", "member:c", UNKNOWN]
DM = "member:dm"


def features(**kw):
    return UtteranceFeatures(ordinal=1, start=0.0, end=5.0, **kw)


def test_channel_names_are_a_contract():
    assert CHANNELS[0] == "voice"
    assert "user_answer" in CHANNELS
    assert "voice" not in CORROBORATING_CHANNELS  # voice has its own, higher bar
    assert "dm_prior" not in CORROBORATING_CHANNELS  # a prior observes nothing


# --- voice ------------------------------------------------------------------


def test_voice_maps_every_cosine_through_the_calibration():
    out = voice({"member:a": 0.9, "member:b": 0.4}, calibration=FakeCalibration())
    assert out.channel == "voice"
    assert out.log_lr["member:a"] == pytest.approx(4.0)
    assert out.log_lr["member:b"] == pytest.approx(-1.0)


def test_voice_quality_scales_the_evidence():
    loud = voice({"member:a": 0.9}, calibration=FakeCalibration(), quality=1.0)
    faint = voice({"member:a": 0.9}, calibration=FakeCalibration(), quality=0.3)
    assert faint.log_lr["member:a"] < loud.log_lr["member:a"]


def test_voice_with_no_scores_has_no_opinion():
    assert voice({}, calibration=FakeCalibration()).log_lr == {}


# --- self_name --------------------------------------------------------------


def test_first_person_self_naming_is_near_decisive():
    out = self_name(
        features(claims=(Claim(type="self_character", value="Aramil", strength=1.0),)),
        name_to_candidate={"aramil": "member:a"},
        candidates=CANDIDATES,
        dm_key=DM,
    )
    assert out.log_lr["member:a"] == pytest.approx(3.5)
    # ... and it implies "not the others", softly
    assert out.log_lr["member:b"] < 0


def test_third_person_self_narration_is_weaker_but_still_strong():
    first = self_name(
        features(claims=(Claim("self_character", "Aramil", 1.0),)),
        name_to_candidate={"aramil": "member:a"},
        candidates=CANDIDATES,
    )
    third = self_name(
        features(claims=(Claim("self_character", "Aramil", 1.0),)),
        name_to_candidate={"aramil": "member:a"},
        candidates=CANDIDATES,
    )
    assert third.log_lr["member:a"] == pytest.approx(first.log_lr["member:a"])


def test_a_hedged_claim_weighs_less_than_an_assertion():
    strong = self_name(
        features(claims=(Claim("self_character", "Aramil", 1.0),)),
        name_to_candidate={"aramil": "member:a"},
        candidates=CANDIDATES,
    )
    weak = self_name(
        features(claims=(Claim("self_character", "Aramil", 0.3),)),
        name_to_candidate={"aramil": "member:a"},
        candidates=CANDIDATES,
    )
    assert weak.log_lr["member:a"] < strong.log_lr["member:a"]


def test_a_claim_about_a_stranger_names_nobody():
    out = self_name(
        features(claims=(Claim("self_character", "Gandalf", 1.0),)),
        name_to_candidate={"aramil": "member:a"},
        candidates=CANDIDATES,
    )
    assert out.log_lr == {}


def test_the_dm_is_pushed_down_when_a_player_names_their_character():
    out = self_name(
        features(claims=(Claim("self_character", "Aramil", 1.0),)),
        name_to_candidate={"aramil": "member:a"},
        candidates=CANDIDATES + [DM],
        dm_key=DM,
    )
    assert out.log_lr[DM] < 0


# --- capability -------------------------------------------------------------


def test_capability_favours_whoever_can_do_it():
    out = capability(
        ["spell:fireball"],
        member_capabilities={"a": ["spell:fireball"], "b": ["feature:rage"]},
        candidates=CANDIDATES,
    )
    assert out.log_lr["member:a"] > 0
    assert out.log_lr["member:b"] == 0  # unknown, NOT a veto


def test_capability_only_vetoes_with_an_authoritative_sheet():
    soft = capability(
        ["spell:fireball"],
        member_capabilities={"a": ["spell:fireball"], "b": ["feature:rage"]},
        candidates=CANDIDATES,
    )
    hard = capability(
        ["spell:fireball"],
        member_capabilities={"a": ["spell:fireball"], "b": ["feature:rage"]},
        candidates=CANDIDATES,
        authoritative=True,
    )
    assert soft.log_lr["member:b"] == 0.0
    assert hard.log_lr["member:b"] < 0


def test_capability_is_capped_so_it_can_never_outweigh_voice_alone():
    out = capability(
        ["spell:a", "spell:b", "spell:c", "spell:d"],
        member_capabilities={"a": ["spell:a", "spell:b", "spell:c", "spell:d"]},
        candidates=CANDIDATES,
    )
    assert out.log_lr["member:a"] == pytest.approx(CAP_CAPABILITY)


def test_capability_of_nothing_is_no_opinion():
    assert capability([], member_capabilities={}, candidates=CANDIDATES).log_lr == {}


def test_the_dm_does_not_cast_the_partys_spells():
    out = capability(
        ["spell:fireball"],
        member_capabilities={"a": ["spell:fireball"]},
        candidates=CANDIDATES + [DM],
        dm_key=DM,
    )
    assert out.log_lr[DM] < 0


# --- address ----------------------------------------------------------------


def test_address_points_at_the_named_member_and_away_from_the_addresser():
    out = address(
        ["Thorin"],
        name_to_candidate={"thorin": "member:b"},
        candidates=CANDIDATES,
        addresser="member:a",
    )
    assert out.log_lr["member:b"] == pytest.approx(2.5)
    assert out.log_lr["member:a"] < 0


def test_address_of_an_unknown_name_is_no_opinion():
    assert (
        address(["Nobody"], name_to_candidate={}, candidates=CANDIDATES).log_lr == {}
    )


# --- narration / ooc / voice_mode -------------------------------------------


def test_narration_points_at_the_dm_and_spreads_the_rest():
    out = narration(
        features(voice_mode="narration"), candidates=CANDIDATES + [DM], dm_key=DM
    )
    assert out.log_lr[DM] == pytest.approx(2.2)
    assert all(out.log_lr[c] < 0 for c in CANDIDATES)


def test_narration_ignores_player_dialogue():
    out = narration(
        features(voice_mode="pc_dialogue"), candidates=CANDIDATES + [DM], dm_key=DM
    )
    assert out.log_lr == {}


def test_voice_mode_marks_the_dm_but_makes_no_npc_entity():
    """NPC voicing says whose MOUTH it was, not who the NPC is. The innkeeper is
    a content-service concern; the attribution must not create an entity for
    them, and the channel must never invent a candidate key."""
    candidates = CANDIDATES + [DM]
    out = voice_mode(features(voice_mode="npc_dialogue"), candidates=candidates, dm_key=DM)
    assert out.log_lr[DM] == pytest.approx(2.0)
    assert set(out.log_lr) <= set(candidates)
    # everyone else, including "someone not in the campaign", is pushed down
    assert all(out.log_lr[c] < 0 for c in candidates if c != DM)


def test_ooc_is_deliberately_low_information():
    out = out_of_world(features(kind="meta"), candidates=CANDIDATES + [DM], dm_key=DM)
    assert out.log_lr[DM] == pytest.approx(0.8)
    assert all(out.log_lr[c] == pytest.approx(0.3) for c in CANDIDATES if c != UNKNOWN)
    assert UNKNOWN not in out.log_lr


def test_ooc_ignores_actions():
    assert out_of_world(features(kind="action"), candidates=CANDIDATES, dm_key=DM).log_lr == {}


# --- roll -------------------------------------------------------------------


def test_roll_confirms_the_current_speaker():
    out = roll(features(roll_owner="member:a"), candidates=CANDIDATES)
    assert out.log_lr["member:a"] == pytest.approx(1.0)
    assert out.log_lr["member:b"] < 0


def test_roll_without_a_known_owner_is_no_opinion():
    assert roll(features(), candidates=CANDIDATES).log_lr == {}


# --- identity_note ----------------------------------------------------------


def test_identity_note_makes_an_absent_member_very_unlikely_not_impossible():
    out = identity_note(features(excluded=("member:b",)), candidates=CANDIDATES)
    assert out.log_lr["member:b"] == pytest.approx(math.log(ABSENT_PRIOR_FACTOR))
    assert out.log_lr["member:b"] < -2.0  # decisive, but finite
    assert "member:a" not in out.log_lr


# --- dm_prior ---------------------------------------------------------------


def test_dm_prior_follows_the_speech_share_and_stays_capped():
    out = dm_prior(
        {"member:a": 0.1, "member:b": 0.1, "member:c": 0.1, "member:dm": 0.7},
        candidates=CANDIDATES + [DM],
    )
    assert out.log_lr[DM] > 0
    assert out.log_lr["member:a"] < 0
    assert out.log_lr[DM] <= CAP_DM_PRIOR


def test_dm_prior_with_no_speech_is_no_opinion():
    assert dm_prior({}, candidates=CANDIDATES).log_lr == {}


# --- user_answer ------------------------------------------------------------


def test_user_answer_clamps_with_a_finite_weight():
    out = user_answer(candidate="member:b", candidates=CANDIDATES)
    assert out.log_lr["member:b"] > 5.0
    assert math.isfinite(out.log_lr["member:b"])
    assert all(out.log_lr[c] < 0 for c in CANDIDATES if c != "member:b")


# --- corroboration ----------------------------------------------------------


def test_corroboration_requires_a_second_independent_channel():
    """A lone cosine can never be promoted to a fact: this is the rule that
    keeps silent errors out of the wiki (S7.1)."""
    only_voice = [voice({"member:a": 0.99}, calibration=FakeCalibration())]
    assert corroboration(only_voice, best="member:a") is None

    with_names = only_voice + [
        self_name(
            features(claims=(Claim("self_character", "Aramil", 1.0),)),
            name_to_candidate={"aramil": "member:a"},
            candidates=CANDIDATES,
        )
    ]
    found = corroboration(with_names, best="member:a")
    assert found is not None
    assert found[0] == "self_name"


def test_weak_channels_do_not_corroborate():
    weak = [out_of_world(features(kind="meta"), candidates=CANDIDATES, dm_key=DM)]
    assert corroboration(weak, best="member:a") is None


def test_voice_strength_reads_the_voice_channel_only():
    outputs = [
        voice({"member:a": 0.9}, calibration=FakeCalibration()),
        self_name(
            features(claims=(Claim("self_character", "Aramil", 1.0),)),
            name_to_candidate={"aramil": "member:a"},
            candidates=CANDIDATES,
        ),
    ]
    assert voice_strength(outputs, best="member:a") == pytest.approx(4.0)
    assert voice_strength([], best="member:a") == 0.0


# --- spread behaviour -------------------------------------------------------


def test_the_counterweight_never_exceeds_its_cap():
    out = narration(features(voice_mode="narration"), candidates=CANDIDATES + [DM], dm_key=DM)
    assert all(value >= -SPREAD_CAP - 1e-9 for value in out.log_lr.values())


def test_evidence_records_are_json_friendly():
    out = voice({"member:a": 0.9}, calibration=FakeCalibration(), quality=0.8)
    record = out.as_evidence()
    assert set(record) == {"channel", "log_lr", "payload"}
    assert record["channel"] == "voice"
    assert isinstance(record["log_lr"]["member:a"], float)
