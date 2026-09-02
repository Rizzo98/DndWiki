"""transcription-deepgram-service configuration.

API-backed transcription worker (Deepgram Speech-to-Text, Nova 3, diarized
output).

This service is a drop-in replacement for the on-prem WhisperX worker: it
consumes the same `transcription.jobs` queue, drives the same session state
machine through session-service's internal API, writes the same MinIO
artifacts and publishes the same `transcription.completed` event. The only
difference is the engine: an HTTPS call per audio chunk to Deepgram instead
of locally loaded WhisperX + pyannote + wav2vec2 models.

Swap between this worker and the OpenAI one with the single compose override
docker-compose.transcription.yml:

    TRANSCRIPTION_PROVIDER=deepgram docker compose -f docker-compose.yml -f docker-compose.transcription.yml up -d --build
    TRANSCRIPTION_PROVIDER=openai   docker compose -f docker-compose.yml -f docker-compose.transcription.yml up -d --build
"""

from functools import lru_cache

from dnd_common.config import Settings


class ServiceSettings(Settings):
    service_db_name: str = "dnd_sessions"

    # --- Deepgram Speech-to-Text ---
    # Placeholder in .env / docker-compose.transcription.yml; fill before
    # processing jobs.
    deepgram_api_key: str = ""
    # Endpoint base; override to route through a compatible gateway/proxy.
    deepgram_api_base: str = "https://api.deepgram.com/v1"
    # Nova 3 = Deepgram's most accurate model (ASR + speaker diarization in
    # one request; no separate diarization model to configure).
    deepgram_transcription_model: str = "nova-3"
    # BCP-47 language hint (e.g. "it", "en", "it-IT"). Empty = language=multi
    # (Nova auto-detects the language of each chunk, mirroring the OpenAI
    # variant's "empty = server auto-detect"). Unlike OpenAI, Deepgram does
    # NOT default to auto-detection when the parameter is omitted — omitting
    # it defaults to English, so auto-detect must be requested explicitly.
    deepgram_language: str = ""
    # smart_format normalizes numbers/dates/currency; punctuate adds
    # punctuation. Both are per-request Deepgram features (true by default).
    deepgram_smart_format: bool = True
    deepgram_punctuate: bool = True
    # Long recordings are decoded locally (ffmpeg) and sliced into fixed-length
    # chunks: bounded payloads keep per-request latency low and side-step
    # Deepgram's plan-dependent per-request size limits. Same default as the
    # on-prem worker's WHISPER_CHUNK_SECONDS and the OpenAI variant.
    deepgram_chunk_seconds: int = 300
    # Safety cap per uploaded WAV chunk (16 kHz mono PCM = 32 KB/s).
    deepgram_max_upload_mb: int = 24
    deepgram_request_timeout_sec: float = 900.0
    # Client-side retries for 429/5xx/network failures; the broker adds its
    # own redelivery retries on top (see dnd_common.consume).
    deepgram_max_retries: int = 3

    # --- session-service internal worker API ---
    session_service_url: str = "http://localhost:8003"
    session_service_timeout_sec: float = 30.0

    # --- MinIO buckets ---
    minio_recordings_bucket: str = "recordings"
    minio_transcripts_bucket: str = "transcripts"

    # Where the raw recording is staged before upload (a tmpfs in prod)
    work_dir: str = "/tmp/dnd-transcription"


@lru_cache
def get_settings() -> ServiceSettings:
    return ServiceSettings()
