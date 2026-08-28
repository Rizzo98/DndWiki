"""transcription-service configuration.

WhisperX worker settings plus the integration points it needs:
- session-service internal API (state machine + artifact updates)
- MinIO buckets for raw audio (input) and transcript artifacts (output)
"""

from functools import lru_cache

from dnd_common.config import Settings


class ServiceSettings(Settings):
    service_db_name: str = "dnd_sessions"

    # --- WhisperX pipeline ---
    # small is the CPU-friendly default (large-v3 on CPU is impractically slow)
    whisper_model: str = "small"
    diarization_model: str = "pyannote/speaker-diarization-3.1"
    whisper_batch_size: int = 8
    # Long recordings are processed in fixed-length chunks so partial results
    # can be published as transcription.progress while the job runs, and each
    # chunk stays far below the broker consumer timeout.
    whisper_chunk_seconds: int = 300
    # float16 requires CUDA; on CPU the worker falls back to whisper_cpu_compute_type
    whisper_compute_type: str = "float16"
    # The ASR backend is faster-whisper/CTranslate2: int8 is the memory-safe CPU
    # default (large-v3 float32 needs ~6 GB of RAM and gets OOM-killed on
    # typical dev VMs); float32 is honored only when set explicitly.
    whisper_cpu_compute_type: str = "int8"
    # Empty = auto-detect (cuda when available, else cpu); force with cuda/cpu
    whisper_device: str = ""
    # Required for the gated pyannote diarization models
    hf_token: str = ""
    # Comma-separated ISO-639-1 codes whose alignment checkpoints should be
    # pre-downloaded at container start (e.g. "it,en"). Empty = no prewarm.
    prewarm_languages: str = ""
    # Where torch.hub caches alignment checkpoints (must be on the models
    # volume so they survive container recreates).
    torch_home: str = "/app/models/torch"

    # --- session-service internal worker API ---
    session_service_url: str = "http://localhost:8003"
    session_service_timeout_sec: float = 30.0

    # --- MinIO buckets ---
    minio_recordings_bucket: str = "recordings"
    minio_transcripts_bucket: str = "transcripts"

    # Where raw audio is staged while transcribing (a tmpfs in prod)
    work_dir: str = "/tmp/dnd-transcription"


@lru_cache
def get_settings() -> ServiceSettings:
    return ServiceSettings()
