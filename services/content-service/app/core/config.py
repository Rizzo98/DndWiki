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
    # Output cap for one call. Measured: an ordinary chunk of a real session
    # answers with ~2,000 tokens (a 7 kB JSON object), but the SAME chunk answers
    # with more than 4,096 on some samples - the answer grows by sampling, not by
    # chunk size - and a cut-off answer is never repaired, so the whole session
    # failed. Across two measured arms, S1E1's middle chunk was truncated in 7 runs
    # out of 8 at 4,096 and produced a normal answer at 8,192. 8,192 is the
    # provider's own output ceiling, so this is the strongest setting available
    # rather than a preference (LLM_MAX_TOKENS overrides it).
    llm_max_tokens: int = 8192
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
    # v16 partitions the BEATS across the overlapping chunks: the overlap is for
    # the entities, and letting it drive the story made two chunks narrate one
    # moment in their own words (chunking.owned_ranges).
    # v17 makes the beat budget proportional to the part a chunk owns, because a
    # flat "1-3 lines" made a chunk leave out the last scene of its part
    # (chunking.beat_budget).
    # v18 carries the span each beat came from into the compose call, so it can
    # refuse to write two beats from different parts of the session as one scene
    # (merger.summary_beats), and stops the record's English place reading from
    # being copied into a block label.
    # v19 documents BOTH input forms in the view contract. It used to describe
    # only the attributed view ([u_00412 ...]), while a diarized transcript
    # renders [HH:MM:SS] SPEAKER_02: - so the model was asked to cite [u_XXXXX]
    # ids that do not exist there and answered by enumerating INVENTED ones until
    # it ran out of output tokens, failing the session. v19 also says what a
    # diarization label is: a voice, nameable only from the transcript's own lines.
    prompt_version: str = "v26"
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

    # --- diarized input: who is who -------------------------------------------
    # A transcript that reached generation without speaker identification carries
    # diarization LABELS (SPEAKER_00..), not names: every chunk then decides for
    # itself who is who, and on a real session that produced one NPC under three
    # names and the wrong name on every beat a voice spoke in. With this on, the
    # session is read ONCE (app/speakers.py) and the reading is handed to every
    # chunk as a '[Cast]' note. It is a reading, not identification: it may name
    # nobody, it never fails the job, and it costs one call per session.
    cast_reading_enabled: bool = True

    # --- the campaign's roster -------------------------------------------------
    # The roster is campaign data (member -> character, and who is the DM): it is
    # fetched at run time and used for THREE things - the party line, excluding the
    # people at the table from the drafted characters, and rewriting prose that
    # used a player's name for their character. Those are correctness fixes with a
    # measured result (evals/fixtures/*/README.md).
    #
    # THIS FLAG controls only the fourth thing: whether the pairs are also shown to
    # the extraction as a '[Table]' note. That is the part that could HURT - a
    # closed list of character names invites a model to assign them to voices it
    # cannot identify, which is the failure that made the cast reading hand over
    # narrators only (app/speakers.py). Set it false to measure the roster's data
    # without its prompt.
    roster_note_enabled: bool = True

    # --- the finished record: how long it should be --------------------------
    # A second call over the COMPOSED summary, asked for the same record at this
    # many sentences; 0 turns it off. The composer both selects and writes, and it
    # will not drop content it decided to keep - measured twice, a sentence budget
    # in the compose prompt shortened the draft and lost benchmark facts (100%
    # coverage down to 82%), and cutting the beats instead costs coverage AND
    # accuracy (evals/fixtures/*/README.md). This pass sees only the finished text,
    # so what it can remove is the connective tissue between the facts.
    #
    # Measured over three runs an arm per fixture, off against 14:
    #   S1E1  sentences 16.7 -> 16.0, facts missing 0.3 -> 0.3, contradicted [0,0,7] -> [0,1,5]
    #   S1E2  sentences 17.0 -> 15.3, facts missing 1.0 -> 0.7, contradicted [5,0,6] -> [0,2,0]
    # Denser at no measured cost, and it does NOT reach the published summary's ~10
    # sentences however low the target goes (11 and 14 land in the same place): what
    # is left after the tissue is gone is the facts, and this record carries more of
    # them than the published one does. Costs one call per session.
    summary_sentences_target: int = 14

    # --- transcript -> chunking ---
    # Transcript lines per beat (chunking.beat_budget): how much of the session is
    # compressed into one moment. The composer writes what the beats carry, so this
    # is the only lever on the finished summary's LENGTH - and it is measured, not
    # assumed. Four runs an arm per fixture, 25 lines/beat against 50:
    #
    #            beats      words     sentences   benchmark facts lost   contradicted
    #   S1E1   23.5->21.3  522->485   17.5->14.0        0.8 -> 0.3          3.3 -> 3.0
    #   S1E2     33 -> 25  798->648   25.2->17.8        0.8 -> 1.5          4.5 -> 7.8
    #
    # A quarter fewer beats buys a fifth shorter draft and costs coverage and
    # accuracy where the session is dense. The default stays at 25: the length of
    # this draft is the price of the facts in it. Lower it to trade the other way.
    beat_lines: int = 25
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