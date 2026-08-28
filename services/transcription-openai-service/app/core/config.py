"""transcription-openai-service configuration.

API-backed transcription worker (OpenAI Speech-to-Text, diarized output).

This service is a drop-in replacement for the on-prem WhisperX worker: it
consumes the same `transcription.jobs` queue, drives the same session state
machine through session-service's internal API, writes the same MinIO
artifacts and publishes the same `transcription.completed` event. The only
difference is the engine: a single HTTPS call to OpenAI instead of locally
loaded WhisperX + pyannote + wav2vec2 models.
"""

from functools import lru_cache

from dnd_common.config import Settings


class ServiceSettings(Settings):
    service_db_name: str = "dnd_sessions"

    # --- OpenAI Speech-to-Text ---
    # Placeholder in .env / docker-compose.api.yml; fill before processing jobs.
    openai_api_key: str = ""
    # Endpoint base; override to route through a compatible gateway/proxy
    # (Azure OpenAI, LiteLLM proxy, ...).
    openai_api_base: str = "https://api.openai.com/v1"
    # gpt-4o-transcribe-diarize = ASR + speaker diarization in one call;
    # diarized_json is required to receive speaker annotations.
    openai_transcription_model: str = "gpt-4o-transcribe-diarize"
    openai_response_format: str = "diarized_json"
    # "auto" makes the server VAD-chunk long recordings (required for inputs
    # longer than 30 s).
    openai_chunking_strategy: str = "auto"
    # Optional ISO-639-1 language hint (empty = server-side auto-detect).
    openai_transcription_language: str = ""
    # Long recordings are decoded locally (ffmpeg) and sliced into fixed-length
    # chunks because OpenAI caps each upload at 25 MB. Same default as the
    # on-prem worker's WHISPER_CHUNK_SECONDS.
    openai_chunk_seconds: int = 300
    # Safety cap per uploaded WAV chunk (16 kHz mono PCM = 32 KB/s; must stay
    # below OpenAI's 25 MB per-request limit).
    openai_max_upload_mb: int = 24
    openai_request_timeout_sec: float = 900.0
    # Client-side retries for 429/5xx/network failures; the broker adds its
    # own redelivery retries on top (see dnd_common.consume).
    openai_max_retries: int = 3

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
