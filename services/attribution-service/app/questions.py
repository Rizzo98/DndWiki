"""The six questions the DM can actually answer.

The DM never sees a label, a cluster id or a probability. They see a moment from
their own session and a list of people, and the list always ends with "I don't
know" (docs/attribution-model.md S8).

Two design constraints do the real work here:

- **every question carries a hook** - the quote, the audio span, one line of
  "why this is being asked". A question the DM cannot answer from memory of the
  session is a bug in the generator, not a hard question (S8.3 rule 8), so the
  generator refuses to emit one without a concrete moment to point at;
- **suppression is explicit and testable** - the eight rules are a function, not
  a vibe, and each one has a test.

`same_voice` / `different_voice` are the cheapest questions in the system and,
per unit of DM effort, the highest-yield: a single binary audio comparison
resolves an entire structural decision that no content question can express.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from dnd_common.clustering import cosine

from app.channels import UNKNOWN
from app.statuses import StatusThresholds

#: Effort cost per kind, from S8.1. 0.7 for a binary audio A/B, 1.0 for a
#: roster pick, 1.3 when the DM has to listen to something and think.
QUESTION_COSTS: dict[str, float] = {
    "who_did": 1.0,
    "who_said": 1.0,
    "same_voice": 0.7,
    "different_voice": 0.7,
    "who_is_voice": 1.3,
    "new_person": 0.8,
}
QUESTION_KINDS: tuple[str, ...] = tuple(QUESTION_COSTS)

#: Always the last option offered. It is a real answer, not an escape hatch: it
#: records that the engine asked something the DM could not settle, which is
#: what feeds P_uninformative (S8.1, S9.3).
IDK_OPTION = "idk"
#: "Someone not in the campaign": a guest, a second DM, a passer-by.
STRANGER_OPTION = UNKNOWN
#: How likely the DM is to say "I don't know" when the model has no opinion.
DEFAULT_IDK_PROBABILITY = 0.15
#: A candidate is worth OFFERING from this posterior up (offering is cheap; the
#: suppression rules decide whether asking is worth it at all).
OFFER_THRESHOLD = 0.02
#: The DM option appears when the model gives the narrator at least this much
#: chance. The UI never asks "is this the DM?" when narration is decisive.
DM_OFFER_THRESHOLD = 0.05
#: Rule 4: below this stakes an utterance is filler and is never asked about.
MIN_QUESTION_STAKES = 0.15
#: Questions that target a voice identity rather than a moment. Their overlap is
#: measured on voices; content questions' overlap is measured on utterances.
STRUCTURAL_KINDS: frozenset[str] = frozenset(
    {"same_voice", "different_voice", "who_is_voice", "new_person"}
)
#: A roster member at or above this is 'plausible', which is what stops
#: who_is_voice from asking about a voice we can already place.
PLAUSIBLE_MATCH = 0.4


@dataclass(frozen=True)
class QuestionOption:
    """One thing the DM can pick."""

    key: str
    label: str
    why: str | None = None

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"key": self.key, "label": self.label}
        if self.why:
            payload["why"] = self.why
        return payload


@dataclass(frozen=True)
class UtteranceView:
    """The part of an utterance a question needs."""

    ref: str
    start: float
    end: float
    text: str = ""
    gist: str | None = None
    kind: str | None = None
    stakes: float = 0.5
    voice_id: str | None = None
    speech_sec: float | None = None


@dataclass(frozen=True)
class VoiceView:
    """A voice identity, as the question generator sees it."""

    id: str
    handle: str
    speech_sec: float
    purity: float | None = None
    utterance_refs: tuple[str, ...] = ()
    start: float | None = None
    end: float | None = None
    #: The identity's centroid, for the merge question. Real audio similarity,
    #: not a proxy: two identities are only proposed as one person when their
    #: centroids really are closer than the campaign's own same-voice distance.
    centroid: tuple[float, ...] = ()
    #: Mean posterior over candidates, pooled from the identity's utterances.
    posterior: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateQuestion:
    """A question that COULD be asked, before ranking decides if it should be."""

    kind: str
    prompt_text: str
    options: list[QuestionOption]
    target_utterances: list[str]
    target_voices: list[str]
    hook: dict[str, Any]
    cost: float
    mean_stakes: float
    #: Free-form generator note, kept for the audit trail and the tests.
    note: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "prompt_text": self.prompt_text,
            "options": [o.as_payload() for o in self.options],
            "target_utterances": list(self.target_utterances),
            "target_voices": list(self.target_voices),
            "hook": self.hook,
            "cost": self.cost,
            "mean_stakes": self.mean_stakes,
        }


@dataclass
class QuestionContext:
    """Everything the generators need: a snapshot of the current belief."""

    candidates: list[str]
    label_of: Mapping[str, str]
    dm_key: str | None = None
    language: str = "en"
    thresholds: StatusThresholds = field(default_factory=StatusThresholds)
    #: Voice identities already covered by a question that was asked or skipped.
    covered_voices: set[str] = field(default_factory=set)
    #: Utterance refs already covered (more than 50% overlap suppresses).
    covered_utterances: set[str] = field(default_factory=set)
    #: refs the review already resolved: never ask about these again.
    settled_utterances: set[str] = field(default_factory=set)
    #: Refs the ENGINE is confident about - auto_high, propagated or
    #: user_confirmed. Only those count as "the belief already answers it": a
    #: peaked posterior the statuses refuse to promote is not knowledge, it is
    #: exactly what the review exists to ask about (S7.1, S8.3 rule 1).
    confident_utterances: set[str] = field(default_factory=set)


def _quote(text: str, limit: int = 140) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def hook_of(view: UtteranceView) -> dict[str, Any]:
    """The quote plus the audio span: what makes a question answerable in seconds."""
    return {
        "quote": _quote(view.text),
        "audio": {"start": round(view.start, 3), "end": round(view.end, 3)},
        "ref": view.ref,
    }


def _minutes(seconds: float) -> str:
    return f"{max(1, round(seconds / 60.0))}"


def options_for(
    posterior: Mapping[str, float],
    *,
    context: QuestionContext,
    exclude: Sequence[str] = (),
) -> list[QuestionOption]:
    """The option list for one question, always ending with *I don't know*.

    Roster members in posterior order, then the DM when the narrator has a real
    chance, then "someone not in the campaign". A candidate is never offered at
    0.0001 - the list is short on purpose, because a long list is a slow
    question.
    """
    excluded = set(exclude)
    member_options: list[tuple[float, QuestionOption]] = []
    for candidate, probability in posterior.items():
        if candidate in excluded or candidate in {UNKNOWN, IDK_OPTION}:
            continue
        if candidate == context.dm_key:
            if probability < DM_OFFER_THRESHOLD:
                continue
        elif probability < OFFER_THRESHOLD:
            continue
        member_options.append(
            (
                probability,
                QuestionOption(
                    key=candidate,
                    label=context.label_of.get(candidate, candidate),
                    why=f"{probability:.0%} likely",
                ),
            )
        )
    member_options.sort(key=lambda item: (-item[0], item[1].key))

    options = [option for _, option in member_options]
    options.append(
        QuestionOption(
            key=STRANGER_OPTION,
            label="Someone not in the campaign",
            why="a guest, a second DM, or somebody visiting the table",
        )
    )
    options.append(
        QuestionOption(
            key=IDK_OPTION,
            label="I don't know",
            why="nobody is penalised for this answer",
        )
    )
    return options


# --- the six generators -----------------------------------------------------


def who_did(
    view: UtteranceView,
    posterior: Mapping[str, float],
    *,
    context: QuestionContext,
) -> CandidateQuestion | None:
    """"Who cast Fireball on the three goblins?"

    The main event question: a high-stakes action or decision with more than one
    candidate in play.
    """
    if view.kind not in {"action", "decision"}:
        return None
    subject = (view.gist or "").strip()
    if not subject:
        return None  # rule 8: no gist, nothing the DM can be shown
    if len([p for c, p in posterior.items() if c not in {UNKNOWN}]) < 2:
        return None
    return CandidateQuestion(
        kind="who_did",
        prompt_text=f"Who {_lower_first(subject)}?",
        options=options_for(posterior, context=context),
        target_utterances=[view.ref],
        target_voices=[view.voice_id] if view.voice_id else [],
        hook=hook_of(view),
        cost=QUESTION_COSTS["who_did"],
        mean_stakes=view.stakes,
        note="high-stakes action with more than one plausible actor",
    )


def who_said(
    view: UtteranceView,
    posterior: Mapping[str, float],
    *,
    context: QuestionContext,
    referenced: bool = False,
) -> CandidateQuestion | None:
    """"Who said they wanted to enter from the eastern passage?"

    A dialogue line the session later quotes, decides on or argues with - so the
    DM has a reason to remember it.
    """
    if view.kind not in {"dialogue", "decision"}:
        return None
    if not referenced and not (view.text or "").strip():
        return None
    if len([p for c, p in posterior.items() if c not in {UNKNOWN}]) < 2:
        return None
    quote = _quote(view.text)
    if not quote:
        return None
    return CandidateQuestion(
        kind="who_said",
        prompt_text=f"Who said: “{quote}”?",
        options=options_for(posterior, context=context),
        target_utterances=[view.ref],
        target_voices=[view.voice_id] if view.voice_id else [],
        hook=hook_of(view),
        cost=QUESTION_COSTS["who_said"],
        mean_stakes=view.stakes,
        note="quoted dialogue with more than one plausible speaker",
    )


def same_voice(
    voice: VoiceView,
    *,
    context: QuestionContext,
    clip_a: tuple[float, float],
    clip_b: tuple[float, float],
) -> CandidateQuestion:
    """Two short clips: "same person?" - the split decision, in one question."""
    return CandidateQuestion(
        kind="same_voice",
        prompt_text="Same person?",
        options=[
            QuestionOption(key="same", label="Same person"),
            QuestionOption(key="different", label="Different people"),
            QuestionOption(key=IDK_OPTION, label="I don't know"),
        ],
        target_utterances=list(voice.utterance_refs),
        target_voices=[voice.id],
        hook={
            "audio_a": {"start": round(clip_a[0], 3), "end": round(clip_a[1], 3)},
            "audio_b": {"start": round(clip_b[0], 3), "end": round(clip_b[1], 3)},
            "note": f"voice {voice.handle} may be more than one person",
        },
        cost=QUESTION_COSTS["same_voice"],
        mean_stakes=0.7,
        note="identity purity sits between the split and clean thresholds",
    )


def different_voice(
    left: VoiceView,
    right: VoiceView,
    *,
    context: QuestionContext,
    clip_a: tuple[float, float],
    clip_b: tuple[float, float],
) -> CandidateQuestion:
    """Two short clips from two identities: "same person?" - the merge decision."""
    return CandidateQuestion(
        kind="different_voice",
        prompt_text="Same person?",
        options=[
            QuestionOption(key="same", label="Same person"),
            QuestionOption(key="different", label="Different people"),
            QuestionOption(key=IDK_OPTION, label="I don't know"),
        ],
        target_utterances=list(left.utterance_refs) + list(right.utterance_refs),
        target_voices=[left.id, right.id],
        hook={
            "audio_a": {"start": round(clip_a[0], 3), "end": round(clip_a[1], 3)},
            "audio_b": {"start": round(clip_b[0], 3), "end": round(clip_b[1], 3)},
            "note": f"voices {left.handle} and {right.handle} sound alike",
        },
        cost=QUESTION_COSTS["different_voice"],
        mean_stakes=0.7,
        note="two identities closer than the campaign's own same-voice distance",
    )


def who_is_voice(
    voice: VoiceView, *, context: QuestionContext
) -> CandidateQuestion | None:
    """"This voice speaks for 12 minutes and we can't place it. Who is it?"

    The highest-yield question in the system, and the reason it exists: one
    answer teaches the engine a voice model that re-scores the whole session
    (S10.2).
    """
    if voice.speech_sec < 60.0:
        return None
    if not voice.utterance_refs:
        return None
    best_roster = max(
        (p for c, p in voice.posterior.items() if c != UNKNOWN), default=0.0
    )
    if best_roster >= PLAUSIBLE_MATCH:
        return None  # something plausible is already in play: ask about THAT
    if voice.posterior.get(UNKNOWN, 0.0) >= 0.6:
        # A stranger-dominated voice is new_person's question: it is more
        # specific, cheaper to answer, and asking both would be asking twice.
        return None
    return CandidateQuestion(
        kind="who_is_voice",
        prompt_text=(
            f"This voice speaks for {_minutes(voice.speech_sec)} minutes "
            "and we can't place it. Who is it?"
        ),
        options=options_for(voice.posterior, context=context),
        target_utterances=list(voice.utterance_refs),
        target_voices=[voice.id],
        hook={
            "audio": {
                "start": round(voice.start or 0.0, 3),
                "end": round(voice.end or 0.0, 3),
            },
            "note": f"voice {voice.handle}, {voice.speech_sec:.0f}s of speech",
        },
        cost=QUESTION_COSTS["who_is_voice"],
        mean_stakes=0.8,
        note="a long voice with no plausible roster match",
    )


def new_person(
    voice: VoiceView, *, context: QuestionContext
) -> CandidateQuestion | None:
    """"We found a voice that isn't anyone in the party. Is that right?"

    A guest, a second DM, a visitor. The answer creates a guest pseudo-member so
    their utterances are excluded from character pages rather than attributed to
    the nearest party member.
    """
    if voice.posterior.get(UNKNOWN, 0.0) < 0.6:
        return None
    return CandidateQuestion(
        kind="new_person",
        prompt_text="We found a voice that isn't anyone in the party. Is that right?",
        options=[
            QuestionOption(key=UNKNOWN, label="Yes - someone outside the party"),
            *[
                QuestionOption(key=c, label=context.label_of.get(c, c))
                for c in _roster_above(voice.posterior, context)
            ],
            QuestionOption(key=IDK_OPTION, label="I don't know"),
        ],
        target_utterances=list(voice.utterance_refs),
        target_voices=[voice.id],
        hook={
            "audio": {
                "start": round(voice.start or 0.0, 3),
                "end": round(voice.end or 0.0, 3),
            },
            "note": f"voice {voice.handle} matches nobody in the roster",
        },
        cost=QUESTION_COSTS["new_person"],
        mean_stakes=0.8,
        note="the best hypothesis is 'not in the campaign'",
    )


def _roster_above(posterior: Mapping[str, float], context: QuestionContext) -> list[str]:
    return sorted(
        (
            candidate
            for candidate, probability in posterior.items()
            if candidate not in {UNKNOWN, IDK_OPTION} and probability >= OFFER_THRESHOLD
        ),
        key=lambda c: (-posterior[c], c),
    )


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text else text


# --- suppression (S8.3) -----------------------------------------------------


def is_settled(
    posterior: Mapping[str, float],
    *,
    thresholds: StatusThresholds,
    confident: bool = False,
) -> bool:
    """Rule 1: the ENGINE already answers it, so asking would be theatre.

    A peaked posterior is not the same thing as a settled moment, and conflating
    the two is how this shipped: the mean-field fallback can push every utterance
    of a session onto one candidate at p=0.99 while the statuses - correctly -
    refuse to promote any of it, because a lone weak channel is not corroboration
    (S7.1). Rule 1 then read that manufactured peak as knowledge, suppressed
    every content question the session had, and left the DM with a single
    structural "who is this voice?" while the session was in fact unattributed.

    So the question is not "is the posterior peaked" but "will the wiki ACT on
    this?" - which is what 'confident' carries in from the verdicts.
    """
    if not confident:
        return False
    if not posterior:
        return True
    values = sorted(posterior.values(), reverse=True)
    top = values[0]
    second = values[1] if len(values) > 1 else 0.0
    return top >= thresholds.auto_high_pmin and (top - second) >= thresholds.auto_high_margin


def overlap_ratio(
    question: CandidateQuestion,
    *,
    covered_voices: set[str] | None = None,
    covered_utterances: set[str] | None = None,
) -> float:
    """Rule 3: how much of this question another question already covered.

    Split by kind on purpose. A structural question is "about" a voice, and a
    content question is "about" a moment: mixing the two would let a question
    about a voice suppress every utterance that voice speaks, which would empty
    the review after one answer.
    """
    if question.kind in STRUCTURAL_KINDS:
        targets = set(question.target_voices)
        covered = covered_voices or set()
    else:
        targets = set(question.target_utterances)
        covered = covered_utterances or set()
    if not targets:
        return 0.0
    return len(targets & covered) / len(targets)


def suppress(
    questions: Sequence[CandidateQuestion],
    *,
    context: QuestionContext,
    posteriors: Mapping[str, Mapping[str, float]],
) -> list[tuple[CandidateQuestion, str]]:
    """Apply the suppression rules; return (question, reason) pairs.

    Returning the reasons rather than dropping silently is deliberate: "why was
    I not asked about the Fireball?" is the first question a DM will have, and
    the audit trail has to be able to answer it.
    """
    kept: list[tuple[CandidateQuestion, str]] = []
    for question in questions:
        if question.kind in STRUCTURAL_KINDS:
            # a structural question about a voice already covered adds nothing
            if overlap_ratio(
                question, covered_voices=context.covered_voices
            ) >= 1.0:
                kept.append((question, "subject_already_covered"))
                continue
            kept.append((question, ""))
            continue

        ref = question.target_utterances[0] if question.target_utterances else ""
        posterior = posteriors.get(ref, {})
        if ref in context.settled_utterances:
            kept.append((question, "already_answered"))
            continue
        if is_settled(
            posterior,
            thresholds=context.thresholds,
            confident=ref in context.confident_utterances,
        ):
            kept.append((question, "posterior_already_answers_it"))
            continue
        if question.mean_stakes < MIN_QUESTION_STAKES:
            kept.append((question, "filler"))
            continue
        plausible = [
            candidate
            for candidate in posterior
            if candidate not in {UNKNOWN} and posterior[candidate] >= OFFER_THRESHOLD
        ]
        if len(plausible) < 2:
            kept.append((question, "degenerate_candidate_set"))
            continue
        if ref in context.covered_utterances:
            kept.append((question, "subject_already_covered"))
            continue
        if not question.hook.get("quote") and "audio" not in question.hook:
            # rule 8: a question with nothing concrete to point at is a bug
            kept.append((question, "no_concrete_moment"))
            continue
        kept.append((question, ""))
    return kept


def generate(
    *,
    context: QuestionContext,
    utterances: Mapping[str, UtteranceView],
    posteriors: Mapping[str, Mapping[str, float]],
    voices: Sequence[VoiceView] = (),
    referenced: Mapping[str, bool] | None = None,
    purity_split_threshold: float = 0.80,
    same_voice_similarity: float = 0.72,
    clips: Mapping[str, tuple[float, float]] | None = None,
) -> list[CandidateQuestion]:
    """Every question the current belief could justify, before ranking.

    Content questions come from the utterances; structural questions come from
    the voice identities (an impure one for a split, two similar ones for a
    merge). Nothing here decides ORDER - that is app.ranking's job - and nothing
    here talks to the database.
    """
    referenced = referenced or {}
    clips = clips or {}
    out: list[CandidateQuestion] = []

    for ref, view in utterances.items():
        posterior = posteriors.get(ref)
        if not posterior:
            continue
        did = who_did(view, posterior, context=context)
        if did is not None:
            out.append(did)
        said = who_said(
            view, posterior, context=context, referenced=referenced.get(ref, False)
        )
        if said is not None:
            out.append(said)

    for voice in voices:
        if voice.purity is not None and purity_split_threshold > voice.purity > 0.2:
            clip_a = clips.get(voice.id)
            clip_b = clips.get(f"{voice.id}#second")
            if clip_a and clip_b:
                out.append(same_voice(voice, context=context, clip_a=clip_a, clip_b=clip_b))
        if voice.posterior.get(UNKNOWN, 0.0) >= 0.6:
            person = new_person(voice, context=context)
            if person is not None:
                out.append(person)
        placed = who_is_voice(voice, context=context)
        if placed is not None:
            out.append(placed)

    for index, left in enumerate(voices):
        for right in voices[index + 1 :]:
            if _centroid_similarity(left, right) < same_voice_similarity:
                continue
            clip_a = clips.get(left.id)
            clip_b = clips.get(right.id)
            if clip_a and clip_b:
                out.append(
                    different_voice(
                        left, right, context=context, clip_a=clip_a, clip_b=clip_b
                    )
                )

    return [q for q, reason in suppress(out, context=context, posteriors=posteriors) if not reason]


def _centroid_similarity(left: VoiceView, right: VoiceView) -> float:
    """Audio similarity of two identities, from their centroids.

    An identity without a centroid (no embedded observations) scores 0.0 and is
    never proposed for a merge: "we have no audio for it" must not read as "it
    sounds like everybody".
    """
    if not left.centroid or not right.centroid:
        return 0.0
    if len(left.centroid) != len(right.centroid):
        return 0.0
    return float(cosine(left.centroid, right.centroid))
