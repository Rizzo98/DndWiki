"""Per-observation audio quality: how much a window can be trusted.

The redesign scores every utterance's *voice evidence* by the quality of the
audio behind it, instead of trusting every cosine equally
(docs/attribution-model.md S12.3). A 1.5-second clip recorded through a bad
laptop mic while two people talk over each other must not weigh as much as a
20-second clean turn.

The module splits cleanly in two:

- the **policy** (length / SNR / overlap -> a 0..1 score) is pure arithmetic
  and is unit-tested without any audio at all;
- the **feature extraction** (waveform -> SNR estimate, overlap heuristic) uses
  numpy through a lazy import, so importing this module never requires it.

The overlap heuristic is deliberately crude and is documented as such: it is a
*prior raiser*, not a diarizer. It flags a window as suspicious when a
substantial share of its voiced frames are both far louder and far
higher-frequency than the window's own typical frame - the signature of a second
voice or a noise burst landing on top of the speaker. Its job is to down-weight
the window, never to decide anything on its own.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# --- policy defaults (mirrored by ServiceSettings knobs) --------------------
DEFAULT_MIN_SEC = 1.0          # below this a window carries no usable voice
DEFAULT_FULL_SEC = 6.0         # at/above this the length component saturates
DEFAULT_SNR_FLOOR_DB = 5.0     # at/below this the SNR component is zero
DEFAULT_SNR_FULL_DB = 20.0     # at/above this the SNR component saturates
#: SNR is often unavailable (some backends, very short clips). An unknown SNR
#: must neither help nor hurt much, so it scores a neutral 0.6 rather than 0.
NEUTRAL_SNR_SCORE = 0.6
W_LENGTH = 0.40
W_SNR = 0.35
W_OVERLAP = 0.25
#: Below this an observation is not worth enrolling or trusting as voice
#: evidence (docs/attribution-model.md S12.3).
USABLE_FLOOR = 0.35

# --- frame analysis ---------------------------------------------------------
FRAME_SEC = 0.025
HOP_SEC = 0.010
DEFAULT_SAMPLE_RATE = 16000


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def length_score(
    duration_sec: float,
    *,
    min_sec: float = DEFAULT_MIN_SEC,
    full_sec: float = DEFAULT_FULL_SEC,
) -> float:
    """Ramp from 0 at min_sec up to 1 at full_sec (and 0 below min_sec).

    A window shorter than min_sec scores 0 rather than a small positive number:
    ECAPA-TDNN needs a few seconds and a shorter clip is not weak evidence, it
    is no evidence.
    """
    if duration_sec < min_sec:
        return 0.0
    if full_sec <= min_sec:
        return 1.0
    return _clamp01((duration_sec - min_sec) / (full_sec - min_sec))


def snr_score(
    snr_db: float | None,
    *,
    floor_db: float = DEFAULT_SNR_FLOOR_DB,
    full_db: float = DEFAULT_SNR_FULL_DB,
) -> float:
    """Ramp from 0 at floor_db to 1 at full_db; None -> the neutral score."""
    if snr_db is None:
        return NEUTRAL_SNR_SCORE
    if full_db <= floor_db:
        return 1.0
    return _clamp01((snr_db - floor_db) / (full_db - floor_db))


def overlap_score(overlap_ratio: float) -> float:
    """1 for a clean window, 0 once half of it looks overlapped."""
    return _clamp01(1.0 - 2.0 * max(0.0, overlap_ratio))


def combine_scores(length: float, snr: float, overlap: float) -> float:
    """Weighted geometric mean of the three components.

    Geometric rather than arithmetic on purpose: a zero in any component must
    pull the whole score down hard (a window with no length is unusable however
    clean it sounds), while the weights still let a good window absorb one
    mediocre component.
    """
    length = _clamp01(length)
    snr = _clamp01(snr)
    overlap = _clamp01(overlap)
    # A hard zero anywhere is a hard zero overall. Short-circuiting it (rather
    # than leaning on the 1e-9 log guard) keeps "no length / no SNR" at exactly
    # 0.0, which is what the usable gate and the callers expect to see.
    if length == 0.0 or snr == 0.0 or overlap == 0.0:
        return 0.0
    product = (
        max(length, 1e-9) ** W_LENGTH
        * max(snr, 1e-9) ** W_SNR
        * max(overlap, 1e-9) ** W_OVERLAP
    )
    return _clamp01(product)


@dataclass(frozen=True)
class ObservationQuality:
    """The quality verdict for one observation window."""

    duration_sec: float
    snr_db: float | None
    overlap_ratio: float
    length_score: float
    snr_score: float
    overlap_score: float
    score: float
    usable: bool

    def as_payload(self) -> dict[str, float | None]:
        """Compact form written into the Qdrant observation payload."""
        return {
            "quality": round(self.score, 4),
            "snr_db": None if self.snr_db is None else round(self.snr_db, 2),
            "overlap_ratio": round(self.overlap_ratio, 4),
        }


def assess(
    duration_sec: float,
    *,
    snr_db: float | None = None,
    overlap_ratio: float = 0.0,
    min_sec: float = DEFAULT_MIN_SEC,
    full_sec: float = DEFAULT_FULL_SEC,
    floor: float = USABLE_FLOOR,
) -> ObservationQuality:
    """Combine the three components into the stored quality verdict."""
    length = length_score(duration_sec, min_sec=min_sec, full_sec=full_sec)
    snr = snr_score(snr_db)
    overlap = overlap_score(overlap_ratio)
    score = combine_scores(length, snr, overlap)
    return ObservationQuality(
        duration_sec=duration_sec,
        snr_db=snr_db,
        overlap_ratio=overlap_ratio,
        length_score=length,
        snr_score=snr,
        overlap_score=overlap,
        score=score,
        usable=score >= floor and duration_sec >= min_sec,
    )


# --- feature extraction (numpy, lazily imported) ----------------------------


def _frames(samples: Sequence[float], sample_rate: int):
    """Split a waveform into (n_frames, frame_len) float64 frames (no padding)."""
    import numpy as np  # lazy: this module stays importable without numpy

    signal = np.asarray(samples, dtype=np.float64)
    frame_len = max(1, int(FRAME_SEC * sample_rate))
    hop = max(1, int(HOP_SEC * sample_rate))
    if signal.size < frame_len:
        return np.zeros((0, frame_len), dtype=np.float64)
    count = 1 + (signal.size - frame_len) // hop
    idx = np.arange(frame_len)[None, :] + hop * np.arange(count)[:, None]
    return signal[idx]


def estimate_snr_db(
    samples: Sequence[float], sample_rate: int = DEFAULT_SAMPLE_RATE
) -> float | None:
    """Crude SNR estimate: speech-level frames over noise-floor frames.

    The noise floor is the mean RMS of the quietest 20% of frames and the speech
    level the mean RMS of the loudest 10%. Returns None when the window has no
    frames or is digitally silent (both cases where a ratio is meaningless).
    """
    import numpy as np  # lazy

    frames = _frames(samples, sample_rate)
    if frames.shape[0] == 0:
        return None
    rms = np.sqrt(np.mean(frames**2, axis=1))
    rms = rms[np.isfinite(rms)]
    if rms.size == 0:
        return None
    noise = float(np.mean(np.sort(rms)[: max(1, rms.size // 5)]))
    speech = float(np.mean(np.sort(rms)[-max(1, rms.size // 10) :]))
    if noise <= 1e-12 or speech <= 1e-12:
        return None
    return 20.0 * math.log10(speech / noise)


def estimate_overlap_ratio(
    samples: Sequence[float], sample_rate: int = DEFAULT_SAMPLE_RATE
) -> float:
    """Share of voiced frames that look like a second talker on top of the first.

    A frame is 'suspicious' when it is both much louder than the window's own
    typical voiced frame and has a much higher zero-crossing rate (a bright,
    noisy event such as another voice, a chair scrape or a dice tray). Returns a
    value in [0, 1]; 0.0 when there is nothing to judge.

    'Typical' is the median, not an upper quartile, on purpose: if a quarter of
    the window is overlapped, a p75 threshold would simply move up to the
    overlapped level and flag nothing. The cost of using the median is that the
    heuristic saturates once MORE than half the window is overlapped (the median
    then describes the intrusion, not the speaker); that is acceptable because
    the value only ever down-weights a window, and a window that is mostly
    somebody else is already below the usable floor on the length/overlap mix.
    """
    import numpy as np  # lazy

    frames = _frames(samples, sample_rate)
    if frames.shape[0] < 4:
        return 0.0
    rms = np.sqrt(np.mean(frames**2, axis=1))
    # zero-crossing rate per frame
    signs = np.signbit(frames)
    zcr = np.mean(signs[:, 1:] != signs[:, :-1], axis=1)
    peak = float(np.max(rms))
    if peak <= 1e-12:
        return 0.0
    gate = max(0.05 * peak, float(np.percentile(rms, 25)))
    voiced = rms >= gate
    if int(np.count_nonzero(voiced)) < 4:
        return 0.0
    vrms = rms[voiced]
    vzcr = zcr[voiced]
    loud = vrms >= 2.0 * float(np.median(vrms))
    median_zcr = float(np.median(vzcr))
    if median_zcr <= 0.0:
        # A perfectly periodic window has no meaningful ZCR dispersion; only the
        # loudness anomaly can flag it.
        suspicious = loud
    else:
        suspicious = loud & (vzcr >= 1.5 * median_zcr)
    return float(np.count_nonzero(suspicious)) / float(vzcr.size)
