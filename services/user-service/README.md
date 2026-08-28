# user-service

Users, profiles, speaker identities, and **voiceprint enrollment**.

## Responsibilities

- Profile CRUD (display name, avatar) linked to the Keycloak subject. Rows are
  **lazy-provisioned** from JWT claims on first access — no signup API here
  (Keycloak owns accounts).
- Voiceprint enrollment: the DM triggers the flow, a member records 10–30 s of
  speech (mobile app) → the clip is stored in MinIO `voice-samples` →
  ECAPA-TDNN embedding extracted (SpeechBrain) → upserted into Qdrant
  collection `voiceprints` (payload: `user_id`, `campaign_id`, `sample_uri`,
  `version`).
- Voiceprint queries used by speaker-service for matching.

## Owns

- PostgreSQL database `dnd_users` (`users`, `voice_profiles`)
- Qdrant collection `voiceprints` (192-d, cosine)
- MinIO buckets `voice-samples` (enrollment clips) + `wiki-assets/avatars/`

## Identity convention

`users.id` **equals the Keycloak subject** (a UUID by default). This is the
`user_id` that other services store in foreign keys (e.g.
`campaign_members.user_id`), so rosters and voiceprint status join cleanly.
`users.keycloak_sub` is kept as a redundant lookup column per the data model.

If a Keycloak account is deleted and re-registered with the same email (new
subject, Keycloak never reuses subjects), `get_or_create_user` adopts the
existing row and re-links `keycloak_sub` to the new subject: the id - and
every `user_id` foreign key in other services - stays stable, so profiles and
voiceprints survive. `/internal/users` accepts both the id and the current
subject as identifiers; the public voice endpoints (`/api/voice/profiles`,
DELETE) resolve the current JWT sub to the stable row id before scoping, so
re-registered accounts still see and manage their own voiceprints.

## API

Public (behind the gateway, JWT):

| Method | Path | Notes |
|---|---|---|
| GET | `/api/users/me` | own profile; provisions the user row on first access |
| PATCH | `/api/users/me` | edit `display_name` (email is Keycloak-managed) |
| PUT | `/api/users/me/avatar` | multipart image → `wiki-assets/avatars/<id>.<ext>` |
| GET | `/api/users/search?q=&limit=` | find users by display name/email (email stays private); used by the DM to link a campaign member to an account |
| GET | `/api/users/{user_id}` | public profile (no email) for rosters/attribution |
| POST | `/api/voice/enroll` | multipart `campaign_id` + audio file; self-enrollment (membership checked against campaign-service) |
| GET | `/api/voice/profiles?campaign_id=` | the caller's own voiceprints (with presigned sample URLs) |
| DELETE | `/api/voice/profiles/{id}` | remove own voiceprint (also deletes the Qdrant point) |

Internal (service-to-service, `dnd-services` token):

| Method | Path | Notes |
|---|---|---|
| GET | `/internal/users?ids=...` | profile summaries (display name/email/avatar) for foreign keys |
| GET | `/internal/voice-profiles?campaign_id=` | enrollment status per campaign (DM console) |

## Local dev

```bash
pip install -e libs/python/dnd_common
pip install -e "services/user-service[ml]"   # [ml] adds speechbrain/torch for enrollment
uvicorn app.main:app --reload --port 8001
```

Without the `[ml]` extras the service runs and serves profile APIs; enrollment
returns 503 until speechbrain is installed.

```bash
pytest            # from services/user-service (integrations are faked)
ruff check .      # lint
```

## Configuration (env, see dnd_common.config.Settings)

| Variable | Default | Purpose |
|---|---|---|
| `SERVICE_DB_NAME` | `dnd_users` | this service's Postgres database |
| `CAMPAIGN_SERVICE_URL` | `http://localhost:8002` | membership checks before enrollment |
| `VOICE_EMBEDDING_MODEL` | `speechbrain/spkrec-ecapa-voxceleb` | ECAPA-TDNN model |
| `QDRANT_URL` | `http://localhost:6333` | voiceprints collection |
| `MINIO_*` | dev defaults | voice samples + avatars storage |

## TODO

- [ ] Extract the SpeechBrain embedder to `dnd_common` (shared with
      speaker-service, which currently only has a sketch in its worker).
- [ ] DM-facing list of voiceprint status per campaign roster (join
      campaign-service members with `/internal/voice-profiles`).
- [ ] Voiceprint duration guidance surfaced to the mobile app (10–30 s).
