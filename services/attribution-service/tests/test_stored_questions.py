"""Stored questions must come back as the questions that were stored.

The rows key their targets by the tables' own ids (utterance UUIDs, voice
UUIDs); the engine works in utterance refs ("u_00001") and voice handles ("V1").
Returning ids where the engine expects refs does not FAIL - it makes every
question inert, because the answer is written onto a key no node has. The gain
of answering such a question is exactly zero, so the review plans nothing, and
the DM is asked nothing at all. That is how it shipped.
"""

import uuid

from app.services.review import rebuild_questions

SESSION = uuid.uuid4()
UTTERANCE_A = uuid.uuid4()
UTTERANCE_B = uuid.uuid4()
VOICE_1 = uuid.uuid4()


class _Row:
    """A review_questions row, as the ORM hands it over."""

    def __init__(self, **overrides):
        self.id = uuid.uuid4()
        self.kind = "who_did"
        self.prompt_text = "Who cast Fireball?"
        self.options = [
            {"key": "member:a", "label": "Alice"},
            {"key": "member:b", "label": "Bob"},
            {"key": "idk", "label": "I don't know"},
        ]
        self.target_utterances = [UTTERANCE_A]
        self.target_voices = [VOICE_1]
        self.hook = {"quote": "I cast Fireball"}
        self.cost = 1.0
        for key, value in overrides.items():
            setattr(self, key, value)


REF_OF = {UTTERANCE_A: "u_00001", UTTERANCE_B: "u_00002"}
VOICE_OF = {VOICE_1: "V1"}


def test_stored_ids_become_the_refs_the_engine_uses():
    (question,) = rebuild_questions(
        [_Row()], ref_of=REF_OF, voice_of=VOICE_OF
    )
    assert question.target_utterances == ["u_00001"]
    assert question.target_voices == ["V1"]


def test_a_question_with_nothing_resolvable_is_dropped():
    """Not kept with an unresolvable target: it would be unanswerable for the
    DM and inert for the ranking at the same time."""
    unknown = _Row(target_utterances=[uuid.uuid4()], target_voices=[uuid.uuid4()])
    assert rebuild_questions([unknown], ref_of=REF_OF, voice_of=VOICE_OF) == []


def test_a_voice_question_survives_a_missing_utterance():
    """Structural questions target voices; their utterance list may be stale
    without making the question unanswerable."""
    question_row = _Row(target_utterances=[uuid.uuid4()], target_voices=[VOICE_1])
    (question,) = rebuild_questions([question_row], ref_of=REF_OF, voice_of=VOICE_OF)
    assert question.target_voices == ["V1"]


def test_mean_stakes_come_from_the_moments_not_a_constant():
    (question,) = rebuild_questions(
        [_Row(target_utterances=[UTTERANCE_A, UTTERANCE_B])],
        ref_of=REF_OF,
        voice_of=VOICE_OF,
        stakes={"u_00001": 0.9, "u_00002": 0.3},
    )
    assert question.mean_stakes == 0.6


def test_the_prompt_options_and_hook_survive_the_round_trip():
    (question,) = rebuild_questions([_Row()], ref_of=REF_OF, voice_of=VOICE_OF)
    assert question.prompt_text == "Who cast Fireball?"
    assert [option.key for option in question.options] == ["member:a", "member:b", "idk"]
    assert question.hook["quote"] == "I cast Fireball"
    assert question.cost == 1.0
