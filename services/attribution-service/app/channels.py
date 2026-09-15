"""The evidence channels: every signal, as a log likelihood ratio.

This module is the intellectual core of the engine and is deliberately pure: one
function per channel, each taking plain data and returning
`{candidate_key: log_lr}`, each independently unit-tested with hand-written
fixtures. No database, no model, no queue. If a channel cannot be tested on its
own, it is doing too much.

Two rules that hold for every channel (docs/attribution-model.md S4.1, S4.2):

1. **Nothing is a hard veto.** There is no `-inf` anywhere except an explicit
   DM statement, because all of our "impossible" knowledge is itself inferred.
   A capability constraint that is wrong must be able to lose to voice evidence.
2. **A single channel never yields high confidence.** The status rules (S7.1)
   enforce that downstream; the channels simply must not pretend to be
   decisive on their own.

Log LR convention: positive favours the candidate, negative disfavours it, and
a candidate missing from the returned dict is at 0.0 (no opinion).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

#: Every channel the engine knows, in the order they are evaluated. The names
#: are persisted on utterance_evidence.channel, so they are a contract.
CHANNELS: tuple[str, ...] = (
    "voice",
    "self_name",
    "capability",
    "address",
    "narration",
    "ooc",
    "roll",
    "identity_note",
    "voice_mode",
    "dm_prior",
    "user_answer",
)

#: Channels that can corroborate an auto_high verdict on their own (S7.1). The
#: voice channel has its own, higher bar; dm_prior and roll cannot corroborate
#: anything because they are priors, not observations.
CORROBORATING_CHANNELS: frozenset[str] = frozenset(
    {
        "self_name",
        "capability",
        "address",
        "narration",
        "ooc",
        "identity_note",
        "voice_mode",
    }
)

#: The candidate key of "nobody in the roster": a guest, or a voice we cannot
#: place. Always present, never excluded - a session with a stranger in it must
#: be representable.
UNKNOWN = "unknown"

# --- per-channel weights (S4.2) --------------------------------------------
W_SELF_NAME_FIRST = 3.5
W_SELF_NAME_THIRD = 2.0
W_CAPABILITY = 1.2
CAP_CAPABILITY = 3.0
W_ADDRESS = 2.5
W_ADDRESSER = -0.4
W_NARRATION = 2.2
W_OOC_DM = 0.8
W_OOC_EVERYONE = 0.3
W_ROLL = 1.0
W_VOICE_MODE_DM = 2.0
W_DM_PRIOR = 0.5
CAP_DM_PRIOR = 1.5
#: How hard the "somebody else probably said it" counterweight pushes down the
#: candidates a channel is NOT pointing at.
SPREAD_CAP = 0.5
#: An identity_note multiplies a member's prior by this while it applies.
ABSENT_PRIOR_FACTOR = 0.05


class Calibration(Protocol):
    """Turns a raw cosine into a log likelihood ratio (S7.2)."""

    def log_lr(self, cosine: float, *, quality: float = 1.0) -> float: ...


@dataclass(frozen=True)
class ChannelOutput:
    """One channel's contribution to one utterance."""

    channel: str
    log_lr: dict[str, float]
    payload: dict[str, Any] = field(default_factory=dict)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "log_lr": {k: round(v, 6) for k, v in self.log_lr.items()},
            "payload": self.payload,
        }


@dataclass(frozen=True)
class Claim:
    """A self-identification or third-person mention found by the evidence pass."""

    type: str  # self_character | other_character | self_player
    value: str
    #: How strongly the pass believes the claim (0..1). A hedge ("I think I'm
    #: playing Thorin tonight") must not weigh like a plain statement.
    strength: float = 1.0

    @property
    def first_person(self) -> bool:
        return self.type == "self_character"


@dataclass(frozen=True)
class UtteranceFeatures:
    """Everything the channels need to know about one utterance."""

    ordinal: int
    start: float
    end: float
    text: str = ""
    kind: str | None = None
    voice_mode: str | None = None
    gist: str | None = None
    stakes: float = 0.5
    capability_reqs: tuple[str, ...] = ()
    claimed_names: tuple[str, ...] = ()
    addressed_names: tuple[str, ...] = ()
    claims: tuple[Claim, ...] = ()
    voice_quality: float | None = None
    gap_before_sec: float | None = None
    #: Set when this utterance is the reply the previous one called for.
    roll_owner: str | None = None
    #: Member keys excluded for a window by an identity_note.
    excluded: tuple[str, ...] = ()


