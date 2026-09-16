"""End-to-end pass tests: segments in, a belief, questions and an artifact out."""

import random

from app.attribution_artifact import build_artifact, confident_refs, render_line, status_of
from app.calibration import Calibration
from app.capabilities import Capability, CapabilityStore
from app.evidence import parse_evidence
from app.pipeline import ObservationInput, PlanInput, plan_session, roster_index
from app.scenes import Scene
from app.statuses import StatusThresholds

ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"
DM = "33333333-3333-3333-3333-333333333333"
CAMPAIGN = "44444444-4444-4444-4444-444444444444"
SESSION = "55555555-5555-5555-5555-555555555555"


def member(member_id, player, character, role="player"):
    return {
        "member_id": member_id,
        "player_name": player,
        "character_name": character,
        "role": role,
    }


MEMBERS = [
    member(DM, "Dana", None, role="dm"),
    member(ALICE, "Alice", "Aramil"),
    member(BOB, "Bob", "Thorin"),
]


def unit_vector(seed, dim=12):
    r = random.Random(seed)
    v = [r.gauss(0.0, 1.0) for _ in range(dim)]
    norm = sum(x * x for x in v) ** 0.5
    return tuple(x / norm for x in v)


VOICE = {
    "dm": unit_vector(1),
    "alice": unit_vector(2),
    "bob": unit_vector(3),
}


def seg(start, end, speaker, text, **extra):
    return {"start": start, "end": end, "speaker": speaker, "text": text, **extra}


def segment_list():
    return [
        seg(0.0, 5.0, "SPEAKER_00", "as you push the door open you see three goblins"),
        seg(6.0, 11.0, "SPEAKER_01", "I cast fireball on the three goblins"),
        seg(12.0, 17.0, "SPEAKER_02", "Thorin, what do you do?"),
        seg(18.0, 23.0, "SPEAKER_03", "I rage and attack the captain"),
    ]


def evidence_pass():
    return parse_evidence(
        {
            "language": "en",
            "utterances": [
                {
                    "ref": "u_00001",
                    "kind": "narration",
                    "voice_mode": "narration",
                    "gist": "describes the room behind the door",
                    "stakes": 0.6,
                },
                {
                    "ref": "u_00002",
                    "kind": "action",
                    "voice_mode": "pc_dialogue",
                    "gist": "casts Fireball on the three goblins",
                    "capability_requirements": ["spell:fireball"],
                    "claims": [{"type": "self_character", "value": "Aramil", "strength": 1.0}],
                    "stakes": 0.9,
                },
                {
                    "ref": "u_00003",
                    "kind": "dialogue",
                    "voice_mode": "narration",
                    "gist": "calls Thorin's turn",
                    "addresses": [{"name": "Thorin", "target": "next_turn"}],
                    "stakes": 0.4,
                },
                {
                    "ref": "u_00004",
                    "kind": "action",
                    "voice_mode": "pc_dialogue",
                    "gist": "rages and attacks the captain",
                    "capability_requirements": ["feature:rage"],
                    "stakes": 0.85,
                },
            ],
        }
    )


def observations(calibration_floor=0.85):
    """One observation per utterance, each closest to its true voice."""
    return {
        "u_00001": ObservationInput(
            ref="u_00001",
            label="SPEAKER_00",
            quality=0.95,
            vector=VOICE["dm"],
            nearest=(("member:" + DM, 0.92),),
        ),
        "u_00002": ObservationInput(
            ref="u_00002",
            label="SPEAKER_01",
            quality=0.95,
            vector=VOICE["alice"],
            nearest=(("member:" + ALICE, 0.91), ("member:" + BOB, 0.30)),
        ),
        "u_00003": ObservationInput(
            ref="u_00003",
            label="SPEAKER_02",
            quality=0.9,
            vector=VOICE["dm"],
            nearest=(("member:" + DM, 0.88),),
        ),
        "u_00004": ObservationInput(
            ref="u_00004",
            label="SPEAKER_03",
            quality=0.9,
            vector=VOICE["bob"],
            nearest=(("member:" + BOB, 0.9), ("member:" + ALICE, 0.25)),
        ),
    }


