"""Question generator tests: the six kinds, their cost, and the suppression rules."""


from app.channels import UNKNOWN
from app.questions import (
    DEFAULT_IDK_PROBABILITY,
    DM_OFFER_THRESHOLD,
    IDK_OPTION,
    MIN_QUESTION_STAKES,
    QUESTION_COSTS,
    QUESTION_KINDS,
    STRANGER_OPTION,
    CandidateQuestion,
    QuestionContext,
    QuestionOption,
    UtteranceView,
    VoiceView,
    different_voice,
    generate,
    is_settled,
    new_person,
    options_for,
    overlap_ratio,
    same_voice,
    suppress,
    who_did,
    who_is_voice,
    who_said,
)

A, B, C = "member:a", "member:b", "member:c"
DM = "member:dm"


def context(**kw):
    base_kw = {
        "candidates": [A, B, C, DM, UNKNOWN],
        "label_of": {A: "Alice — Aramil (Wizard)", B: "Bob — Thorin (Fighter)",
                     C: "Carla — Keth (Rogue)", DM: "The Dungeon Master"},
        "dm_key": DM,
    }
    base_kw.update(kw)
    return QuestionContext(**base_kw)


def view(ref="u_00001", **kw):
    base_kw = {
        "ref": ref,
        "start": 100.0,
        "end": 104.0,
        "text": "I cast fireball on the three goblins",
        "gist": "casts Fireball on the three goblins",
        "kind": "action",
        "stakes": 0.85,
        "voice_id": "V3",
    }
    base_kw.update(kw)
    return UtteranceView(**base_kw)


TORN = {A: 0.45, B: 0.35, C: 0.1, UNKNOWN: 0.1}
SETTLED = {A: 0.95, B: 0.03, UNKNOWN: 0.02}


# --- kinds and costs --------------------------------------------------------


def test_the_six_kinds_and_their_costs_are_the_contract():
    assert set(QUESTION_KINDS) == {
        "who_did",
        "who_said",
        "same_voice",
        "different_voice",
        "who_is_voice",
        "new_person",
    }
    assert QUESTION_COSTS["same_voice"] == 0.7  # cheapest: a binary audio A/B
    assert QUESTION_COSTS["who_is_voice"] == 1.3  # the DM has to listen
    assert QUESTION_COSTS["who_did"] == 1.0


# --- options ----------------------------------------------------------------


def test_options_always_end_with_i_dont_know():
    options = options_for(TORN, context=context())
    assert options[-1].key == IDK_OPTION
    assert options[-2].key == STRANGER_OPTION
    keys = [o.key for o in options]
    assert keys.index(A) < keys.index(B)  # posterior order


def test_the_dm_is_only_offered_when_narration_has_a_real_chance():
    without = options_for({A: 0.5, B: 0.5}, context=context())
    assert DM not in [o.key for o in without]
    with_dm = options_for({A: 0.5, DM: DM_OFFER_THRESHOLD + 0.01}, context=context())
    assert DM in [o.key for o in with_dm]


def test_negligible_candidates_are_not_offered():
    options = options_for({A: 0.9, B: 0.00001}, context=context())
    assert B not in [o.key for o in options]


def test_option_labels_are_player_and_character_never_a_raw_label():
    options = options_for({A: 0.9}, context=context())
    label = next(o for o in options if o.key == A).label
    assert label == "Alice — Aramil (Wizard)"
    assert "SPEAKER" not in label


# --- the six generators -----------------------------------------------------


def test_who_did_is_phrased_from_the_gist():
    question = who_did(view(), TORN, context=context())
    assert question.kind == "who_did"
    assert question.prompt_text == "Who casts Fireball on the three goblins?"
    assert question.hook["quote"].startswith("I cast fireball")
    assert question.hook["audio"] == {"start": 100.0, "end": 104.0}


def test_who_did_needs_an_action_and_a_gist():
    assert who_did(view(kind="dialogue"), TORN, context=context()) is None
    assert who_did(view(gist=None), TORN, context=context()) is None


def test_who_did_needs_more_than_one_plausible_actor():
    assert who_did(view(), {A: 0.97, UNKNOWN: 0.03}, context=context()) is None


def test_who_said_quotes_the_line():
    question = who_said(
        view(kind="dialogue", text="we enter from the eastern passage"), TORN,
        context=context(),
    )
    assert question.kind == "who_said"
    assert "we enter from the eastern passage" in question.prompt_text