def _spread(total: float, candidates: Sequence[str], benefactor: str) -> dict[str, float]:
    """Push down everyone the channel is not pointing at.

    Without this, a channel that boosts the DM by +2.2 says nothing about the
    other four candidates, and the posterior stays flatter than the evidence
    warrants. The counterweight is deliberately soft and capped: it nudges, it
    never vetoes (S4.1).
    """
    others = [c for c in candidates if c != benefactor]
    if not others:
        return {}
    each = min(SPREAD_CAP, abs(total) / len(others))
    return {c: -each for c in others}


# --- voice ------------------------------------------------------------------


def voice(
    scores: Mapping[str, float],
    *,
    calibration: Calibration,
    quality: float | None = None,
) -> ChannelOutput:
    """Calibrated cosine -> log LR for every candidate with a voice model.

    The threshold 0.75 that used to live here as a policy is gone (S12.1): it is
    now one point on the fitted score-to-LLR curve, and the curve is what turns a
    cosine into a statement about evidence rather than about a decision.
    """
    quality = 1.0 if quality is None else max(0.0, min(1.0, quality))
    log_lr: dict[str, float] = {}
    for candidate, cosine in scores.items():
        log_lr[candidate] = float(calibration.log_lr(float(cosine), quality=quality))
    return ChannelOutput(
        channel="voice",
        log_lr=log_lr,
        payload={
            "scores": {k: round(float(v), 4) for k, v in scores.items()},
            "quality": round(quality, 4),
        },
    )


# --- self_name --------------------------------------------------------------


def self_name(
    features: UtteranceFeatures,
    *,
    name_to_candidate: Mapping[str, str],
    candidates: Sequence[str],
    dm_key: str | None = None,
) -> ChannelOutput:
    """The speaker naming their own character.

    First person ("Aramil casts Shield", "I, Aramil, ...") is near-decisive;
    third-person self-narration ("Aramil moves to the door") is very common at
    real tables and strong but weaker. The strength the pass attached to the
    claim scales both.
    """
    log_lr: dict[str, float] = {}
    payload: dict[str, Any] = {"claims": []}
    for claim in features.claims:
        target = name_to_candidate.get(claim.value.strip().lower())
        if target is None or target not in candidates:
            continue
        base = W_SELF_NAME_FIRST if claim.first_person else W_SELF_NAME_THIRD
        weight = base * max(0.0, min(1.0, claim.strength))
        log_lr[target] = log_lr.get(target, 0.0) + weight
        payload["claims"].append(
            {
                "value": claim.value,
                "type": claim.type,
                "strength": round(claim.strength, 3),
                "weight": round(weight, 3),
            }
        )
    if not log_lr:
        return ChannelOutput(channel="self_name", log_lr={}, payload=payload)

    # A self-naming claim also implies "not the others": the DM does not play a
    # party character, so the DM is pushed down hardest.
    best = max(log_lr.values())
    for candidate, penalty in _spread(best, candidates, benefactor="").items():
        log_lr.setdefault(candidate, penalty)
    if dm_key and dm_key in candidates:
        log_lr.setdefault(dm_key, -SPREAD_CAP)
    return ChannelOutput(channel="self_name", log_lr=log_lr, payload=payload)


# --- capability -------------------------------------------------------------


def capability(
    requirements: Sequence[str],
    *,
    member_capabilities: Mapping[str, Sequence[str]],
    candidates: Sequence[str],
    dm_key: str | None = None,
    authoritative: bool = False,
    weight: float = W_CAPABILITY,
    cap: float = CAP_CAPABILITY,
) -> ChannelOutput:
    """A requirement only some members can satisfy ("I cast Fireball").

    `compat` is +1 for a member known to have the capability, 0 for unknown and
    -1 ONLY when the campaign has a DM-authored capability sheet: without one
    our "only the wizard can cast Fireball" is itself an inference, and an
    inference must not veto (S16).
    """
    if not requirements:
        return ChannelOutput(channel="capability", log_lr={}, payload={})

    def key_of(candidate: str) -> str:
        return candidate.split(":", 1)[1] if ":" in candidate else candidate

    def caps_of(candidate: str) -> set[str]:
        return set(member_capabilities.get(key_of(candidate), ()))

    log_lr: dict[str, float] = {}
    detail: dict[str, Any] = {"requirements": list(requirements), "authoritative": authoritative}
    for candidate in candidates:
        if candidate == UNKNOWN:
            continue
        if dm_key and candidate == dm_key:
            # The DM does not cast the party's spells; a small nudge only.
            log_lr[candidate] = -0.4
            continue
        caps = caps_of(candidate)
        total = 0.0
        for requirement in requirements:
            if requirement in caps:
                compat = 1.0
            elif authoritative and caps:
                compat = -1.0
            else:
                compat = 0.0
            total += weight * compat
        log_lr[candidate] = max(-cap, min(cap, total))
    return ChannelOutput(channel="capability", log_lr=log_lr, payload=detail)


