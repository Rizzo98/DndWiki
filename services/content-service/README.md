# content-service

LLM worker: turns the **named transcript** into a **reviewable session
summary** and — once the DM confirms it — into the structured wiki drafts
(characters and locations; events/session recaps live on the session page and
on the campaign timeline) the DM then reviews.

The session summary is an **intermediate layer of the pipeline**, not a
by-product: nothing is written to the wiki before the DM has confirmed it.

## Pipeline (per session)

### Phase 1 — draft summary (`speakers.identified` / `speakers.assigned`)

1. Skip sessions with `pending_assignment` (generation happens once the DM
   has named every speaker) and ignore late `speakers.assigned` events for a
   session that is already past the summary phase — a speaker rename must
   never throw away a summary the DM is reviewing.
2. Move the session to `summarizing` via the session-service internal API
   (a 409 = another delivery already handled it → ack, no-op).
3. Download the named transcript from MinIO
   (`transcripts/<session_id>/transcript.json`), resolve speaker labels
   preferring member CHARACTER names (party members are labeled by their
   character, never the player name; user-service resolves the rest) and build
   a compact `[HH:MM:SS] NAME: text` view prefixed with the party's character
   names.
4. Split into overlapping chunks (~`CHUNK_TOKENS` tokens, `CHUNK_OVERLAP`
   overlap, never splitting a segment).
5. For each chunk, call the LLM (via **LiteLLM**) with a strict JSON schema
   (`app/prompts.py`, `PROMPT_VERSION=v11`): transcript language, session
   summary, characters (name, aliases, description, durable facts,
   session-specific facts, `is_party`, mentions), locations (+ `place_type`,
   `part_of` geospatial hints and the v8 type-specific detail fields:
   population/government/districts for settlements, terrain/climate/capital
   for regions, pantheon/planes for worlds, owner/purpose for buildings,
   entrance/levels/hazards for dungeons, flora/fauna for wilderness, plus the
   narrative `history` section), events, timeline entries. The prompt enforces
   the players' language for every output field, proper-name-only entities
   (no "the city" / "città" pages) and — since v11 — a summary written as
   **1-3 short lines per chunk, one beat per line**. Calls run in parallel
   (`LLM_CHUNK_CONCURRENCY`). Malformed LLM output is repaired locally
   (`json-repair`: missing/trailing commas, fences, ...) and, if that fails,
   re-asked once per `LLM_JSON_RETRIES` with the parse error appended.
6. Merge across chunks (`app/merger.py`): dedupe entities by normalized name,
   longest description wins, facts/aliases/mentions union (durable `facts`
   kept separate from `session_facts`), majority-vote language, drop
   whole-generic names ("città", "the city"), per-entity confidence =
   fraction of chunks that mentioned it, overall confidence = mean. The
   chunks' summary lines are concatenated in order and de-duplicated
   (overlapping chunks repeat beats) — that is the text the DM reviews, so it
   is stored **one line per beat**.
7. Persist the draft in `session_summaries` (`review_status='draft'`,
   `revision=1`, plus the language and the party character names the wiki
   phase will need), record the `generation_jobs` row (`phase='summary'`),
   move the session to `summary_ready` and publish
   **`content.summary.drafted`**. **No page, no event and no timeline entry is
   created here.**

### Phase 1b — summary rewrite with the DM's feedback (`summary.regenerate`)

