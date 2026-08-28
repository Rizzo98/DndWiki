"""speaker-service configuration.

The worker identifies diarized ``SPEAKER_XX`` labels by embedding each
speaker's voice (ECAPA-TDNN via SpeechBrain) and cosine-searching the campaign
voiceprints in Qdrant. It also enrolls a session-derived voiceprint whenever
the DM names a speaker, so the name is reused automatically in later sessions.
"""

from functools import lru_cache

from dnd_common.config import Settings


class ServiceSettings(Settings):
    service_db_name: str = "dnd_sessions"

    # --- voiceprint matching ---
    # ECAPA-TDNN speaker embedding (same model as user-service enrollment)
    voice_embedding_model: str = "speechbrain/spkrec-ecapa-voxceleb"
    # Minimum cosine similarity to auto-assign a name; below this -> pending
    speaker_match_threshold: float = 0.75
    voiceprint_collection: str = "voiceprints"
    # Embedding dimensionality (ECAPA-TDNN voxceleb -> 192-d)
    embedding_dim: int = 192
    # Payload tag written for session-derived voiceprints (bumped on model change)
    embedding_version: int = 1
    # Shortest speaker clip worth embedding (shorter -> no reliable voiceprint)
    voice_sample_min_sec: float = 2.0
    # How much of a speaker's audio to pool per embedding (longest run, capped)
    speaker_pool_sec: float = 20.0

    # --- LLM contextual refinement (refiner-service) ---
    # When the refiner is enabled, speaker-service ignores transcription.completed
    # (the refiner emits transcription.refined instead, with labels already fixed
    # by the LLM contextual pass) and matches the refined labels directly.
    refiner_enabled: bool = True

    # --- speaker re-clustering (override untrusted raw diarizer labels) ---
    # Enable the embedding -> clustering -> relabel stage before matching.
    relabel_enabled: bool = True
    # "threshold": cosine agglomerative clustering cut at relabel_merge_threshold
    #   (robust when the speaker count is unknown, e.g. chunked API labels);
    # "count": cluster into K = number of distinct raw labels (the diarizer's
    #   speaker count — reasonable only when the file was diarized in one pass).
    relabel_mode: str = "threshold"
    # Cosine similarity at/below which two turns are merged in threshold mode.
    relabel_merge_threshold: float = 0.5
    # Cosine similarity at/below which a cluster is snapped to an enrolled user.
    relabel_anchor_threshold: float = 0.6
    # Turns shorter than this are not embedded; they inherit their neighbour's label.
    relabel_min_turn_sec: float = 1.0
    # Force the cluster count K (overrides relabel_mode); e.g. the DM knows N players.
    relabel_speaker_count: int | None = None

    # Required for gated HF models if the embedding model ever needs a token
    hf_token: str = ""

    # --- session-service internal worker API ---
    session_service_url: str = "http://localhost:8003"
    session_service_timeout_sec: float = 30.0

    # --- MinIO buckets ---
    minio_recordings_bucket: str = "recordings"
    minio_transcripts_bucket: str = "transcripts"

    # Where raw audio is staged while extracting speaker clips (a tmpfs in prod)
    work_dir: str = "/tmp/dnd-speakers"


@lru_cache
def get_settings() -> ServiceSettings:
    return ServiceSettings()