def test_who_is_voice_needs_a_long_unplaced_voice():
    voice = VoiceView(
        id="V9", handle="V9", speech_sec=720.0, utterance_refs=("u_1",),
        start=10.0, end=730.0, posterior={A: 0.35, B: 0.3, C: 0.2, UNKNOWN: 0.15},
    )
    question = who_is_voice(voice, context=context())
    assert question is not None
    assert "12 minutes" in question.prompt_text
    assert question.cost == 1.3


def test_who_is_voice_yields_to_new_person_when_the_voice_is_a_stranger():
    """Asking both would be asking twice: a stranger-dominated voice is
    new_person's question, and it is cheaper to answer."""
    voice = VoiceView(id="V9", handle="V9", speech_sec=720.0, utterance_refs=("u_1",),
                      posterior={A: 0.2, B: 0.15, UNKNOWN: 0.65})
    assert who_is_voice(voice, context=context()) is None
    assert new_person(voice, context=context()) is not None


def test_who_is_voice_is_suppressed_when_a_candidate_is_plausible():
    voice = VoiceView(id="V9", handle="V9", speech_sec=720.0, utterance_refs=("u_1",),
                      posterior={A: 0.5})
    assert who_is_voice(voice, context=context()) is None


def test_who_is_voice_ignores_a_short_voice():
    voice = VoiceView(id="V9", handle="V9", speech_sec=30.0, utterance_refs=("u_1",),
                      posterior={UNKNOWN: 0.9})
    assert who_is_voice(voice, context=context()) is None


def test_new_person_fires_on_a_stranger_and_offers_a_way_back():
    voice = VoiceView(id="V9", handle="V9", speech_sec=200.0, utterance_refs=("u_1",),
                      posterior={UNKNOWN: 0.7, A: 0.2, B: 0.1})
    question = new_person(voice, context=context())
    assert question is not None
    keys = [o.key for o in question.options]
    assert keys[0] == UNKNOWN
    assert IDK_OPTION in keys
    assert A in keys  # the DM can still say it was somebody after all


def test_new_person_stays_quiet_below_its_threshold():
    voice = VoiceView(id="V9", handle="V9", speech_sec=200.0, utterance_refs=("u_1",),
                      posterior={UNKNOWN: 0.5, A: 0.5})
    assert new_person(voice, context=context()) is None


def test_same_voice_and_different_voice_are_binary_audio_questions():
    voice = VoiceView(id="V1", handle="V1", speech_sec=200.0, utterance_refs=("u_1", "u_2"))
    split = same_voice(voice, context=context(), clip_a=(0.0, 4.0), clip_b=(600.0, 604.0))
    assert split.prompt_text == "Same person?"
    assert [o.key for o in split.options] == ["same", "different", IDK_OPTION]
    assert split.hook["audio_a"]["start"] == 0.0

    other = VoiceView(id="V2", handle="V2", speech_sec=90.0, utterance_refs=("u_9",))
    merge = different_voice(
        voice, other, context=context(), clip_a=(0.0, 4.0), clip_b=(900.0, 904.0)
    )
    assert merge.target_voices == ["V1", "V2"]
    assert len(merge.target_utterances) == 3


# --- suppression ------------------------------------------------------------


def test_rule_1_the_posterior_already_answers_it():
    confident = context().thresholds
    assert is_settled(SETTLED, thresholds=confident, confident=True) is True
    assert is_settled(TORN, thresholds=confident, confident=True) is False


def test_a_peaked_posterior_the_engine_will_not_act_on_is_still_asked_about():
    """The statuses exist because one weak channel is not corroboration (S7.1).

    A moment like that comes out `auto_low`: peaked, and refused by the wiki
    gate. Rule 1 used to read the peak as knowledge and drop every content
    question of the session, which is how a session with seven unattributed
    voices produced exactly one structural question and then stopped.
    """
    assert is_settled(SETTLED, thresholds=context().thresholds, confident=False) is False


def test_a_settled_utterance_is_never_asked_about():
    confident = context(confident_utterances={"u_00001"})
    question = who_did(view(), TORN, context=confident)
    pairs = suppress([question], context=confident, posteriors={"u_00001": SETTLED})
    assert pairs == [(question, "posterior_already_answers_it")]


def test_rule_4_filler_is_never_asked_about():
    question = who_did(view(stakes=MIN_QUESTION_STAKES - 0.01), TORN, context=context())
    pairs = suppress([question], context=context(), posteriors={"u_00001": TORN})
    assert pairs[0][1] == "filler"


