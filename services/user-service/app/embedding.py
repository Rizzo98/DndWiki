"""ECAPA-TDNN voice embedding (SpeechBrain), same model as speaker-service.

torch/speechbrain are heavy optional dependencies (pyproject `[ml]` extra):
they are imported lazily so the service boots and serves profile APIs without
them. Enrollment calls ``VoiceEmbedder.embed_bytes`` and gets a clear error if
the ML extras are not installed.

When speaker-service lands its own embedding module this should be extracted
into dnd_common and shared (user-service README TODO).
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)

# spkrec-ecapa-voxceleb (and SpeechBrain in general) expects 16 kHz mono input;
# encode_batch does NOT resample (normalize=False default), so anything embedded
# at its native rate (e.g. 48 kHz uploads) lands in a different embedding space
# and never matches the 16 kHz session-derived prints. Resample explicitly.
SAMPLE_RATE = 16000


class EmbeddingUnavailable(RuntimeError):
    """The ML extras are not installed; voiceprint enrollment cannot run."""


def _flatten_embedding(emb: object) -> list[float]:
    """Reduce classifier output to one flat embedding (never nested).

    ``encode_batch`` returns one embedding per input row. ``embed_bytes``
    downmixes to mono first; this guard averages any residual multi-row
    output so a list-of-lists vector can never reach Qdrant (there it is a
    multi-dense vector, which a regular 192-d collection rejects with a 400).
    """
    if not isinstance(emb, list):
        return [emb]  # single-scalar edge case
    if emb and isinstance(emb[0], (list, tuple)):
        rows = [list(row) for row in emb]
        width = len(rows[0])
        return [sum(row[i] for row in rows) / len(rows) for i in range(width)]
    return emb


class VoiceEmbedder:
    """Extracts 192-d ECAPA-TDNN embeddings from enrollment audio clips."""

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
                    "speechbrain is not installed; install user-service[ml] to enroll voiceprints"
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

    async def embed_bytes(self, data: bytes, suffix: str = ".wav") -> tuple[list[float], float]:
        """Return ``(embedding, duration_sec)`` for a raw audio clip.

        The clip is written to a temp file so torchaudio's ffmpeg backend can
        load any container (m4a/aac/webm/...) regardless of extension.

        The waveform is downmixed to mono before embedding: torchaudio.load
        returns ``[channels, samples]`` and SpeechBrain treats the first
        dimension as a batch, so a stereo upload would yield two 192-d
        embeddings - a nested vector that Qdrant reads as a multi-dense
        vector and a regular 192-d 'voiceprints' collection rejects with a
        400 ("Conversion between multi and regular vectors failed").
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

            # Same mono downmix as speaker-service's slice_wav_bytes.
            if signal.shape[0] > 1:
                signal = signal.mean(dim=0, keepdim=True)

            # CRITICAL: resample to the model's 16 kHz. Without this, a 48 kHz
            # upload yields an embedding SpeechBrain computes as if it were
            # 16 kHz -> cosine ~0 against every 16 kHz session-derived print,
            # so profile-enrolled voiceprints are never recognized.
            if sr != SAMPLE_RATE:
                signal = torchaudio.transforms.Resample(sr, SAMPLE_RATE)(signal)

            classifier = self._load_classifier()
            emb = _flatten_embedding(classifier.encode_batch(signal).squeeze().tolist())

        return emb, float(duration)