def capabilities():
    store = CapabilityStore()
    store.add(Capability(member_id=ALICE, capability="spell:fireball", source="wiki", confidence=0.9))
    store.add(Capability(member_id=BOB, capability="feature:rage", source="wiki", confidence=0.9))
    return store


#: A stretch that puts one player character somewhere else, so the pass has
#: presence questions to build. The reading itself is tested in test_scenes; what
#: this fixture is for is the path from a reading to a question the DM sees.
SCENES = (
    Scene(
        index=1,
        start_ordinal=1,
        end_ordinal=2,
        first_ref="u_00001",
        last_ref="u_00002",
        location="the ruin behind the door",
        present=("Aramil",),
        absent=("Thorin",),
        reason="Thorin stays at the door",
    ),
    Scene(
        index=2,
        start_ordinal=3,
        end_ordinal=4,
        first_ref="u_00003",
        last_ref="u_00004",
        location="the ruin behind the door",
        present=("Aramil", "Thorin"),
        reason="Thorin catches up",
    ),
)


def evidence_with_scenes():
    """The same evidence, plus a reading that puts Thorin somewhere else."""
    pass_ = evidence_pass()
    pass_.scenes = SCENES
    return pass_


def build_plan(**overrides):
    kwargs = {
        "session_id": SESSION,
        "campaign_id": CAMPAIGN,
        "segments": segment_list(),
        "members": MEMBERS,
        "observations": observations(),
        "evidence": evidence_pass(),
        "calibration": Calibration(),
        "capabilities": capabilities(),
        "thresholds": StatusThresholds(),
    }
    kwargs.update(overrides)
    return plan_session(PlanInput(**kwargs))


# --- roster -----------------------------------------------------------------


def test_roster_index_maps_shouted_names_to_candidates():
    keys, labels, dm_key, names = roster_index(MEMBERS)
    assert dm_key == f"member:{DM}"
    assert names["aram" + "il"] == f"member:{ALICE}"
    assert names["thorin"] == f"member:{BOB}"
    assert labels[f"member:{ALICE}"] == "Alice — Aramil"
    assert "unknown" in keys


def test_labels_never_leak_a_raw_diarization_label():
    _, labels, _, _ = roster_index(MEMBERS)
    assert all("SPEAKER" not in label for label in labels.values())




def test_the_pass_attributes_a_simple_session():
    plan = build_plan()
    assert len(plan.utterances) == 4
    assert plan.coverage > 0.5
    statuses = {ref: v.status for ref, v in plan.verdicts.items()}
    assert statuses["u_00001"] in {"auto_high", "auto_low", "propagated", "unresolved"}


def test_a_line_with_no_corroborating_channel_is_planned_not_crashed():
    """The common case, and the one that took the worker down on a real session.

    Ordinary dialogue carries voice evidence and a DM prior and nothing else.
    Both outrank "no evidence" when the pass picks the leading candidate, so the
    pass asks corroboration() for a second, independent reason - and gets None,
    because there isn't one. That is the DESIGNED answer (it is what stops a
    lone cosine from reaching the wiki), not an error, and the caller used to
    index it anyway.
    """
    plan = build_plan(
        segments=[seg(0.0, 5.0, "SPEAKER_00", "so what do we do about the door")],
        observations={
            "u_00001": ObservationInput(
                ref="u_00001",
                label="SPEAKER_00",
                quality=0.5,
                vector=VOICE["alice"],
                nearest=(("member:" + ALICE, 0.45),),
            )
        },
        evidence=parse_evidence(
            {"language": "en", "utterances": [{"ref": "u_00001", "kind": "dialogue", "stakes": 0.3}]}
        ),
    )
    verdict = plan.verdicts["u_00001"]
    # auto_low, not auto_high: one weak cosine is not a second reason. The
    # verdict also records that no independent channel supported the leader,
    # which is exactly the condition that used to crash the pass.
    assert verdict.status == "auto_low"
    assert verdict.corroborated_by is None


def test_capability_plus_corroboration_places_the_wizard():
    """'I cast Fireball' + a self-naming claim + a matching voice is exactly the
    multi-channel agreement auto_high is supposed to require."""
    plan = build_plan()
    best = plan.inference.posteriors["u_00002"]
    assert best[f"member:{ALICE}"] == max(best.values())
    assert plan.verdicts["u_00002"].status in {"auto_high", "auto_low"}
    assert plan.verdicts["u_00002"].status != "unresolved"


