"""content-service configuration.

The worker needs four integration points beyond the shared settings:
- MinIO ``transcripts`` bucket (input: the named transcript JSON)
- session-service internal API (pipeline state machine: summarizing ->
  summary_ready -> generating_wiki -> content_ready, or failed)
- user-service internal API (resolve user ids -> display names for the
  named-transcript view)
- wiki-service API (campaign page listing, session content guard, apply)

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
    # KEEP IN SYNC with prompts.PROMPT_VERSION. v13 tells the extraction what the
    # '[Stretches]' note in front of a chunk means: in a stretch where the record
    # puts exactly one party member present, a beat about a party member is about
    # THAT member, and an unnamed actor ("un personaggio") is never acceptable.
    # v14 makes the summary revision answer with a PATCH (the summary + the
    # items the correction touches) instead of echoing the whole extraction:
    # the echo did not fit the completion cap and its tail - events, timeline
    # entries - was silently replaced by the previous revision's (app/revision.py).
    # v15 writes the summary as one NARRATIVE in scene blocks and is reviewed by
    # highlighted portion instead of ticking lines (app/summary.py).
    prompt_version: str = "v15"
    # Parallel per-chunk LLM calls per session job.
    llm_chunk_concurrency: int = 4
    # Corrective retries per chunk when the LLM returns malformed JSON
    # (0 disables retries; each retry re-asks the model with the parse error).
    llm_json_retries: int = 1
    # Safety cap: refuse to generate when a session yields more chunks than this.
    # Raised from 16 for the redesign: the '[u_XXXXX]' reference on every view
    # line adds roughly 2.5 tokens per line, which on a four-hour session is one
    # to two extra chunks - and exceeding the cap raises ValueError and fails the
    # job, so the old ceiling would have turned a correct change into an outage.
    max_chunks_per_session: int = 24

    # --- attribution (docs/attribution-model.md) ---
    # When true, generation reads transcripts/{id}/attributed.json instead of
    # transcript.json + the speakers.identified label map, and the attribution
    # gate filters what may reach a character page. False keeps the old path.
    attribution_enabled: bool = False
    # NOTE: there is deliberately no attribution_service_url here. Generation
    # reads transcripts/{id}/attributed.json from MinIO - the same artifact the
    # engine writes and the audit trail the DM can inspect - rather than asking
    # the service over HTTP, so a down attribution-service cannot stall a session
    # whose attribution is already computed.

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