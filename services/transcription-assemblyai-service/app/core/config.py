"""transcription-assemblyai-service configuration.

API-backed transcription worker (AssemblyAI Speech-to-Text, Universal-3.5 Pro,
diarized output).

This service is a drop-in replacement for the on-prem WhisperX worker: it
consumes the same `transcription.jobs` queue, drives the same session state
machine through session-service's internal API, writes the same MinIO
artifacts and publishes the same `transcription.completed` event. The only
difference is the engine: per audio chunk it uploads the WAV to AssemblyAI,
submits a pre-recorded transcript job (Universal-3.5 Pro with speaker
diarization) and polls until it completes — instead of locally loaded
WhisperX + pyannote + wav2vec2 models.

Swap between this worker and the other cloud backends with the single compose
override docker-compose.transcription.yml:

    TRANSCRIPTION_PROVIDER=assemblyai docker compose -f docker-compose.yml -f docker-compose.transcription.yml up -d --build
    TRANSCRIPTION_PROVIDER=deepgram    docker compose -f docker-compose.yml -f docker-compose.transcription.yml up -d --build
    TRANSCRIPTION_PROVIDER=openai      docker compose -f docker-compose.yml -f docker-compose.transcription.yml up -d --build

Per-campaign parameters (language, expected speaker count, contextual prompt)
are read from campaign-service at job time — see ASSEMBLYAI_LANGUAGE /
ASSEMBLYAI_PROMPT below for the env-level fallbacks.
"""

from functools import lru_cache

from dnd_common.config import Settings


class ServiceSettings(Settings):
    service_db_name: str = "dnd_sessions"

    # --- AssemblyAI Speech-to-Text (Universal-3.5 Pro) ---
    # Placeholder in .env / docker-compose.transcription.yml; fill before
    # processing jobs (https://www.assemblyai.com/dashboard/home).
    assemblyai_api_key: str = ""
    # Endpoint base (no trailing slash); the v2 paths are appended by the client.
    assemblyai_api_base: str = "https://api.assemblyai.com"
    # Universal-3.5 Pro = AssemblyAI's most accurate model (ASR + speaker
    # diarization in one request; the prompt parameter steers transcription).
    # Sent as the speech_models ARRAY on /v2/transcript; may list several
    # models comma-separated (e.g. "universal-3-5-pro,universal-2") so the API
    # can fall back when the first model does not support the audio language.
    assemblyai_transcription_model: str = "universal-3-5-pro"
    # Optional BCP-47-ish language hint (e.g. "it", "en", "de"). EMPTY =
    # prefer the campaign's language (campaign-service) at job time, falling
    # back to AssemblyAI auto-detection. Set this to force a language.
    assemblyai_language: str = ""
    # Contextual prompt (https://www.assemblyai.com/docs/pre-recorded-audio/
    # universal-3-5-pro/prompting): a plain-language description of WHAT the
    # audio is (domain/scenario/details). Empty = the worker uses the built-in
    # D&D-session prompt (app.prompts), enriched with the campaign name/
    # description when campaign-service is reachable.
    assemblyai_prompt: str = ""
    # Inject the campaign roster (character/player names) as keyterms_prompt
    # so fantasy names are recognized accurately (best-effort, per job).
    assemblyai_keyterms_enabled: bool = True
    # Long recordings are decoded locally (ffmpeg) and sliced into fixed-length
    # chunks: bounded jobs keep per-chunk latency low, produce
    # transcription.progress events after every chunk and side-step the
    # account's parallel-job rate limits. Same default as the other workers.
    assemblyai_chunk_seconds: int = 300
    # Safety cap per uploaded WAV chunk (16 kHz mono PCM = 32 KB/s).
    assemblyai_max_upload_mb: int = 24
    assemblyai_request_timeout_sec: float = 900.0
    # Client-side retries for 429/5xx/network failures; the broker adds its
    # own redelivery retries on top (see dnd_common.consume).
    assemblyai_max_retries: int = 3
    # Polling of the async transcript job.
    assemblyai_poll_interval_sec: float = 3.0
    assemblyai_poll_timeout_sec: float = 1800.0

    # --- campaign-service internal worker API ---
    # The campaign's language, roster size (expected speakers) and
    # name/description (prompt context) are fetched per job from here.
    campaign_service_url: str = "http://localhost:8002"
    campaign_service_timeout_sec: float = 5.0

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