def test_a_different_capability_places_a_different_member():
    plan = build_plan()
    best = plan.inference.posteriors["u_00004"]
    assert best[f"member:{BOB}"] == max(best.values())


def test_the_narrator_goes_to_the_dm():
    plan = build_plan()
    best = plan.inference.posteriors["u_00001"]
    assert best[f"member:{DM}"] == max(best.values())


def test_asked_to_speak_the_next_turn_shifts_away_from_the_addresser():
    plan = build_plan()
    answer = plan.inference.posteriors["u_00004"]
    assert answer[f"member:{DM}"] < answer[f"member:{BOB}"]


def test_voice_identities_are_anonymous_and_numbered():
    plan = build_plan()
    assert plan.voices
    assert all(vid.startswith("V") for vid in plan.voices)
    assert set(plan.handles.values()) == {f"V{i}" for i in range(1, len(plan.voices) + 1)}


def test_the_pass_is_deterministic():
    first = build_plan()
    second = build_plan()
    assert first.inference.posteriors == second.inference.posteriors
    assert [v.status for v in first.verdicts.values()] == [
        v.status for v in second.verdicts.values()
    ]


def test_a_session_with_no_segments_is_an_empty_plan_not_an_error():
    plan = build_plan(segments=[])
    assert plan.utterances == []
    assert plan.coverage == 1.0
    assert plan.questions == []


def test_a_session_without_audio_degrades_to_text_only():
    """No voice evidence is a degraded run, not a failed one: the text channels
    still get their chance and the coverage is honestly lower (S16)."""
    plan = build_plan(observations={})
    assert len(plan.utterances) == 4
    best = plan.inference.posteriors["u_00002"]
    assert best[f"member:{ALICE}"] == max(best.values())  # the self-name + capability still work


def test_an_unreachable_roster_falls_back_to_unknown():
    plan = build_plan(members=[], observations={})
    assert plan.roster == []
    assert plan.verdicts  # still attributed, to 'unknown' at worst


# --- questions --------------------------------------------------------------


def test_a_session_the_engine_can_answer_produces_no_questions():
    """The tidy fixture is attributed well, and rule 1 then has nothing to ask.

    This used to assert the opposite, and it passed for the wrong reason: the
    questions it found were the VOICE questions, which existed on every session
    regardless of whether the engine had already settled it - including on
    sessions where the "voice" was three different people. What is left asks only
    about what the engine genuinely cannot decide.
    """
    plan = build_plan()
    assert plan.questions == []


def test_the_pass_generates_questions_the_dm_can_answer():
    """Without voice evidence the moments stay open, which is when there IS
    something to ask: every question is about one of those moments."""
    plan = build_plan(observations={})
    assert plan.questions, "an unattributed session has something to ask"
    for question in plan.questions:
        assert question.target_utterances, "every question is about a moment"
        assert question.hook, "every question carries a concrete moment"
        assert question.options[-1].key == "idk", "I don't know is always offered"
        assert not any("SPEAKER" in option.label for option in question.options)


def test_a_scene_reading_does_not_produce_questions_of_its_own():
    """The reading's job is the belief, not the review.

    It still excludes a member the table put elsewhere (S12.6) - what it no
    longer does is ask the DM to confirm the cast, because a scene holds almost
    everybody almost always and the answer was therefore a foregone "yes"."""
    plan = build_plan(observations={}, evidence=evidence_with_scenes())
    kinds = {question.kind for question in plan.questions}
    assert not (kinds & {"presence", "who_is_voice", "same_voice", "different_voice"})
    assert all(question.target_utterances for question in plan.questions)


def test_the_review_can_plan_and_answer():
    plan = build_plan()
    scored, decision = plan.review.next_question()
    if scored is None:
        assert decision.stop is True
        return
    outcome = plan.review.answer(scored, scored.question.options[0].key)
    assert outcome.coverage_after >= outcome.coverage_before
    assert plan.review.status()["questions_asked"] == 1


# --- the artifact -----------------------------------------------------------


