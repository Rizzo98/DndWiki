"""Calibration: turning a cosine into a statement about evidence.

The old pipeline decided with a constant: a pooled cosine above 0.75 became a
name and everything else became a question for the DM. That constant cannot know
how similar two samples of THIS person's voice are, and it cannot know how good
the audio was (docs/attribution-model.md S7.2, S12.1).

After the redesign the threshold is one point on a FITTED curve:

    log LR(s) = log f_same(s) - log f_diff(s)

where f_same and f_diff are estimated from the campaign's own labelled history.
Cold start - no labelled audio yet - uses a logistic ramp whose slope and
midpoint come from the dispersion of the campaign's enrollment samples.

Everything here is pure NumPy: no database, no LLM. The persistence lives in
attribution_calibrations, and `Calibration.as_params` is what goes in it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.channels import CHANNELS
from app.inference import softmax

#: The voice channel's log LR is clipped to this range. A cosine of 0.99 is not
#: infinitely more informative than 0.95, and an unclipped KDE happily returns
#: +40 for a point in the far tail, which would let one channel dominate the
#: whole posterior.
LLR_CLIP = 6.0
#: Cold-start logistic ramp: log LR = slope * (s - midpoint).
COLD_START_SLOPE = 12.0
COLD_START_MIDPOINT = 0.55
#: KDE bandwidth floor, in cosine units. Below this the density estimate is a
#: row of spikes and the LL-R is meaningless.
MIN_BANDWIDTH = 0.02
#: Channel weights are fitted but never allowed outside this range: a negative
#: weight would invert a channel's meaning, and >2 would let one channel
#: override the fusion.
MIN_CHANNEL_WEIGHT = 0.0
MAX_CHANNEL_WEIGHT = 2.0


def _stable_logsumexp(values: np.ndarray) -> np.ndarray:
    if values.size == 0:
        return np.array(float("-inf"))
    maximum = np.max(values)
    if not np.isfinite(maximum):
        return np.array(maximum)
    return maximum + np.log(np.sum(np.exp(values - maximum)))


def _kde_logpdf(x: float, samples: np.ndarray, bandwidth: float) -> float:
    """Gaussian KDE log-density at x (empty sample -> -inf)."""
    if samples.size == 0:
        return float("-inf")
    z = (x - samples) / bandwidth
    return float(
        _stable_logsumexp(-0.5 * z * z)
        - math.log(bandwidth)
        - 0.5 * math.log(2 * math.pi)
        - math.log(samples.size)
    )


def silverman_bandwidth(samples: Sequence[float]) -> float:
    """Silverman's rule of thumb, floored so small samples stay smooth."""
    values = np.asarray(samples, dtype=np.float64)
    if values.size < 2:
        return MIN_BANDWIDTH
    std = float(np.std(values, ddof=1))
    spread = std if std > 0 else float(np.ptp(values)) or MIN_BANDWIDTH
    bandwidth = 1.06 * spread * values.size ** (-1.0 / 5.0)
    return max(MIN_BANDWIDTH, bandwidth)


@dataclass(frozen=True)
class CalibrationParams:
    """A fitted (or cold-start) score -> log LR map for the voice channel."""

    kind: str = "cold_start"
    slope: float = COLD_START_SLOPE
    midpoint: float = COLD_START_MIDPOINT
    same: tuple[float, ...] = ()
    different: tuple[float, ...] = ()
    bandwidth: float = MIN_BANDWIDTH
    n_labeled: int = 0
    clip: float = LLR_CLIP

    @classmethod
    def cold_start(cls) -> CalibrationParams:
        return cls()

    @property
    def fitted(self) -> bool:
        return self.kind == "kde"

    def raw_log_lr(self, cosine: float) -> float:
        if not self.fitted:
            return self.slope * (float(cosine) - self.midpoint)
        same = np.asarray(self.same, dtype=np.float64)
        different = np.asarray(self.different, dtype=np.float64)
        log_same = _kde_logpdf(cosine, same, self.bandwidth)
        log_diff = _kde_logpdf(cosine, different, self.bandwidth)
        if not math.isfinite(log_same):
            return -self.clip
        if not math.isfinite(log_diff):
            return self.clip
        return log_same - log_diff

    def log_lr(self, cosine: float, *, quality: float = 1.0) -> float:
        """The voice channel's log LR, clipped and scaled by audio quality.

        Quality is applied here AND inside alpha_u. That is deliberate: poor
        audio should lose twice - once as weaker evidence for this utterance and
        once as weaker trust in the cluster's vote - and the safe direction for a
        double penalty is "we are less sure", never "we are wrongly sure".
        """
        raw = self.raw_log_lr(cosine)
        clipped = max(-self.clip, min(self.clip, raw))
        return float(clipped * max(0.0, min(1.0, quality)))

    def as_params(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "slope": round(self.slope, 6),
            "midpoint": round(self.midpoint, 6),
            "same": [round(v, 6) for v in self.same],
            "different": [round(v, 6) for v in self.different],
            "bandwidth": round(self.bandwidth, 6),
            "n_labeled": self.n_labeled,
            "clip": self.clip,
        }

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> CalibrationParams:
        return cls(
            kind=str(params.get("kind", "cold_start")),
            slope=float(params.get("slope", COLD_START_SLOPE)),
            midpoint=float(params.get("midpoint", COLD_START_MIDPOINT)),
            same=tuple(float(v) for v in params.get("same", ())),
            different=tuple(float(v) for v in params.get("different", ())),
            bandwidth=float(params.get("bandwidth", MIN_BANDWIDTH)),
            n_labeled=int(params.get("n_labeled", 0)),
            clip=float(params.get("clip", LLR_CLIP)),
        )


