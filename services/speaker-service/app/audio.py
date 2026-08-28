"""Speaker audio helpers: window picking + WAV slicing.

Diarized segments are grouped by speaker label; for each label we pick the
longest run of consecutive speech (up to pool_sec) as the embedding window.
The window is sliced out of the session recording at 16 kHz mono WAV, which is
what the ECAPA-TDNN classifier expects.

torch/torchaudio are imported lazily so pure window logic stays testable in the
dev venv (which has neither installed).
"""

from __future__ import annotations

import io

# Maximum silence gap allowed between two segments of the same speaker run.
GAP_TOLERANCE_SEC = 1.5
TARGET_SAMPLE_RATE = 16000


def _runs(spans: list[tuple[float, float]], gap_tolerance: float):
    """Yield (start, end) runs of consecutive spans (gap <= tolerance)."""
    run_start, run_end = spans[0]
    for start, end in spans[1:]:
        if start - run_end <= gap_tolerance:
            run_end = max(run_end, end)
        else:
            yield run_start, run_end
            run_start, run_end = start, end
    yield run_start, run_end


def speaker_windows(
    segments: list[dict],
    pool_sec: float = 20.0,
    gap_tolerance: float = GAP_TOLERANCE_SEC,
) -> dict[str, tuple[float, float]]:
    """Map each speaker label to the (start, end) window to embed.

    The window is the longest run of consecutive segments for that speaker,
    capped at pool_sec; speakers without a usable window are omitted.
    """
    groups: dict[str, list[tuple[float, float]]] = {}
    for seg in segments:
        label = seg.get("speaker")
        if not label:
            continue
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", 0.0))
        if end <= start:
            continue
        groups.setdefault(label, []).append((start, end))

    windows: dict[str, tuple[float, float]] = {}
    for label, spans in groups.items():
        spans.sort()
        best = (0.0, 0.0)
        for run_start, run_end in _runs(spans, gap_tolerance):
            window_end = min(run_end, run_start + pool_sec)
            if window_end - run_start > best[1] - best[0]:
                best = (run_start, window_end)
        if best[1] > best[0]:
            windows[label] = best
    return windows


def slice_wav_bytes(audio_path: str, start: float, end: float) -> bytes:
    """Slice [start, end] out of the recording as 16 kHz mono WAV bytes.

    Loading goes through torchaudio (torchcodec/ffmpeg -> any container);
    the WAV encode uses soundfile (libsndfile), because torchcodec cannot
    write to a BytesIO stream.
    """
    import soundfile as sf
    import torch  # lazy: heavy/optional deps
    import torchaudio

    waveform, sr = torchaudio.load(audio_path)
    if waveform.shape[0] > 1:
        waveform = torch.mean(waveform, dim=0, keepdim=True)
    if sr != TARGET_SAMPLE_RATE:
        waveform = torchaudio.transforms.Resample(sr, TARGET_SAMPLE_RATE)(waveform)
    s = max(0, int(start * TARGET_SAMPLE_RATE))
    e = min(waveform.shape[1], int(end * TARGET_SAMPLE_RATE))
    if e <= s:
        raise ValueError(f"empty audio window {start:.3f}-{end:.3f}")
    clip = waveform[:, s:e].squeeze(0)
    buf = io.BytesIO()
    sf.write(buf, clip.numpy(), TARGET_SAMPLE_RATE, format="WAV")
    return buf.getvalue()
