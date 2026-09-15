"""The five attribution statuses and the rule that assigns them.

docs/attribution-model.md S7.1. The rule is the safety property of the whole
design, so it lives in its own dependency-free module rather than inside the
inference or the ORM:

  user_confirmed  the DM answered a question covering this utterance
  auto_high       p_max >= 0.90 AND margin >= 0.50 AND a SECOND channel agrees
  propagated      p_max >= 0.95 AND margin >= 0.75 (inferred, not observed)
  auto_low        p_max >= 0.60
  unresolved      anything else

The corroboration requirement in `auto_high` is the whole point: a single
channel never yields high confidence. Today a bare cosine of 0.76 does, which is
exactly how a silent error reaches the wiki.

`propagated` is STRICTER than `auto_high` on purpose. It generalises from an
answer instead of observing the moment, so if the DM later says a propagated
attribution is wrong, the blast radius is the propagation rule and the engine
can invalidate the rule instead of distrusting the DM.

The order of ATTRIBUTION_STATUSES is also the monotonicity rule (S7.3): a
downstream consumer may never promote an utterance to a status it was not
assigned.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

ATTRIBUTION_STATUSES: tuple[str, ...] = (
    "unresolved",
    "auto_low",
    "auto_high",
    "propagated",
    "user_confirmed",
)
STATUS_RANK: dict[str, int] = {status: i for i, status in enumerate(ATTRIBUTION_STATUSES)}

#: Statuses the wiki gate accepts as the source of a character-level fact.
CONFIDENT_STATUSES: frozenset[str] = frozenset(
    {"user_confirmed", "auto_high", "propagated"}
)
#: Statuses the review is allowed to leave alone.
SETTLED_STATUSES: frozenset[str] = CONFIDENT_STATUSES | {"auto_low"}


def rank(status: str) -> int:
    """Position of a status in the monotonicity order (-1 for an unknown value)."""
    return STATUS_RANK.get(status, -1)


def at_least(status: str, floor: str) -> bool:
    """Whether 'status' is at least as strong as 'floor'."""
    return rank(status) >= rank(floor)


def is_confident(status: str) -> bool:
    return status in CONFIDENT_STATUSES


@dataclass(frozen=True)
class StatusThresholds:
    """The knobs from S15.5, bundled so the rule is testable without settings."""

    auto_high_pmin: float = 0.90
    auto_high_margin: float = 0.50
    auto_high_min_channel_lr: float = 1.5
    auto_high_min_voice_lr: float = 3.0
    auto_low_pmin: float = 0.60
    propagated_pmin: float = 0.95
    propagated_margin: float = 0.75


@dataclass(frozen=True)
class Verdict:
    """The stored classification of one utterance."""

    status: str
    confidence: float
    margin: float
    entropy: float
    decided_by: str
    corroborated_by: str | None = None

    @property
    def confident(self) -> bool:
        return self.status in CONFIDENT_STATUSES


def classify(
    *,
    p_max: float,
    margin: float,
    entropy: float,
    corroborating_channel: str | None = None,
    voice_lr: float = 0.0,
    decided_by: str = "engine",
    answered: bool = False,
    propagated: bool = False,
    thresholds: StatusThresholds | None = None,
) -> Verdict:
    """Assign one of the five statuses to an utterance.

    Order matters: an explicit answer outranks everything; a propagated
    attribution outranks an observed one only when it clears the HIGHER bar; and
    `auto_high` needs a second, independent reason.
    """
    t = thresholds or StatusThresholds()

    if answered:
        return Verdict(
            "user_confirmed", p_max, margin, entropy, decided_by, corroborating_channel
        )

    if propagated:
        if p_max >= t.propagated_pmin and margin >= t.propagated_margin:
            return Verdict(
                "propagated", p_max, margin, entropy, decided_by, corroborating_channel
            )
        # A propagated attribution that misses its bar must NOT fall back to
        # auto_high. auto_high says "we observed this moment and we are sure";
        # this utterance was INFERRED from somebody else's answer, so promoting
        # it would overstate the evidence and destroy the audit trail that makes
        # a bad propagation rule invalidatable without distrusting the DM.
        if p_max >= t.auto_low_pmin:
            return Verdict(
                "auto_low", p_max, margin, entropy, decided_by, corroborating_channel
            )
        return Verdict(
            "unresolved", p_max, margin, entropy, decided_by, corroborating_channel
        )

    strong_voice = voice_lr >= t.auto_high_min_voice_lr
    corroborated = corroborating_channel is not None
    if (
        p_max >= t.auto_high_pmin
        and margin >= t.auto_high_margin
        and (corroborated or strong_voice)
    ):
        return Verdict(
            "auto_high",
            p_max,
            margin,
            entropy,
            decided_by,
            corroborating_channel if corroborated else "voice",
        )

    if p_max >= t.auto_low_pmin:
        return Verdict("auto_low", p_max, margin, entropy, decided_by, corroborating_channel)

    return Verdict("unresolved", p_max, margin, entropy, decided_by, corroborating_channel)


def coverage(
    rows: Sequence[Mapping[str, Any]],
    *,
    stakes_key: str = "stakes",
    seconds_key: str = "speech_sec",
) -> float:
    """Stakes-weighted speech share that is confidently attributed (S11.1).

    Defined on speech SECONDS weighted by stakes, not on label counts: "3 of 5
    labels confirmed" says nothing about how much of a four-hour session is
    actually understood, and it is the number the DM was shown before.
    """
    total = 0.0
    covered = 0.0
    for row in rows:
        weight = float(row.get(stakes_key, 0.5)) * float(row.get(seconds_key, 0.0))
        total += weight
        if is_confident(str(row.get("status", "unresolved"))):
            covered += weight
    if total <= 0:
        return 1.0
    return covered / total


def unresolved_stakes(rows: Sequence[Mapping[str, Any]], *, stakes_key: str = "stakes") -> float:
    """Share of stakes mass NOT confidently attributed: the stopping rule's input.

    "Not confident", not "the unresolved status". The stopping rule exists to
    decide whether the wiki can be built, and the wiki gate only accepts the
    CONFIDENT statuses - so a moment the engine merely leans towards
    (auto_low) is exactly as unattributed as one it cannot call at all.

    Counting only the lowest status made the two disagree in the worst possible
    direction: a session where every single moment was auto_low reported zero
    unresolved stakes, so the review declared itself finished and asked the DM
    NOTHING, while coverage said three quarters of the session was still
    unattributed. The DM was never given the chance to settle it.
    """
    total = sum(float(r.get(stakes_key, 0.5)) for r in rows)
    if total <= 0:
        return 0.0
    open_mass = sum(
        float(r.get(stakes_key, 0.5))
        for r in rows
        if not is_confident(str(r.get("status", "unresolved")))
    )
    return open_mass / total