# --- address ----------------------------------------------------------------


def address(
    addressed: Sequence[str],
    *,
    name_to_candidate: Mapping[str, str],
    candidates: Sequence[str],
    addresser: str | None = None,
    weight: float = W_ADDRESS,
) -> ChannelOutput:
    """Somebody was called by name; the reply is theirs.

    Applied to the utterance that FOLLOWS the one containing the name, within
    `handoff_sec` (the caller decides whether the gap qualifies). The addresser
    is pushed slightly down: at a table, the person who asked is usually not the
    person who answers.
    """
    log_lr: dict[str, float] = {}
    payload: dict[str, Any] = {"addressed": []}
    for name in addressed:
        target = name_to_candidate.get(name.strip().lower())
        if target is None or target not in candidates:
            continue
        log_lr[target] = log_lr.get(target, 0.0) + weight
        payload["addressed"].append({"name": name, "candidate": target})
    if not log_lr:
        return ChannelOutput(channel="address", log_lr={}, payload=payload)
    if addresser and addresser in candidates:
        log_lr[addresser] = log_lr.get(addresser, 0.0) + W_ADDRESSER
    return ChannelOutput(channel="address", log_lr=log_lr, payload=payload)


# --- narration / ooc / voice_mode ------------------------------------------


def narration(
    features: UtteranceFeatures,
    *,
    candidates: Sequence[str],
    dm_key: str | None,
    weight: float = W_NARRATION,
) -> ChannelOutput:
    """Second-person scene description, NPC voice, rules adjudication, framing."""
    if not dm_key or dm_key not in candidates:
        return ChannelOutput(channel="narration", log_lr={}, payload={})
    if features.voice_mode not in {"narration", "npc_dialogue"}:
        return ChannelOutput(channel="narration", log_lr={}, payload={})
    log_lr = {dm_key: weight}
    log_lr.update(_spread(weight, candidates, benefactor=dm_key))
    return ChannelOutput(
        channel="narration",
        log_lr=log_lr,
        payload={"voice_mode": features.voice_mode},
    )


def out_of_world(
    features: UtteranceFeatures,
    *,
    candidates: Sequence[str],
    dm_key: str | None,
) -> ChannelOutput:
    """Table meta-talk: snacks, scheduling, rules lawyering.

    Low information on purpose. Everybody talks about the pizza, so the channel
    must not pretend to know who did.
    """
    if features.kind != "meta":
        return ChannelOutput(channel="ooc", log_lr={}, payload={})
    log_lr = {candidate: W_OOC_EVERYONE for candidate in candidates if candidate != UNKNOWN}
    if dm_key and dm_key in candidates:
        log_lr[dm_key] = W_OOC_DM
    return ChannelOutput(channel="ooc", log_lr=log_lr, payload={"kind": features.kind})


def voice_mode(
    features: UtteranceFeatures,
    *,
    candidates: Sequence[str],
    dm_key: str | None,
    weight: float = W_VOICE_MODE_DM,
) -> ChannelOutput:
    """An NPC speaking is the DM, in all but pathological cases.

    It does NOT create an NPC entity: naming the innkeeper is a content-service
    concern, and the attribution only needs to know whose MOUTH it was (S5.5).
    """
    if features.voice_mode != "npc_dialogue" or not dm_key or dm_key not in candidates:
        return ChannelOutput(channel="voice_mode", log_lr={}, payload={})
    log_lr = {dm_key: weight}
    log_lr.update(_spread(weight, candidates, benefactor=dm_key))
    return ChannelOutput(
        channel="voice_mode", log_lr=log_lr, payload={"voice_mode": features.voice_mode}
    )


# --- roll -------------------------------------------------------------------


