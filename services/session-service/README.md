# session-service

Recording upload and the **session state machine** — the entry point of the
whole DnD Wiki pipeline. A phone app uploads one raw audio file per session;
the service validates, hashes and streams it to MinIO, then publishes
`session.recorded` so the WhisperX pipeline can start.

## Responsibilities

- `POST /api/sessions` — create a session for a campaign (membership checked
  against campaign-service).
- `PUT /api/sessions/{id}/recording` — multipart audio upload; mime + size
  validation (`MAX_UPLOAD_MB`), SHA-256 checksum, streaming PUT to MinIO
  (`recordings/` bucket), optional client-supplied `duration_sec`.
- On upload completion: `status = recorded`, row in `session_recordings`,
  publish **`session.recorded`** (docs/event-contracts.md).
- `GET /api/sessions?campaign_id=` / `GET /api/sessions/{id}` — session list
  and detail with short-lived presigned URLs for audio/transcript/diarization.
- Speaker assignments: workers write them via the internal API; the DM names
  unknown speakers by campaign member (emits **`speakers.assigned`**). Members
  without a linked user account are fully supported — their voice is labeled
  (the event carries the member display name) but not voiceprint-enrolled,
  since enrollment keys on a user.
- Track pipeline progress: workers move `sessions.status` through the state
  machine via internal endpoints; the UI polls `GET /api/sessions/{id}`.

### Public API (JWT: `player` or `dm`)

| Method | Path | Notes |
|---|---|---|
| POST | `/api/sessions` | body `{campaign_id, title?, session_no?}` |
| GET | `/api/sessions?campaign_id=` | member-only list |
| GET | `/api/sessions/{id}` | detail + presigned media URLs |
| PUT | `/api/sessions/{id}/recording` | multipart `file` + optional `duration_sec` |
| PATCH | `/api/sessions/{id}` | DM: edit title / session_no |
| GET | `/api/sessions/{id}/speakers` | speaker assignments |
| POST | `/api/sessions/{id}/speakers/{label}/assign` | DM names a speaker by campaign member `{member_id}` (userless players allowed) |

### Internal API (dnd-services client token)

| Method | Path | Purpose |
|---|---|---|
| PATCH | `/internal/sessions/{id}/status` | validated state-machine transition |
| PATCH | `/internal/sessions/{id}/artifacts` | attach transcript/diarization URIs |
| POST | `/internal/sessions/{id}/speakers` | bulk upsert speaker assignments |

## State machine

`uploaded → recorded → transcribing → transcribed → refining → refined →
identifying_speakers → speakers_identified → (speaker_pending) → summarizing →
summary_ready → generating_wiki → wiki_plan_ready → applying_wiki →
content_ready → reviewed → published` — plus `failed` from any active state.
Enforced in `app/status.py`; invalid transitions return 409.

`summarizing`/`summary_ready` is the **session-summary review layer**:
content-service distills the transcript into a draft summary the DM reviews
and either sends back for a rewrite (`summary_ready → summarizing`) or
confirms (`summary_ready → generating_wiki`).

`generating_wiki`/`wiki_plan_ready`/`applying_wiki` is the **change-set review
layer**: the confirmed summary becomes a set of proposed wiki changes the DM
inspects, edits and confirms; only `applying_wiki` writes them. Nothing
reaches the wiki before that second confirmation.

The pipeline never goes backwards: a session that reached `content_ready` can
only move forward (or be unpublished from `published` back to `reviewed`).
The only re-entry is a failed phase being retried by its redelivered queue
message (`failed → summarizing|generating_wiki|applying_wiki`).

## Owns

- PostgreSQL database `dnd_sessions` (sessions, session_recordings,
  speaker_assignments) — schema managed by Alembic (`alembic upgrade head`).
- MinIO bucket `recordings` (raw audio); reads `transcripts` for presigned URLs.

## Local dev

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -e libs/python/dnd_common -e "services/session-service[dev]"
cd services/session-service
uv run alembic upgrade head          # against docker-compose Postgres
uv run uvicorn app.main:app --reload --port 8003
```

Tests and lint:

```bash
uv run pytest                        # unit + API tests (SQLite, all externals faked)
uv run ruff check .
```

Integration smoke test (real Postgres + MinIO + RabbitMQ):

```bash
docker compose up -d postgres rabbitmq minio minio-init
cd services/session-service && alembic upgrade head
python scripts/smoke_integration.py  # upload → MinIO round-trip → session.recorded on the broker
```

## Layout

```
app/
  api/           public + internal routers
  clients/       campaign-service membership client (service token)
  services/      business logic (state machine, upload, assignments)
  storage.py     MinIO streaming upload + presigned URLs (aioboto3)
  broker.py      RabbitMQ publisher (robust connection, topology declared)
  models.py      SQLAlchemy models (sessions, session_recordings, speaker_assignments)
  schemas.py     Pydantic request/response models
  status.py      session pipeline state machine
  deps.py        service-token auth + app.state accessors
migrations/      Alembic (initial: 0001)
scripts/         smoke_integration.py
tests/           pytest suite (37 tests)
```

## TODO

- [ ] Status webhook/WebSocket for live progress in the UI
- [ ] Presigned PUT path so the phone uploads straight to MinIO (server only validates)
- [ ] `failed` → retry endpoint (workers currently retry without changing status)