def test_the_artifact_carries_a_status_per_utterance():
    artifact = build_artifact(build_plan(), revision=3)
    assert artifact["attribution_revision"] == 3
    assert artifact["session_id"] == SESSION
    assert len(artifact["utterances"]) == 4
    for utterance in artifact["utterances"]:
        assert utterance["status"] in {
            "user_confirmed", "auto_high", "propagated", "auto_low", "unresolved"
        }
        assert utterance["id"].startswith("u_")
        assert utterance["text"]


def test_the_artifact_never_contains_a_raw_label_or_a_player_name_in_a_speaker():
    artifact = build_artifact(build_plan())
    for utterance in artifact["utterances"]:
        speaker = utterance.get("speaker") or {}
        assert "SPEAKER" not in str(speaker.get("label") or "")
        # the label is "Player — Character"; character_name is what pages use
        assert speaker.get("character_name") in {None, "Aramil", "Thorin"}


def test_the_artifact_carries_the_stretches_and_their_solo_member():
    """The stretch note content-service could not see until now.

    A narration is filed under the DM, so a beat about somebody else had no name
    to use and came out as "un personaggio". The reading knows; the artifact has
    to carry it. 'solo_character' is the narrow licence to name a subject the
    speaker cannot supply: ONE party member present AND the rest named elsewhere.
    """
    artifact = build_artifact(build_plan(evidence=evidence_with_scenes()))
    stretches = artifact["stretches"]
    assert [s["index"] for s in stretches] == [1, 2]
    first, second = stretches
    assert first["place"] == "the ruin behind the door"
    assert first["from"] == "u_00001"
    assert first["absent"] == ["Thorin"]
    assert first["solo_character"] == "Aramil"
    # ...and the second stretch names nobody: two members present licenses
    # nothing about who did what.
    assert second["solo_character"] is None


def test_a_stretch_without_a_stated_absence_licenses_no_name():
    """A cast list is not a statement that the others were away: only a reading
    that names somebody ELSEWHERE says anything about who is here."""
    only_present = (
        Scene(
            index=1,
            start_ordinal=1,
            end_ordinal=2,
            first_ref="u_00001",
            last_ref="u_00002",
            location="the ruin behind the door",
            present=("Aramil",),
        ),
    )
    pass_ = evidence_pass()
    pass_.scenes = only_present
    artifact = build_artifact(build_plan(evidence=pass_))
    assert artifact["stretches"][0]["solo_character"] is None


def test_an_artifact_without_a_reading_carries_no_stretches():
    assert build_artifact(build_plan())["stretches"] == []


def test_confident_refs_are_exactly_the_wiki_gate_input():
    artifact = build_artifact(build_plan())
    refs = confident_refs(artifact)
    for utterance in artifact["utterances"]:
        expected = utterance["status"] in {"user_confirmed", "auto_high", "propagated"}
        assert (utterance["id"] in refs) is expected


def test_render_line_marks_uncertainty_in_the_text_itself():
    artifact = build_artifact(build_plan())
    ref = artifact["utterances"][0]["id"]
    status = status_of(artifact, ref)
    line = render_line(artifact, ref)
    assert line.startswith(f"[{ref} ")
    if status == "unresolved":
        assert "(unattributed)" in line
    elif status == "auto_low":
        assert line.split("] ")[1].startswith(tuple(
            (artifact["utterances"][0].get("speaker") or {}).get("character_name") or "(",
        )) or "(unattributed)" in line


def test_unknown_ref_renders_empty_rather_than_raising():
    artifact = build_artifact(build_plan())
    assert render_line(artifact, "u_99999") == ""
    assert status_of(artifact, "u_99999") == "unresolved"


# --- evidence parsing -------------------------------------------------------


def test_parse_evidence_drops_malformed_rows_instead_of_failing():
    parsed = parse_evidence(
        {
            "utterances": [
                {"ref": "u_1", "kind": "not-a-kind", "stakes": 5.0},
                {"no_ref": True},
                "not a dict",
                {"ref": "u_2", "claims": [{"type": "self_character"}]},
            ]
        }
    )
    assert "u_2" in parsed.items or "u_1" in parsed.items
    assert parsed.item("u_1").kind is None
    assert parsed.item("u_1").stakes == 1.0


def test_parse_evidence_of_garbage_is_empty_not_fatal():
    assert parse_evidence(None).items == {}
    assert parse_evidence("nonsense").items == {}
