"""refiner-service configuration.

The worker runs the LLM contextual-diarization stage between transcription and
speaker identification: it consumes transcription.completed, asks an LLM to fix
transcription errors and speaker attribution (turn-level, preserving every
segment's timings), rewrites transcript/diarization.json, and emits
transcription.refined. speaker-service then matches the refined labels against
the campaign voiceprints in Qdrant.

LLM calls go through LiteLLM (any provider); provider keys come from env and
mirror content-service (LLM_PROVIDER / LLM_MODEL / DEEPSEEK_API_KEY / ...), so
one LLM configuration drives both services. The refiner keeps its own
windowing (REFINER_*) and a low temperature for deterministic editing.
"""

from functools import lru_cache

from dnd_common.config import Settings


class ServiceSettings(Settings):
    service_db_name: str = "dnd_sessions"

    # --- stage switch ---
    # True: run the LLM contextual pass and emit transcription.refined
    # (speaker-service then skips transcription.completed).
    # False: pass through - speaker-service identifies directly from
    # transcription.completed (with its own embedding re-clustering), exactly
    # as before the refiner existed.
    refiner_enabled: bool = True

    # --- LLM (via LiteLLM, same provider config as content-service) ---
    # The model string is LiteLLM's "provider/model", which selects the API.
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek/deepseek-chat"
    # Editing should be deterministic; keep near 0.
    refiner_temperature: float = 0.0
    refiner_max_tokens: int = 4096
    # Corrective retries per window when the LLM returns malformed JSON.
    refiner_json_retries: int = 1
    # Version of app/prompts.py shipped with this deployment; recorded on the
    # rewritten artifacts so the DM can see which prompt produced a transcript.
    prompt_version: str = "v2"
    # Optional per-stage model override (e.g. a bigger editor model); empty
    # falls back to llm_model.
    refiner_model: str = ""
    # API keys / endpoints per provider (placeholders until configured).
    deepseek_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    ollama_base_url: str = ""

    # --- windowing ---
    # Turns per LLM call. Long sessions are refined in sliding windows so a
    # single call stays inside the model context; windows overlap to carry
    # speaker labels across the boundary.
    refiner_window_turns: int = 100
    # Leading turns of each non-first window that are already-finalized
    # context: their labels are kept and reused for cross-window consistency.
    refiner_window_overlap: int = 15

    # --- internal service APIs ---
    session_service_url: str = "http://localhost:8003"
    session_service_timeout_sec: float = 30.0
    # campaign-service provides the campaign cast (character names + physical
    # descriptions) injected into the LLM prompt so it can correct names and
    # attribute speakers to the right person.
    campaign_service_url: str = "http://localhost:8002"
    campaign_service_timeout_sec: float = 5.0

    # --- MinIO ---
    minio_transcripts_bucket: str = "transcripts"

    @property
    def effective_model(self) -> str:
        """Model actually used for refinement (refiner_model overrides llm_model)."""
        return self.refiner_model or self.llm_model


@lru_cache
def get_settings() -> ServiceSettings:
    return ServiceSettings()