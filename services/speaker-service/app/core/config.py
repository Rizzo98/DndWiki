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

    # --- voice samples from previous sessions' manual identifications ---
    # The DM naming a speaker is a labelled sample: while identifying a
    # session, the confirmed labels of earlier sessions in the same campaign
    # are turned into extra voiceprints (see app.history). Idempotent: each
    # (session, label, window) is enrolled at most once.
    history_samples_enabled: bool = True
    # Only turns at least this long become samples (ECAPA needs a few seconds)
    history_sample_min_sec: float = 3.0
    # ... and at most this much of a turn is embedded (one window per turn)
    history_sample_max_sec: float = 20.0
    # Turns whose diarization confidence is below this are not trusted as
    # samples; segments without any confidence data are kept (some backends
    # do not report one, and the DM did confirm the label).
    history_sample_min_confidence: float = 0.8
    # Windows kept per named label, and embeddings per identification run
    history_max_windows_per_label: int = 3
    history_max_windows_per_run: int = 24
    # How many earlier sessions of the campaign to scan (newest first)
    history_max_sessions: int = 5

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

    # Whether the attribution engine is running. It changes no behaviour here -
    # it only lets this service WARN when it is asked to produce evidence from
    # labels the refiner's LLM rewrote (REFINER_SPEAKERS=true). The engine treats
    # the diarizer's labels as measurements, and a label the LLM guessed is not a
    # measurement, so that combination silently degrades the engine's evidence.
    attribution_enabled: bool = False

    # --- per-observation voice evidence (attribution redesign, phase 0) ---
    # Instead of one pooled window per label, embed every speaker turn and store
    # it in its own Qdrant collection, so cluster purity can be measured and an
    # individual utterance can defect from its cluster (attribution-model S5/S12).
    observations_enabled: bool = True
    voice_observation_collection: str = "voice_observations"
    member_voice_model_collection: str = "member_voice_models"
    # A turn shorter than this is not embedded (ECAPA needs a few seconds); it
    # still becomes an observation so its utterances get an attribution.
    observation_min_sec: float = 1.0
    # Length component of the quality score saturates here.
    observation_full_sec: float = 6.0
    # Below this an observation is not trustworthy voice evidence.
    observation_quality_floor: float = 0.35
    # --- enrollment gates (attribution-model S12.3) ---
    # An observation is only enrolled when its own quality clears this ...
    enroll_min_quality: float = 0.5
    # ... and the identity it came from is this pure (filled by the engine; when
    # the engine has not run, a single-observation label counts as pure).
    enroll_min_purity: float = 0.9
    # How many per-centroid similarities one observation reports.
    evidence_max_centroids: int = 8
    # Centroids below this cosine are not reported (the best one always is).
    evidence_cosine_floor: float = 0.2
    # Multi-centroid aggregation over a member's voice models.
    member_score_top_k: int = 3
    member_score_mode: str = "topk_mean"

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