def roll(
    features: UtteranceFeatures,
    *,
    candidates: Sequence[str],
    weight: float = W_ROLL,
) -> ChannelOutput:
    """"I rolled a 19 plus five" confirms the same speaker as the last action.

    Weak on its own, which is why it is capped at +1.0 and why it is not a
    corroborating channel: a dice result tells you who is still talking, not who
    started talking.
    """
    if not features.roll_owner or features.roll_owner not in candidates:
        return ChannelOutput(channel="roll", log_lr={}, payload={})
    log_lr = {features.roll_owner: weight}
    log_lr.update(_spread(weight, candidates, benefactor=features.roll_owner))
    return ChannelOutput(
        channel="roll", log_lr=log_lr, payload={"roll_owner": features.roll_owner}
    )


# --- identity_note ----------------------------------------------------------


def identity_note(
    features: UtteranceFeatures,
    *,
    candidates: Sequence[str],
) -> ChannelOutput:
    """In-session facts that change the CANDIDATE SET, not the odds.

    "Keth's player is in the bathroom", "Thorin isn't here yet": for a window,
    that member is very unlikely to be speaking. Expressed as a large negative
    log LR rather than a veto, because a note can be misheard, mistimed or about
    somebody who came back early.
    """
    if not features.excluded:
        return ChannelOutput(channel="identity_note", log_lr={}, payload={})
    penalty = math.log(ABSENT_PRIOR_FACTOR)  # ~ -3.0
    log_lr = {
        candidate: penalty for candidate in features.excluded if candidate in candidates
    }
    if not log_lr:
        return ChannelOutput(channel="identity_note", log_lr={}, payload={})
    return ChannelOutput(
        channel="identity_note",
        log_lr=log_lr,
        payload={"excluded": list(features.excluded)},
    )


# --- dm_prior ---------------------------------------------------------------


def dm_prior(
    speech_share: Mapping[str, float],
    *,
    candidates: Sequence[str],
    weight: float = W_DM_PRIOR,
    cap: float = CAP_DM_PRIOR,
) -> ChannelOutput:
    """The DM speaks the most and starts scenes.

    A prior, not an observation: it is the same for every utterance of the
    session, so it moves the starting point without ever deciding anything.
    """
    shares = {
        candidate: float(speech_share.get(candidate, 0.0))
        for candidate in candidates
        if candidate != UNKNOWN
    }
    total = sum(shares.values())
    if total <= 0 or not shares:
        return ChannelOutput(channel="dm_prior", log_lr={}, payload={})
    mean = total / len(shares)
    log_lr = {}
    for candidate, share in shares.items():
        if share <= 0:
            log_lr[candidate] = -cap
            continue
        value = weight * math.log(share / mean)
        log_lr[candidate] = max(-cap, min(cap, value))
    return ChannelOutput(
        channel="dm_prior",
        log_lr=log_lr,
        payload={"speech_share": {k: round(v, 4) for k, v in shares.items()}},
    )


# --- user_answer ------------------------------------------------------------


def user_answer(
    *, candidate: str, candidates: Sequence[str], weight: float = 8.0
) -> ChannelOutput:
    """The DM answered a question about exactly this utterance.

    A near-clamp rather than an absolute one: the DM can be right about a guest
    using someone else's headset, and the contradiction path (S10.7) still wants
    to record that the voice evidence disagreed. A finite weight keeps the
    posterior computable and the disagreement visible.
    """
    log_lr = {candidate: weight}
    log_lr.update(_spread(weight, candidates, benefactor=candidate))
    return ChannelOutput(
        channel="user_answer", log_lr=log_lr, payload={"answer": candidate}
    )


# --- channel registry -------------------------------------------------------


def corroboration(
    outputs: Sequence[ChannelOutput], *, best: str, min_lr: float = 1.5
) -> tuple[str, float] | None:
    """The strongest non-voice channel that supports 'best' on its own.

    This is what makes `auto_high` mean something (S7.1): a verdict needs a
    SECOND, independent reason, so a lone cosine - however high - can never be
    promoted to a fact that reaches the wiki.
    """
    strongest: tuple[str, float] | None = None
    for output in outputs:
        if output.channel not in CORROBORATING_CHANNELS:
            continue
        value = output.log_lr.get(best)
        if value is None or value < min_lr:
            continue
        if strongest is None or value > strongest[1]:
            strongest = (output.channel, value)
    return strongest


def voice_strength(outputs: Sequence[ChannelOutput], *, best: str) -> float:
    """The voice channel's log LR for 'best' (0.0 when it had no opinion)."""
    for output in outputs:
        if output.channel == "voice":
            return float(output.log_lr.get(best, 0.0))
    return 0.0