def fit_score_to_llr(
    same: Sequence[float],
    different: Sequence[float],
    *,
    min_samples: int = 30,
) -> CalibrationParams:
    """Fit f_same / f_diff from labelled pair similarities.

    'same' are cosine similarities of two observations known to be one speaker,
    'different' of two known to be different speakers. Below 'min_samples' per
    side there is not enough data for a density estimate and the cold-start ramp
    is returned instead: a badly fitted KDE is worse than an honest prior.
    """
    same_values = [float(v) for v in same]
    diff_values = [float(v) for v in different]
    if len(same_values) < min_samples or len(diff_values) < min_samples:
        return CalibrationParams.cold_start()

    bandwidth = max(
        MIN_BANDWIDTH,
        (silverman_bandwidth(same_values) + silverman_bandwidth(diff_values)) / 2.0,
    )
    return CalibrationParams(
        kind="kde",
        same=tuple(same_values),
        different=tuple(diff_values),
        bandwidth=bandwidth,
        n_labeled=len(same_values) + len(diff_values),
    )


@dataclass(frozen=True)
class AnswerExample:
    """One DM answer, as the channel-weight fitter sees it."""

    #: channel -> {candidate: log LR}
    log_lr: dict[str, dict[str, float]]
    chosen: str
    #: optional log prior per candidate (the campaign roster prior)
    prior: dict[str, float] = field(default_factory=dict)


def fit_channel_weights(
    examples: Sequence[AnswerExample],
    *,
    channels: Sequence[str] = CHANNELS,
    initial: float = 1.0,
    regularisation: float = 1.0,
    steps: int = 300,
    learning_rate: float = 0.5,
) -> dict[str, float]:
    """Fit the per-channel weights to maximise the likelihood of the DM's answers.

    The model is the fusion itself: for one utterance and candidate c,

        score(c) = prior(c) + SUM_e w_e * log LR_e(u, c)

    and the DM's answer is a draw from its softmax. The gradient is the familiar
    "observed minus expected" of a softmax model, with an L2 pull toward
    `initial` so that a handful of answers cannot swing a channel somewhere
    absurd. Below the campaign's labelled-data threshold the caller simply does
    not call this and keeps the module defaults (S7.2).
    """
    if not examples:
        return {channel: initial for channel in channels}

    weights = np.full(len(channels), float(initial))
    n = len(examples)
    for _ in range(max(1, steps)):
        gradient = -regularisation * (weights - initial)
        for example in examples:
            candidates = set(example.prior) | set(example.log_lr.get("", {}))
            for channel in channels:
                candidates.update(example.log_lr.get(channel, {}))
            if not candidates:
                continue
            scores = {
                candidate: float(example.prior.get(candidate, 0.0))
                + sum(
                    weights[i] * float(example.log_lr.get(channel, {}).get(candidate, 0.0))
                    for i, channel in enumerate(channels)
                )
                for candidate in candidates
            }
            probabilities = softmax(scores)
            for i, channel in enumerate(channels):
                lrs = example.log_lr.get(channel, {})
                observed = float(lrs.get(example.chosen, 0.0))
                expected = sum(
                    probabilities[c] * float(lrs.get(c, 0.0)) for c in candidates
                )
                gradient[i] += observed - expected
        weights = weights + learning_rate * (gradient / n)
        weights = np.clip(weights, MIN_CHANNEL_WEIGHT, MAX_CHANNEL_WEIGHT)
    return {channel: float(weights[i]) for i, channel in enumerate(channels)}


@dataclass(frozen=True)
class Calibration:
    """Everything the engine needs to score evidence for one campaign."""

    voice: CalibrationParams = field(default_factory=CalibrationParams.cold_start)
    weights: dict[str, float] = field(default_factory=dict)
    n_labeled: int = 0
    false_confident_rate: float | None = None

    @property
    def fitted(self) -> bool:
        return self.voice.fitted or bool(self.weights)

    def log_lr(self, cosine: float, *, quality: float = 1.0) -> float:
        return self.voice.log_lr(cosine, quality=quality)

    def channel_weight(self, channel: str) -> float:
        return float(self.weights.get(channel, 1.0))

    def tightened(self, *, factor: float = 0.9) -> Calibration:
        """A stricter copy of this calibration.

        Used when the measured false-confident rate exceeds the campaign's
        tolerance: the thresholds are tightened rather than the DM being told to
        be more careful (S7.2, S16). Shrinking the voice curve's slope does
        exactly that - the same cosine now buys less confidence.
        """
        return Calibration(
            voice=CalibrationParams(
                kind=self.voice.kind,
                slope=self.voice.slope * factor,
                midpoint=self.voice.midpoint,
                same=self.voice.same,
                different=self.voice.different,
                bandwidth=self.voice.bandwidth,
                n_labeled=self.voice.n_labeled,
                clip=self.voice.clip * factor,
            ),
            weights={k: v * factor for k, v in self.weights.items()},
            n_labeled=self.n_labeled,
            false_confident_rate=self.false_confident_rate,
        )

    def as_params(self) -> dict[str, Any]:
        return {
            "voice": self.voice.as_params(),
            "weights": {k: round(float(v), 6) for k, v in self.weights.items()},
            "n_labeled": self.n_labeled,
            "false_confident_rate": self.false_confident_rate,
        }

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> Calibration:
        return cls(
            voice=CalibrationParams.from_params(params.get("voice") or {}),
            weights={k: float(v) for k, v in (params.get("weights") or {}).items()},
            n_labeled=int(params.get("n_labeled", 0)),
            false_confident_rate=(
                float(params["false_confident_rate"])
                if params.get("false_confident_rate") is not None
                else None
            ),
        )
