# wiki-service

The content heart of the platform: wiki pages, versions, cross-references,
visibility, and DM approval. **All permission enforcement for content lives
here** — queries are always scoped by `campaign_id` and filtered by the
caller's role (checked against campaign-service).

## Responsibilities

- Page CRUD by kind (character/location/faction/item/quest — the five wiki
  categories; events/session recaps live on the session page, events later on
  the timeline view). Each kind has a validated `attributes` schema in
  `app/page_attributes.py` (e.g. character `npc|player`, location geospatial
  fields); unknown attribute keys are rejected with a 422.
- Draft review loop: `draft → published | archived` (DM only). There is no
  'pending review' status (migration 0005 folded the legacy rows into drafts).
  Pages the pipeline generates do NOT go through it: they are written by the
  internal apply endpoint of a change set the DM already confirmed, and land
  **published** (see below).
- Visibility: `public` (players see it) / `dm_only` / `hidden` (nobody but DM).
- Versioning (`page_versions`, per-page 1-based `version_no`) and proposed
  relations (`page_relations`).
- Timelines (`timeline_events`) with in-world dates.
- Publishes `wiki.published` / `wiki.updated` / `wiki.archived` → search +
  notifications, and `wiki.draft_ready` when a draft page is created through
  the public API.

## Permission rules

- Players: read `published` + `public` pages of their campaign only.
- DM: everything in their campaign, including drafts and hidden pages.
- Enforcement: JWT roles (Keycloak) + campaign membership (campaign-service)
  + `page.visibility` filter — every query carries `campaign_id`.

## API surface (`/api/wiki`, JWT-authenticated)

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/api/wiki/pages?campaign_id=…` | members | players: published+public only; `kind`, `status` (DM), `q`, `limit`/`offset` |
| POST | `/api/wiki/pages` | DM **or** service token | service tokens may only create `draft` pages |
| GET | `/api/wiki/pages/{id}` | members | visibility-filtered |
| PATCH | `/api/wiki/pages/{id}` | DM | content edit writes a version; published → `wiki.updated` |
| POST | `/api/wiki/pages/{id}/approve` | DM | `draft → published`, emits `wiki.published` |
| POST | `/api/wiki/pages/{id}/archive` | DM | emits `wiki.archived`; terminal in v1 |
| PATCH | `/api/wiki/pages/{id}/visibility` | DM | `public|dm_only|hidden` |
| GET | `/api/wiki/pages/{id}/versions` | members | newest first |
| GET/POST/DELETE | `/api/wiki/pages/{id}/relations` | read: members, write: DM | |
| GET/POST | `/api/wiki/timeline` | GET: members, POST: DM | players see approved events only |
| PATCH | `/api/wiki/timeline/{id}` | DM | edit/approve |

## Internal API (`/internal/wiki`, dnd-services token)

| Method | Path | Purpose |
|---|---|---|
| GET | `/internal/wiki/pages?campaign_id=` | flat page listing (content-service dedupe input) |
| GET | `/internal/wiki/timeline?campaign_id=` | every timeline entry, approved or not |
| POST | `/internal/wiki/timeline/upsert` | create/refresh the entry of an event page (created pending) |
| POST | `/internal/wiki/changes/apply` | **write a DM-confirmed change set** |
| GET | `/internal/wiki/sessions/{id}/content` | pages + timeline entries a session wrote (deletion guard for session-service) |

`POST /internal/wiki/changes/apply` is the single path that publishes
pipeline-generated content: content-service calls it after the DM confirmed
the proposed changes on the session page, so the new pages are created
`published` (the confirmation IS the approval — no "pending review" step) and
new timeline entries `approved`, while an existing entry keeps whatever
approval state the DM gave it. It is idempotent: a create whose page the
campaign already documents (same kind + title) or that this session already
wrote is skipped and reported, so a retried message never duplicates a page.

## Events published

- `wiki.published` / `wiki.updated` / `wiki.archived` — payload
  `{page_id, campaign_id, slug, kind, visibility}` (search-service indexes).
- `wiki.draft_ready` — payload `{campaign_id, session_id, draft_count}`
  (notification-service pings the DM). v1 emits one event per draft
  (`draft_count: 1`); batching per generation job is future work.

## Owns

- PostgreSQL database `dnd_wiki` (wiki_pages, page_versions, page_relations,
  timeline_events)
- MinIO bucket `wiki-assets` (images, handouts)

## Local dev

```bash
pip install -e libs/python/dnd_common
pip install -e services/wiki-service
alembic -c services/wiki-service/alembic.ini upgrade head   # from repo root
uvicorn app.main:app --reload --port 8007                   # from services/wiki-service
```

Tests (from `services/wiki-service`):

```bash
python -m pytest
ruff check .
ruff format --check .
```

## TODO / future work

- [ ] Batch `wiki.draft_ready` (one event per content-service generation job)
- [ ] Page "unarchive" / status rollback (v1 treats `archived` as terminal)
- [ ] LLM-proposed timeline events (`approved=false`) via content-service
- [ ] Media handling for the `wiki-assets` bucket (presigned uploads, image blocks)
- [ ] Full-text `q` on content_json (v1 searches titles only)
