"""ECAPA-TDNN voice embedding (SpeechBrain), same model as user-service.

torch/speechbrain are heavy optional dependencies: they are imported lazily so
the worker boots (and health checks pass) without them. Identification calls
VoiceEmbedder.embed_bytes and gets a clear error if the ML stack is missing.

This mirrors user-service/app/embedding.py on purpose (the two services share
the model + savedir so a print enrolled by one is comparable to a print
extracted by the other).
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.core.config import ServiceSettings
from app.quality import estimate_overlap_ratio, estimate_snr_db

logger = logging.getLogger(__name__)

# ECAPA-TDNN (spkrec-ecapa-voxceleb) expects 16 kHz mono; SpeechBrain's
# encode_batch does NOT resample (normalize=False default). Worker input is
# already 16 kHz mono (slice_wav_bytes), but normalize explicitly so raw
# uploads/stereo clips are embedded identically to user-service enrollment.
SAMPLE_RATE = 16000


@dataclass(frozen=True)
class WindowAnalysis:
    """One embedded window plus the quality measurements taken from its audio."""

    embedding: list[float] | None
    snr_db: float | None
    overlap_ratio: float


class EmbeddingUnavailable(RuntimeError):
    """The ML stack is not installed; speaker identification cannot run."""


class VoiceEmbedder:
    """Extracts 192-d ECAPA-TDNN embeddings from audio clips."""

    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings
        self._classifier = None

    def _load_classifier(self):
        if self._classifier is None:
            try:
                from speechbrain.inference.speaker import EncoderClassifier
                from speechbrain.utils.fetching import FetchConfig
            except ImportError as exc:  # pragma: no cover - env-dependent
                raise EmbeddingUnavailable(
                    "speechbrain is not installed; speaker identification cannot run"
                ) from exc
            token = self._settings.hf_token or None
            if token:
                os.environ.setdefault("HF_TOKEN", token)
            # SpeechBrain >= 1.0 dropped use_auth_token on from_hparams (extra
            # kwargs go to Pretrained.__init__ -> TypeError); HF auth now goes
            # through FetchConfig. token=True makes hf_hub_download pick up the
            # HF_TOKEN env var set above (public models work with token=False).
            self._classifier = EncoderClassifier.from_hparams(
                source=self._settings.voice_embedding_model,
                savedir="/app/models",
                fetch_config=FetchConfig(token=bool(token)),
            )
        return self._classifier

    def embed_windows_full_sync(
        self, audio_path: str, windows: list[tuple[float, float]]
    ) -> list[WindowAnalysis]:
        """Embed windows AND measure their quality from the same decode.

        The redesign scores every observation's voice evidence by the quality of
        the audio behind it (attribution-model S12.3), which needs the waveform,
        not just the embedding. Decoding the recording twice (once to embed, once
        to measure) would double the cost of a four-hour session for no reason,
        so both are produced here from one load.

        Blocking - call from a worker thread.
        """
        import numpy as np  # lazy
        import torchaudio  # lazy

        signal, sr = torchaudio.load(audio_path)
        if signal.shape[0] > 1:
            signal = signal.mean(dim=0, keepdim=True)
        if sr != SAMPLE_RATE:
            signal = torchaudio.transforms.Resample(sr, SAMPLE_RATE)(signal)

        classifier = self._load_classifier()
        out: list[WindowAnalysis] = []
        for start, end in windows:
            s = max(0, int(start * SAMPLE_RATE))
            e = min(signal.shape[1], int(end * SAMPLE_RATE))
            if e <= s:
                out.append(WindowAnalysis(embedding=None, snr_db=None, overlap_ratio=0.0))
                continue
            clip = signal[:, s:e]
            emb = classifier.encode_batch(clip).squeeze().tolist()
            if not isinstance(emb, list):
                emb = [emb]  # single-channel edge case
            samples = np.asarray(clip.squeeze(0).numpy(), dtype=np.float64)
            out.append(
                WindowAnalysis(
                    embedding=emb,
                    snr_db=estimate_snr_db(samples, SAMPLE_RATE),
                    overlap_ratio=estimate_overlap_ratio(samples, SAMPLE_RATE),
                )
            )
        return out

    def embed_windows_sync(
        self, audio_path: str, windows: list[tuple[float, float]]
    ) -> list[list[float] | None]:
        """Embed a batch of [start, end] windows from one file (single decode).

        Decodes the recording once (mono downmix + 16 kHz resample, exactly as
        embed_bytes does), then embeds each window's slice in turn. Returns a
        list aligned to ``windows``; a window that resolves to zero samples
        yields ``None``. Blocking — call from a worker thread.
        """
        analyses = self.embed_windows_full_sync(audio_path, windows)
        return [a.embedding for a in analyses]

    async def embed_bytes(self, data: bytes, suffix: str = ".wav") -> tuple[list[float], float]:
        """Return (embedding, duration_sec) for a raw audio clip.

        The clip is written to a temp file so torchaudio's ffmpeg backend can
        load any container regardless of extension.
        """
        import torchaudio  # lazy

        with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
            tmp.write(data)
            tmp.flush()
            path = Path(tmp.name)

            # torchaudio >= 2.9 removed torchaudio.info; derive the duration
            # from the loaded waveform instead (works across versions).
            signal, sr = torchaudio.load(path)
            duration = signal.shape[1] / sr if sr else 0.0

            # Same normalization as slice_wav_bytes / user-service enrollment:
            # mono downmix + 16 kHz resample (identity for worker input).
            if signal.shape[0] > 1:
                signal = signal.mean(dim=0, keepdim=True)
            if sr != SAMPLE_RATE:
                signal = torchaudio.transforms.Resample(sr, SAMPLE_RATE)(signal)

            classifier = self._load_classifier()
            emb = classifier.encode_batch(signal).squeeze().tolist()

        if not isinstance(emb, list):
            emb = [emb]  # single-channel edge case
        return emb, float(duration)
