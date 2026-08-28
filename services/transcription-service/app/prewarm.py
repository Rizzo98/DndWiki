"""Pre-download alignment checkpoints at container start.

whisperx loads a per-language wav2vec2 alignment checkpoint on first use
(torchaudio VoxPopuli bundles, cached under torch.hub's TORCH_HOME). On an
ephemeral container that cache is wiped on every recreate, so the first job
of each container re-downloads it. This module pre-fetches the checkpoints
for PREWARM_LANGUAGES into TORCH_HOME (which lives on the models volume), so
the download happens once at startup and jobs never wait on it.
"""

import logging

from app.core.config import get_settings
from app.pipeline import _with_retry, get_device

logger = logging.getLogger(__name__)


def prewarm_align(languages: list[str], settings) -> None:
    """Download the wav2vec2 alignment checkpoint for each language code."""
    import whisperx

    device = get_device(settings)
    for lang in languages:
        try:
            _with_retry(
                lambda lang=lang: whisperx.load_align_model(
                    language_code=lang, device=device
                )
            )
            logger.info("Alignment checkpoint for %r ready", lang)
        except Exception:
            logger.exception("Failed to prewarm alignment checkpoint for %r", lang)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    languages = [s.strip() for s in settings.prewarm_languages.split(",") if s.strip()]
    if not languages:
        logger.info("PREWARM_LANGUAGES empty; skipping alignment prewarm")
        return
    prewarm_align(languages, settings)


if __name__ == "__main__":
    main()
