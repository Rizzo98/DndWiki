"""content-service configuration.

The worker needs four integration points beyond the shared settings:
- MinIO ``transcripts`` bucket (input: the named transcript JSON)
- session-service internal API (pipeline state machine: generating_wiki ->
  content_ready, or failed)
- user-service internal API (resolve user ids -> display names for the
  named-transcript view)
- wiki-service API (create pending_review draft pages + proposed relations)

LLM calls go through LiteLLM (any provider); provider keys come from env.
The provider is selected by the ``llm_provider`` setting and the model string
uses LiteLLM's ``provider/model`` convention (e.g. ``deepseek/deepseek-chat``).
Add a new provider by wiring its API key env var here and in the root
.env.example / docker-compose.yml — no code changes needed downstream.
"""

from functools import lru_cache

from dnd_common.config import Settings


class ServiceSettings(Settings):
    service_db_name: str = "dnd_content"

    # --- LLM (via LiteLLM) ---
    # Provider name recorded on generation jobs and summaries. The model string
    # is LiteLLM's "provider/model", which is what actually selects the API.
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek/deepseek-chat"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 4096
    # API keys / endpoints per provider (placeholder values until configured).
    # LiteLLM picks the right one from the model prefix; empty keys just mean
    # auth failures until the DM fills them in .env.
    deepseek_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    ollama_base_url: str = ""
    # Version of app/prompts.py shipped with this deployment; recorded per job.
    prompt_version: str = "v10"
    # Parallel per-chunk LLM calls per session job.
    llm_chunk_concurrency: int = 4
    # Corrective retries per chunk when the LLM returns malformed JSON
    # (0 disables retries; each retry re-asks the model with the parse error).
    llm_json_retries: int = 1
    # Safety cap: refuse to generate when a session yields more chunks than this.
    max_chunks_per_session: int = 16

    # --- transcript -> chunking ---
    chunk_tokens: int = 4000
    chunk_overlap: float = 0.1

    # --- MinIO input ---
    minio_transcripts_bucket: str = "transcripts"

    # --- internal service APIs ---
    session_service_url: str = "http://localhost:8003"
    user_service_url: str = "http://localhost:8001"
    wiki_service_url: str = "http://localhost:8007"
    campaign_service_url: str = "http://localhost:8002"
    service_timeout_sec: float = 30.0


@lru_cache
def get_settings() -> ServiceSettings:
    return ServiceSettings()