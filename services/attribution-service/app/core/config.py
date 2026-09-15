"""attribution-service configuration.

Every threshold the engine applies is a knob here rather than a constant buried
in the inference, because docs/attribution-model.md S15.5 makes them the contract
between the product decision ("how often may we be confidently wrong?") and the
code. The kill switch is ATTRIBUTION_ENABLED: with it off, the old
label-to-player path runs untouched.
"""

from functools import lru_cache

from dnd_common.config import Settings


class ServiceSettings(Settings):
    service_db_name: str = "dnd_attribution"

    # --- kill switch ---------------------------------------------------------
    # False: nothing in this service runs; speaker-service and the session page
    # behave exactly as they did before the redesign.
    attribution_enabled: bool = False
    engine_version: str = "attr-1"

    # --- status thresholds (S7.1) -------------------------------------------
    # auto_high demands BOTH a high posterior and a wide margin AND a second
    # corroborating channel: a single channel never yields high confidence, which
    # is exactly how a bare cosine of 0.76 reached the wiki before.
    auto_high_pmin: float = 0.90
    auto_high_margin: float = 0.50
    #: A non-voice channel this strong counts as corroboration ...
    auto_high_min_channel_lr: float = 1.5
    #: ... or the voice channel on its own, if it is this decisive.
    auto_high_min_voice_lr: float = 3.0
    #: Below this the utterance is unresolved rather than merely uncertain.
    auto_low_pmin: float = 0.60
    # propagated is stricter than auto_high on purpose: it generalises from an
    # answer instead of observing the moment, so a later contradiction must be
    # traceable to a propagation rule rather than to the DM.
    propagated_pmin: float = 0.95
    propagated_margin: float = 0.75

    # --- review (S9, S11) ---------------------------------------------------
    #: No question worth less than this is ever asked (bits of global entropy).
    gain_floor_bits: float = 0.15
    #: Stakes-weighted share of unresolved speech the review tolerates.
    target_unresolved: float = 0.10
    max_questions: int = 8
    #: Every extracted event at least this important must have an actor before
    #: the review may stop.
    event_stakes_threshold: float = 0.7
    #: Measured "I don't know" rate per question kind decays that kind.
    uninformative_penalty: float = 1.0
    #: Redundancy penalty against already-asked questions.
    overlap_penalty: float = 0.5
    #: Two consecutive "I don't know" answers end the review.
    max_consecutive_idk: int = 2
    #: Ranking cost controls. The expected gain is MEASURED - one propagation
    #: per option per candidate - and a four-hour session offers hundreds of
    #: candidates, so the ranking simulates a shortlist instead of everything.
    #: Raise these for more thorough ranking at the cost of a slower pass; see
    #: app.ranking.shortlist for what the shortlist gives up.
    simulation_budget: int = 24
    max_simulated_options: int = 3

    # --- inference (S4.3, S4.4) ---------------------------------------------
    #: Damped loopy BP sweeps before giving up and switching to mean field.
    bp_sweeps: int = 10
    bp_max_sweeps: int = 30
    bp_damping: float = 0.5
    #: Within a voice identity, connect each utterance to its k nearest
    #: neighbours in embedding space instead of to all pairs.
    same_voice_neighbours: int = 8
    #: Coupling eps of a same-voice edge whose identity was MEASURED to be one
    #: person (purity 1.0). An identity whose purity was never measured gets the
    #: neutral 0.5 instead, because an unmeasured identity is not a verified
    #: one - see app.inference.identity_coupling_eps for why that distinction
    #: decides whether a session keeps its seven voices or collapses onto one.
    same_voice_eps: float = 0.05
    #: Ranking runs on the highest-stakes utterances plus everything above the
    #: entropy floor; exact inference still runs over the full set.
    surrogate_utterances: int = 400
    #: An utterance addressed to a name expects a reply within this window.
    handoff_sec: float = 12.0

    # --- structure (S5) -----------------------------------------------------
    purity_split_threshold: float = 0.80
    purity_margin: float = 0.10
    purity_min_members: int = 3
    purity_min_speech_sec: float = 10.0
    #: Sliding window (seconds) used to re-diarize an impure identity's span.
    local_rediarize_window: float = 1.5
    #: Padding either side of that span.
    local_rediarize_pad: float = 2.0

    # --- evidence pass (S6) -------------------------------------------------
    evidence_chunk_tokens: int = 8000
    evidence_max_items_per_chunk: int = 40
    evidence_model: str = ""  # empty -> llm_model
    evidence_temperature: float = 0.0
    evidence_json_retries: int = 1
    evidence_concurrency: int = 4
    # KEEP IN SYNC with evidence.PROMPT_VERSION: this is the value REPORTED by
    # /health and used to spot a deployment running a prompt the code no longer
    # ships (the same rule refiner-service and content-service follow).
    prompt_version: str = "attr-ev-v2"

    # --- calibration (S7.2) -------------------------------------------------
    calibration_min_labels: int = 300
    calibration_history_sessions: int = 50

    # --- LLM (LiteLLM; same provider config as content-service) -------------
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek/deepseek-chat"
    deepseek_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    ollama_base_url: str = ""

    # --- internal service APIs ---------------------------------------------
    session_service_url: str = "http://localhost:8003"
    campaign_service_url: str = "http://localhost:8002"
    speaker_service_url: str = "http://localhost:8005"
    wiki_service_url: str = "http://localhost:8007"
    service_timeout_sec: float = 30.0

    # --- MinIO --------------------------------------------------------------
    minio_transcripts_bucket: str = "transcripts"

    @property
    def effective_evidence_model(self) -> str:
        return self.evidence_model or self.llm_model


@lru_cache
def get_settings() -> ServiceSettings:
    return ServiceSettings()
