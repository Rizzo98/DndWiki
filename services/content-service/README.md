# content-service

LLM worker: turns the **named transcript** into structured wiki drafts
(characters and locations only - events/session recaps live on the session
page) that the DM then reviews.

## Pipeline (per session)

1. Consume `content.generate` (fires on `speakers.identified` / `speakers.assigned`).
   Sessions with `pending_assignment` are skipped — generation happens once the
   DM has named every speaker.
2. Move the session to `generating_wiki` via the session-service internal API
   (a 409 = another delivery already handled it -> ack, no-op).
3. Download the named transcript from MinIO (`transcripts/<session_id>/transcript.json`),
   resolve speaker labels preferring member CHARACTER names (party members are
   labeled by their character, never the player name; user-service resolves
   the rest) and build a compact `[HH:MM:SS] NAME: text` view prefixed with
   the party's character names.
4. Split into overlapping chunks (~`CHUNK_TOKENS` tokens, `CHUNK_OVERLAP` overlap,
   never splitting a segment).
5. For each chunk, call the LLM (via **LiteLLM**) with a strict JSON schema
   (`app/prompts.py`, `PROMPT_VERSION=v8`): transcript language, session
   summary, characters (name, aliases, description, durable facts,
   session-specific facts, `is_party`, mentions), locations (+ `place_type`,
   `part_of` geospatial hints and the v8 type-specific detail fields:
   population/government/districts for settlements, terrain/climate/capital
   for regions, pantheon/planes for worlds, owner/purpose for buildings,
   entrance/levels/hazards for dungeons, flora/fauna for wilderness, plus the
   narrative `history` section), events, timeline entries. The prompt enforces
   the players' language for every output field and proper-name-only entities
   (no "the city" / "città" pages). Calls run in parallel
   (`LLM_CHUNK_CONCURRENCY`). Malformed LLM output is repaired locally
   (`json-repair`: missing/trailing commas, fences, ...) and, if that fails,
   re-asked once per `LLM_JSON_RETRIES` with the parse error appended.
6. Merge across chunks (`app/merger.py`): dedupe entities by normalized name,
   longest description wins, facts/aliases/mentions union (durable `facts` kept
   separate from `session_facts`), majority-vote language, drop whole-generic
   names ("città", "the city"), per-entity confidence = fraction of chunks that
   mentioned it, overall confidence = mean. The merged result is persisted in
   `session_summaries` (one row per session), so the session page can show the
   summary even if draft creation fails.
7. Create draft pages through **wiki-service**
   (`POST /api/wiki/pages`, `status=pending_review`) with a confidence score,
   `source_session_id` and validated per-kind `attributes`: characters always
   carry `character_type: player|npc` (matched against the party's member
   character names or the model's `is_party` hint); locations get
   `location_type`/`region` plus the type-specific detail fields the chunk
   supported (scoped to the mapped type, so a draft is never rejected by the
   wiki-service schema), and the narrative `history` section when the chunk
   recounted the place's past. Cross-session
   dedupe: entities already documented by a campaign page (exact title/alias
   match) are not re-drafted — their new facts remain visible on the persisted
   session summary shown on the session page; fuzzy look-alikes still get a
   draft plus a `possible_duplicate` relation proposal.
8. Record a `generation_jobs` row (provider, model, `prompt_version`, draft ids,
   confidence), move the session to `content_ready`, and publish
   `content.generated` -> the DM gets a `wiki.draft_ready` notification.

On failure the session and the job are marked `failed` (error recorded) and the
message re-raises; redeliveries are acked as no-ops via the state-machine 409,
so a failed job is never re-run or DLQ-spammed.

## Configuration

| Env | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `deepseek` | provider name recorded on jobs/summaries |
| `LLM_MODEL` | `deepseek/deepseek-chat` | any LiteLLM `provider/model` id |
| `LLM_TEMPERATURE` | 0.2 | low for extraction |
| `LLM_MAX_TOKENS` | 4096 | per-chunk completion cap |
| `LLM_CHUNK_CONCURRENCY` | 4 | parallel per-chunk LLM calls |
| `LLM_JSON_RETRIES` | 1 | corrective retries per chunk on malformed JSON (0 = off) |
| `PROMPT_VERSION` | `v8` | version of `app/prompts.py`, recorded per job |
| `CHUNK_TOKENS` | 4000 | target chunk size (char/4 estimate) |
| `CHUNK_OVERLAP` | 0.1 | fraction of chunk re-seen by the next one |
| `MAX_CHUNKS_PER_SESSION` | 16 | safety cap; beyond this the job fails |
| `DEEPSEEK_API_KEY` | — | placeholder until configured |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | — | provider keys (alternative providers) |
| `OLLAMA_BASE_URL` | — | self-hosted option |
| `WIKI_SERVICE_URL` | `http://localhost:8007` | internal draft API |
| `SESSION_SERVICE_URL` | `http://localhost:8003` | internal state machine |
| `USER_SERVICE_URL` | `http://localhost:8001` | display-name resolution |
| `CAMPAIGN_SERVICE_URL` | `http://localhost:8002` | DM authorization (debug regenerate) |

## Debug regenerate

`POST /api/content/sessions/{id}/regenerate` re-runs wiki generation from the
SAME transcript: it rebuilds the speaker map from session-service, republishes
a synthetic `speakers.identified` event (the only consumer is this service's
queue), and lets the normal pipeline produce fresh v2 drafts. Allowed from
`content_ready` / `reviewed` sessions with all speakers named. Authorization:
campaign DM, or any user with the Keycloak `dev` realm role.
The web UI exposes it as a "Debug: rerun generation" button on the session
summary card for users carrying that role.

> **Provider switching:** the provider is selected by the `LLM_MODEL` prefix
> (LiteLLM convention). Keys are exported to the env vars LiteLLM expects
> (e.g. `DEEPSEEK_API_KEY`); to use another provider, set `LLM_MODEL` to
> e.g. `openai/gpt-4o-mini` and fill the matching `*_API_KEY`.

## Local dev

```bash
uv pip install --python .venv/Scripts/python.exe -e libs/python/dnd_common
uv pip install --python .venv/Scripts/python.exe -e services/content-service
python -m app.workers.generate            # worker
uvicorn app.main:app --reload --port 8006 # API
```

Tests (no LLM/MinIO/RabbitMQ needed — storage, clients and the LLM are fakes):

```bash
cd services/content-service && pytest
```

## TODO

- [ ] `speakers.assigned` carries only the last-named speaker; a future
      session-service endpoint could return the full label -> user map so the
      transcript view is fully named on the assigned path too
- [ ] Tokenizer-based chunk sizing (tiktoken) instead of the char/4 estimate
