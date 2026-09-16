"""Question generator tests: the kinds, their cost, and the suppression rules."""


from app.channels import UNKNOWN
from app.questions import (
    DEFAULT_IDK_PROBABILITY,
    DM_OFFER_THRESHOLD,
    IDK_OPTION,
    MIN_QUESTION_STAKES,
    QUESTION_COSTS,
    QUESTION_KINDS,
    RETIRED_KINDS,
    STRANGER_OPTION,
    CandidateQuestion,
    QuestionContext,
    QuestionOption,
    UtteranceView,
    generate,
    is_settled,
    options_for,
    overlap_ratio,
    suppress,
    who_did,
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


def test_the_kinds_and_their_costs_are_the_contract():
    assert set(QUESTION_KINDS) == {"who_did", "who_said"}
    assert QUESTION_COSTS["who_did"] == 1.0
    assert QUESTION_COSTS["who_said"] == 1.0


def test_the_voice_questions_are_retired_and_stay_retired():
    """They were measured against the DM's own ear and lost: the diarization
    clusters are mixtures (six of seven held clips of different people), so "who
    is this voice?" had no true answer - and a wrong answer wrote a strong prior
    onto every moment of a cluster that was not one person.

    The set is kept explicit because sessions computed before the rollback still
    have these rows stored, and they are dropped when such a review is reloaded.
    """
    assert RETIRED_KINDS == {
        "who_is_voice",
        "new_person",
        "same_voice",
        "different_voice",
        "presence",
    }
    assert not (RETIRED_KINDS & set(QUESTION_KINDS))


def test_the_presence_questions_are_retired_too():
    """They were the replacement for the voice questions, and they lost on the
    DM's second session: a scene holds almost everybody almost always, so "was
    <name> there?" was answered "yes" by reflex and bought nothing, while taking
    20 of the 24 simulated slots away from every question that could have settled
    a moment.

    The stored rows are dropped on load for the same reason the voice ones are:
    a session computed while the kind existed must stop being asked it.
    """
    assert "presence" in RETIRED_KINDS


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


# --- the moment generators ---


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


def test_generate_collects_content_questions():
    utterances = {"u_00001": view(), "u_00002": view(ref="u_00002", kind="dialogue")}
    posteriors = {"u_00001": TORN, "u_00002": TORN}
    questions = generate(
        context=context(),
        utterances=utterances,
        posteriors=posteriors,
    )
    kinds = {q.kind for q in questions}
    assert "who_did" in kinds
    assert "who_said" in kinds
    # ...and nothing about a voice or a stretch: those kinds are retired
    assert not (kinds & RETIRED_KINDS)




def test_default_idk_probability_is_a_real_option_not_an_afterthought():
    assert 0.0 < DEFAULT_IDK_PROBABILITY < 0.5