The DM opens the session page, ticks the summary lines that are wrong (or
none, for the whole summary) and describes the change ("It wasn't Character A,
it was Character B"). `POST /api/content/sessions/{id}/summary/regenerate`
queues that feedback on the same `content.generate` queue:

8. The worker moves the session back to `summarizing`, loads the persisted
   extraction and makes **one LLM call** with `SUMMARY_REVISION_PROMPT` — the
   model receives the whole extraction plus the correction requests and
   returns the corrected object, so a fixed attribution propagates to the
   summary lines, the entities, the events **and** the timeline entries at
   once.
9. The rewrite is persisted as the next revision (`revision + 1`, the
   feedback appended to `edit_history`, `review_status` back to `draft`),
   the per-entity confidences of the previous extraction are re-attached
   (the model cannot recompute them, a rewrite must not reset every badge),
   the session returns to `summary_ready` and `content.summary.drafted` is
   published with the new revision. The DM can iterate as many times as they
   want.

### Phase 2 — confirmed summary → PROPOSED changes (`summary.confirmed`)

10. `POST /api/content/sessions/{id}/summary/confirm` stamps the summary
    (`review_status='confirmed'`, `confirmed_at/by`) and publishes
    `summary.confirmed`; the worker moves the session to `generating_wiki`
    and turns the confirmed summary into a **change set**
    (`app/planner.py` → `wiki_change_sets`), one entry per page to create,
    page to update and timeline entry to write, plus the cross-references
    proposed between them. Each entry carries the payload that would be
    written (`after`), and updates also carry the page's CURRENT title and
    `content_json` (`before`) so the session page can render a per-field
    diff — the "git status" of the session. **Still nothing is written to the
    wiki.** Cross-session dedupe happens here: an entity the campaign already
    documents (exact title/alias match) is reported as *skipped*, never
    proposed again; fuzzy look-alikes get a change plus a
    `possible_duplicate` link.
11. The run is recorded (`phase='wiki'`, no draft ids), the session parks on
    `wiki_plan_ready` and `content.plan.ready` is published. The DM now
    inspects/edits/drops single changes (`PUT /api/content/sessions/{id}/plan`)
    — title, any content field, the event's timeline entry, its visibility.

### Phase 3 — confirmed changes → wiki (`plan.confirmed`)

12. `POST /api/content/sessions/{id}/plan/confirm` publishes
    `plan.confirmed`; the worker moves the session to `applying_wiki` and
    applies the confirmed set through **wiki-service**
    (`POST /internal/wiki/changes/apply`): the new pages are created
    **published** and new timeline entries **approved**, the existing event
    pages are updated in place (never duplicated) and the proposed relations
    are created. The apply endpoint is idempotent: a create the campaign
    already documents (or one this session already wrote) is skipped and
    reported, so a retried message cannot duplicate a page.
13. The run is recorded (`phase='apply'`, the written page ids, mean
    confidence), the session moves to `content_ready` and
    `content.generated` is published with `created`/`updated`/`skipped`
    per change.

A failed apply puts the change set **back in review** (`status='draft'`, the
error kept on the row): the DM fixes the offending entry and confirms again —
the reviewed proposal is never thrown away. The summary phase is never re-run
for a session that already generated its wiki, and a session that reached
`content_ready` can only move forward (see services/session-service/app/status.py).

On failure the session and the job are marked `failed` (error recorded) and
the message re-raises; redeliveries re-run the phase (`failed ->
summarizing|generating_wiki|applying_wiki`) or are acked as no-ops via the
state-machine 409, so a failed job is never re-run blindly or DLQ-spammed.

## Configuration

| Env | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `deepseek` | provider name recorded on jobs/summaries |
| `LLM_MODEL` | `deepseek/deepseek-chat` | any LiteLLM `provider/model` id |
| `LLM_TEMPERATURE` | 0.2 | low for extraction |
| `LLM_MAX_TOKENS` | 4096 | per-call completion cap |
| `LLM_CHUNK_CONCURRENCY` | 4 | parallel per-chunk LLM calls |
| `LLM_JSON_RETRIES` | 1 | corrective retries per call on malformed JSON (0 = off) |
| `PROMPT_VERSION` | `v11` | version of `app/prompts.py`, recorded per job |
| `CHUNK_TOKENS` | 4000 | target chunk size (char/4 estimate) |
| `CHUNK_OVERLAP` | 0.1 | fraction of chunk re-seen by the next one |
| `MAX_CHUNKS_PER_SESSION` | 16 | safety cap; beyond this the job fails |
| `DEEPSEEK_API_KEY` | — | placeholder until configured |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | — | provider keys (alternative providers) |
| `OLLAMA_BASE_URL` | — | self-hosted option |
| `WIKI_SERVICE_URL` | `http://localhost:8007` | internal draft API |
| `SESSION_SERVICE_URL` | `http://localhost:8003` | internal state machine |
| `USER_SERVICE_URL` | `http://localhost:8001` | display-name resolution |
| `CAMPAIGN_SERVICE_URL` | `http://localhost:8002` | DM authorization |

## API

| Method | Path | Who | Purpose |
|---|---|---|---|
| GET | `/api/content/summaries/{id}` | any campaign member | the stored summary: lines, entities, events, `review_status`, `revision` |
| GET | `/api/content/jobs/{id}` | any campaign member | newest generation run (`phase`, status, draft ids) |
| GET | `/api/content/llm` | any user | provider/model/prompt version of this deployment |
| POST | `/api/content/sessions/{id}/summary/regenerate` | DM or `dev` | rewrite the draft summary from the DM's feedback |
| POST | `/api/content/sessions/{id}/summary/confirm` | DM or `dev` | confirm the summary → propose the wiki changes |
| GET | `/api/content/sessions/{id}/plan` | DM or `dev` | the proposed changes (with the per-field diff of every update) |
| PUT | `/api/content/sessions/{id}/plan` | DM or `dev` | save the review: edited payloads, dropped changes, dropped links |
| POST | `/api/content/sessions/{id}/plan/confirm` | DM or `dev` | confirm the changes → write the pages/events (also the retry for a failed apply) |

The endpoints only publish events; the state-machine transition (and its 409
idempotency guard) happens in the worker, the single writer of
`sessions.status`. The only exception is the confirmation STAMP of each review
layer (summary and change set), written by the endpoint because it is the DM's
act — and each generation phase refuses to start without it.

The web session page is the review UI, in two steps: (1) tick the summary
lines, describe the change, *Regenerate*, then *Confirm summary*; (2) inspect
the proposed changes — creations as a preview, updates as a field-by-field
diff — edit or drop what is wrong, then *Confirm changes & update the wiki*.

### Why there is no "re-generate from scratch" button

The pipeline never goes backwards for a session that already produced wiki
content: re-running the summary phase would re-materialize entities and events
the campaign already documents, creating duplicates and conflicts with the
curated pages. A session therefore enters the summary phase exactly once (when
speakers are identified), and the only re-entry is the automatic retry of a
FAILED phase by the redelivered queue message. Everything else — correcting
the AI — happens on the DRAFT summary, before anything reaches the wiki, via
the two review endpoints above.

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
- [ ] Notify the DM (`notification-service`) when a draft summary is ready and
      when a rewrite finishes (currently only `content.summary.drafted` is
      published)