def test_rule_3_an_already_covered_subject_is_not_re_asked():
    question = who_did(view(), TORN, context=context())
    covered = context(covered_utterances={"u_00001"})
    # a content question's overlap is measured on MOMENTS, not on the voice
    assert overlap_ratio(question, covered_utterances={"u_00001"}) == 1.0
    assert overlap_ratio(question, covered_voices={"V3"}) == 0.0
    pairs = suppress([question], context=covered, posteriors={"u_00001": TORN})
    assert pairs[0][1] == "subject_already_covered"


def test_structural_questions_are_suppressed_by_voice_coverage():
    voice = VoiceView(id="V1", handle="V1", speech_sec=200.0, utterance_refs=("u_1",))
    question = same_voice(voice, context=context(), clip_a=(0.0, 1.0), clip_b=(2.0, 3.0))
    pairs = suppress(
        [question], context=context(covered_voices={"V1"}), posteriors={}
    )
    assert pairs[0][1] == "subject_already_covered"


def test_rule_8_a_question_with_no_concrete_moment_is_a_generator_bug():
    bare = CandidateQuestion(
        kind="who_did",
        prompt_text="Who did something?",
        options=[QuestionOption(key=IDK_OPTION, label="I don't know")],
        target_utterances=["u_00001"],
        target_voices=[],
        hook={},
        cost=1.0,
        mean_stakes=0.9,
    )
    pairs = suppress([bare], context=context(), posteriors={"u_00001": TORN})
    assert pairs[0][1] == "no_concrete_moment"


def test_a_good_question_survives_every_rule():
    question = who_did(view(), TORN, context=context())
    pairs = suppress([question], context=context(), posteriors={"u_00001": TORN})
    assert pairs == [(question, "")]


# --- generate ---------------------------------------------------------------


def test_generate_collects_content_and_structural_questions():
    utterances = {"u_00001": view(), "u_00002": view(ref="u_00002", kind="dialogue")}
    posteriors = {"u_00001": TORN, "u_00002": TORN}
    voices = [
        VoiceView(id="V9", handle="V9", speech_sec=720.0, utterance_refs=("u_00001",),
                  start=0.0, end=720.0, posterior={UNKNOWN: 0.7, A: 0.3}),
    ]
    questions = generate(
        context=context(), utterances=utterances, posteriors=posteriors, voices=voices
    )
    kinds = {q.kind for q in questions}
    assert "who_did" in kinds
    assert "who_said" in kinds
    assert "who_is_voice" in kinds or "new_person" in kinds


def test_generate_proposes_a_merge_only_for_similar_centroids():
    voices = [
        VoiceView(id="V1", handle="V1", speech_sec=200, utterance_refs=("u_1",),
                  centroid=(1.0, 0.0)),
        VoiceView(id="V2", handle="V2", speech_sec=200, utterance_refs=("u_2",),
                  centroid=(0.99, 0.01)),
    ]
    clips = {"V1": (0.0, 4.0), "V2": (100.0, 104.0)}
    merged = generate(
        context=context(), utterances={}, posteriors={}, voices=voices, clips=clips
    )
    assert any(q.kind == "different_voice" for q in merged)

    far = [
        VoiceView(id="V1", handle="V1", speech_sec=200, utterance_refs=("u_1",),
                  centroid=(1.0, 0.0)),
        VoiceView(id="V2", handle="V2", speech_sec=200, utterance_refs=("u_2",),
                  centroid=(0.0, 1.0)),
    ]
    assert not any(
        q.kind == "different_voice"
        for q in generate(
            context=context(), utterances={}, posteriors={}, voices=far, clips=clips
        )
    )


def test_an_identity_without_audio_is_never_proposed_for_a_merge():
    voices = [
        VoiceView(id="V1", handle="V1", speech_sec=200, utterance_refs=("u_1",)),
        VoiceView(id="V2", handle="V2", speech_sec=200, utterance_refs=("u_2",)),
    ]
    clips = {"V1": (0.0, 4.0), "V2": (100.0, 104.0)}
    assert not any(
        q.kind == "different_voice"
        for q in generate(
            context=context(), utterances={}, posteriors={}, voices=voices, clips=clips
        )
    )


def test_question_payload_is_json_friendly():
    question = who_did(view(), TORN, context=context())
    payload = question.as_payload()
    assert set(payload) == {
        "kind", "prompt_text", "options", "target_utterances", "target_voices",
        "hook", "cost", "mean_stakes",
    }
    assert all(isinstance(o, dict) for o in payload["options"])


def test_default_idk_probability_is_a_real_option_not_an_afterthought():
    assert 0.0 < DEFAULT_IDK_PROBABILITY < 0.5
