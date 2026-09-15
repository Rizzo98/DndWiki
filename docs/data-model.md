# DnD Wiki — Data Model

Logical schema per service (database-per-service). All tables use `BIGSERIAL` /
`UUID` primary keys (`id UUID DEFAULT gen_random_uuid()`), `created_at` /
`updated_at` timestamps, and soft-delete flags where noted.

---

## dnd_users (user-service)

```
users
  id            uuid PK                  -- equals the Keycloak subject at
                                         -- creation; the stable user_id stored
                                         -- in other services' foreign keys
  keycloak_sub  text UNIQUE NOT NULL   -- current Keycloak subject; re-linked if
                                         -- the account is re-registered with
                                         -- the same email
  email         text UNIQUE
  display_name  text NOT NULL
  avatar_uri    text                    -- MinIO wiki-assets/avatars/... (bucket/key)
  created_at    timestamptz

voice_profiles                     -- one row per (user, campaign) voiceprint
  id            uuid PK
  user_id       uuid FK -> users
  campaign_id   uuid                -- voiceprints are campaign-scoped
  qdrant_point  text NOT NULL       -- id of embedding in Qdrant 'voiceprints'
  sample_uri    text NOT NULL       -- MinIO clip used to enroll (voice-samples/...)
  embedding_version int DEFAULT 1
  created_at    timestamptz
  UNIQUE (user_id, campaign_id)
```

Qdrant collection `voiceprints`: vector (192-d ECAPA-TDNN), payload
`{user_id, campaign_id, sample_uri}`.

Speaker-service also adds **session-derived** points when the DM names a
speaker: same vector/payload shape plus `{source: "session", session_id,
speaker_label}`. They are not tracked in `voice_profiles` (that table documents
explicit user enrollment) but are searched alongside it, so a name assigned in
one session is reused automatically in later ones.

While identifying a session, speaker-service additionally backfills the
**DM's namings of earlier sessions** of the same campaign: every confirmed,
user-linked assignment there (`speaker_assignments.status = 'confirmed'`) whose
turn is long enough and confident enough is embedded and stored as a point with
`{source: "history", session_id, speaker_label, history_key, turn_confidence}`.
`history_key` is `session_id#label#window`, so each turn is enrolled exactly
once and later runs reuse it. Only the campaign's own history is ever read —
voiceprints stay per campaign.

---

## dnd_campaigns (campaign-service)

```
campaigns
  id            uuid PK
  name          text NOT NULL
  slug          text UNIQUE NOT NULL
  description   text
  language      text NOT NULL DEFAULT 'en'
                -- session language chosen by the DM at creation
                -- (SUPPORTED_LANGUAGES: en|it|de|fr|es|pt)
  dm_user_id    uuid NOT NULL
  status        text DEFAULT 'active'   -- active | archived
  settings      jsonb DEFAULT '{}'      -- e.g. default page visibility
  created_at    timestamptz

campaign_members
  id             uuid PK                -- surrogate member id (stable
                                        -- regardless of the user link)
  campaign_id    uuid FK -> campaigns
  user_id        uuid NULL              -- optional link to users.id
                                        -- (Keycloak subject); unlinked
                                        -- members need no account
  role           text NOT NULL          -- dm | player
  player_name    text NOT NULL          -- DM-curated player name
  character_name text NOT NULL          -- character the player plays
  character_description text NULL       -- physical description of the
                                        -- character (refiner LLM context)
  joined_at      timestamptz
  UNIQUE (campaign_id, user_id)         -- a user links at most once per
                                        -- campaign; NULLs exempt so many
                                        -- unlinked members can coexist

invites
  id            uuid PK
  campaign_id   uuid FK
  email         text
  role          text DEFAULT 'player'
  token         text UNIQUE
  expires_at    timestamptz
  used_at       timestamptz NULL
```

---

## dnd_sessions (session-service)

```
sessions
  id            uuid PK
  campaign_id   uuid NOT NULL
  title         text
  session_no    int                     -- ordering within campaign
  recorded_at   timestamptz
  status        text NOT NULL DEFAULT 'uploaded'
                -- uploaded|recorded|transcribing|transcribed|refining|refined|
                -- identifying_speakers|speakers_identified|speaker_pending|
                -- summarizing|summary_ready|generating_wiki|wiki_plan_ready|
                -- applying_wiki|content_ready|reviewed|published|failed
  raw_audio_uri text                    -- MinIO recordings/...
  transcript_uri text                   -- MinIO transcripts/... (JSON)
  diarization_uri text                  -- MinIO transcripts/... (segments JSON)
  duration_sec  numeric
  error         text
  created_at    timestamptz
  updated_at    timestamptz

session_recordings
  id            uuid PK
  session_id    uuid FK -> sessions
  uploaded_by   uuid
  file_uri      text
  size_bytes    bigint
  mime          text
  sha256        text
  created_at    timestamptz

speaker_assignments                 -- diarized label -> campaign member per session
                                    -- SUPERSEDED (implemented): replaced by the
                                    -- utterance-level model in
                                    -- docs/attribution-model.md (dnd_attribution).
                                    -- This table is now a DERIVED VIEW: the
                                    -- attribution engine is its only writer, via
                                    -- PUT /internal/sessions/{id}/speaker-assignments,
                                    -- and the old upsert path is retired because it
                                    -- silently downgraded confirmed rows.
  id            uuid PK
  session_id    uuid FK -> sessions
  speaker_label text NOT NULL       -- SPEAKER_00, ...
  member_id     uuid NULL           -- campaign member this voice belongs to
                                    -- (may lack a user account)
  user_id       uuid NULL           -- mirrors member.user_id when linked;
                                    -- voiceprint enrollment/matching keys on
                                    -- this; null until assigned
  confidence    numeric             -- cosine similarity of best match
  status        text DEFAULT 'pending'  -- pending (DM names it) | auto (pipeline
                                    -- proposed it) | confirmed (the DM accepted
                                    -- it: what speaker-service learns voices
                                    -- from, see below)
  assigned_by   uuid NULL
  created_at    timestamptz
  updated_at    timestamptz
  UNIQUE (session_id, speaker_label)
```

`transcript_uri` JSON shape (produced by transcription-service):

```json
{
  "session_id": "...",
  "language": "en",
  "model": "large-v3",
  "segments": [
    {
      "start": 0.0, "end": 4.2, "text": "Welcome back, heroes.",
      "chunk": 0, "speaker": "SPEAKER_00", "speaker_confidence": 0.97, "confidence": 0.97,
      "words": [{"word": "Welcome", "start": 0.0, "end": 0.5}]
    }
  ]
}
```

`chunk` (optional int) marks the source transcription chunk; speaker-service
treats chunk boundaries as hard turn boundaries when it re-clusters raw diarizer
labels. After identification, `speaker` labels are canonical `SPEAKER_XX` values
assigned by the re-clustering or — when the refiner-service LLM contextual pass
ran (REFINER_ENABLED) — by the LLM. Both artifacts gain a top-level `refiner`
object ({enabled, provider, model, prompt_version}) when the refiner rewrites
them; segment `start`/`end`/`chunk` are never changed by refinement.

`speaker_confidence` (optional float 0..1) is the diarization confidence for
the segment, reported by cloud API backends that provide it (Deepgram Nova 3:
direct field or mean of the word-level `speaker_confidence` values). The web
UI flags a speaker label when any of its segments falls below the
low-confidence threshold, so the DM can re-check/re-assign the attribution.
The field is preserved untouched by refiner-service and speaker-service
re-clustering (unknown segment fields pass through).

`confidence` (optional float 0..1) is the ASR text probability per utterance
(AssemblyAI reports it; Deepgram/OpenAI omit the key). refiner-service reads
it per sentence so the LLM can decide whether the context can raise a
low-confidence sentence or whether it should keep the engine's wording.

---

## dnd_wiki (wiki-service)

```
wiki_pages
  id            uuid PK
  campaign_id   uuid NOT NULL
  kind          text NOT NULL
                -- character|location|faction|item|quest|event (six categories;
                -- event pages back the campaign timeline: every timeline event
                -- links to its event page via timeline_events.page_id)
  title         text NOT NULL
  slug          text NOT NULL
  content_json  jsonb NOT NULL      -- body blocks + front matter; LLM drafts use
                -- {summary?, history?, physical_look?, personality?, aliases?,
                --  facts?, session_references?, attributes?, image_uri?,
                --  language?}
                --   summary            cross-type "Overview" section (other
                --                      kinds only; character pages have none)
                --   history            cross-type narrative section (locations:
                --                      founding/wars/famous events, from v8)
                --   physical_look      character appearance (Physical look section)
                --   personality        character temperament (Personality section)
                --   image_uri          "bucket/key" of the uploaded portrait
                --   facts              durable cross-session truths (Facts section)
                -- (v6) drafts never reference the chunk/session ("non interviene
                -- in questo frammento"), the narrator (Dungeon Master/DM) is never
                -- a character, and generically-named locations are composed with
                -- their named anchor ("Ospedale di Fatumastra") or dropped
                --   session_references [{session_id, facts[]}] session-scoped details
                --   attributes         VALIDATED per-kind schema (see below)
  status        text DEFAULT 'draft'
                -- draft|published|archived (migration 0005 folded the legacy
                -- pending_review rows into draft: the pipeline writes pages
                -- only after the DM confirmed the proposed changes)
  visibility    text DEFAULT 'public'  -- public|dm_only|hidden
  confidence    numeric              -- 0..1 from the LLM draft
  source_session_id uuid NULL       -- which session generated this page
  created_by    uuid
  updated_by    uuid
  created_at    timestamptz
  updated_at    timestamptz
  UNIQUE (campaign_id, slug)

attributes (validated by app/page_attributes.py; unknown keys are rejected):
  character     {character_type: npc|player (=npc), race?, class?, gender?,
                 height?, weight?, age?}
                -- player pages always use the CHARACTER name, never the player name
  location      {location_type: city|town|village|region|continent|world|
                             building|structure|dungeon|wilderness|other (=other),
                 region?, latitude?, longitude?, founded?,
                 -- type-specific (only valid for the listed location_type;
                 -- other fields are rejected with a 422):
                 population?, government?, ruler?, demographics?, economy?,
                   defenses?, religion?, districts?[], notable_locations?[]
                   (city|town|village),
                 capital?, terrain?, climate? (region|continent|world|wilderness
                   per-field), planes?, pantheon? (world),
                 owner?, purpose? (building|structure),
                 entrance?, levels?, hazards? (dungeon),
                 flora_fauna? (wilderness)}
                -- the content-service merger (v8) emits only the fields valid
                -- for the mapped location_type; e.g. a city draft carries
                -- population/government/districts, a dungeon entrance/levels
  faction       {faction_type: guild|order|government|military|criminal|
                          religious|other (=other), leader?, headquarters?}
  item          {item_type: weapon|armor|potion|artifact|wondrous|trinket|other,
                 rarity?: common|uncommon|rare|very_rare|legendary|artifact,
                 owner?}
  quest         {quest_status: open|in_progress|completed|failed (=open),
                 giver?, reward?}

page_versions
  id            uuid PK
  page_id       uuid FK -> wiki_pages
  version_no    int NOT NULL          -- per-page 1,2,3...; newest version first
  content_json  jsonb
  change_note   text
  created_by    uuid
  created_at    timestamptz
  UNIQUE (page_id, version_no)

page_relations
  id            uuid PK
  page_id       uuid FK -> wiki_pages
  related_page_id uuid FK -> wiki_pages
  relation_type text   -- appears_in|member_of|allied_with|led_by|owner|possible_duplicate|...
                        -- possible_duplicate: content-service tags a fresh draft that
                        -- looks like an existing page (the DM merges manually)
                        -- owner: content-service auto-proposes durable ownership
                        -- (character --[owner]--> place/item) from the transcript
  UNIQUE (page_id, related_page_id, relation_type)

timeline_events
  id            uuid PK
  campaign_id   uuid NOT NULL
  page_id       uuid FK -> wiki_pages NULL
                -- the linked event page (kind='event'); the content pipeline
                -- drafts the page first, then upserts this entry (approved=false
                -- until the DM approves). page_id NULL for legacy DM rows.
  in_world_date text                -- campaign-specific calendar
  summary       text
  approved      boolean DEFAULT false
  source_session_id uuid NULL
  created_at    timestamptz
```

---

## dnd_content (content-service)

```
generation_jobs
  id            uuid PK
  session_id    uuid NOT NULL
  status        text DEFAULT 'queued'   -- queued|running|done|failed
  phase         text DEFAULT 'summary'  -- summary (transcript → draft summary),
                                        -- wiki (confirmed summary → proposed changes),
                                        -- apply (confirmed changes → wiki pages)
  llm_provider  text
  llm_model     text
  prompt_version text
  draft_ids     uuid[]                  -- pages written by an 'apply' run; empty for
                                        -- the summary and wiki phases (no page yet)
  confidence    numeric
  error         text
  created_at    timestamptz
  updated_at    timestamptz

session_summaries                    -- the review layer: merged LLM extraction the
                                     -- session page shows and the DM corrects
  id            uuid PK
  session_id    uuid UNIQUE NOT NULL  -- one row per session (a new draft overwrites it)
  generation_job_id uuid NULL         -- the run that produced this summary
  summary       text NOT NULL         -- session summary LINES, newline separated
                                      -- (one beat per line: the DM selects and
                                      -- corrects single lines)
  language      text NULL             -- transcript language (draft page language)
  party_characters jsonb DEFAULT '[]' -- party CHARACTER names resolved at extraction
                                      -- time (player-vs-NPC tagging in the wiki phase)
  characters    jsonb DEFAULT '[]'    -- merged characters (name/aliases/description/facts/session_facts/mentions/confidence)
  locations     jsonb DEFAULT '[]'    -- merged locations (same shape)
  events        jsonb DEFAULT '[]'    -- merged events (title/description/participants/confidence)
  timeline_entries jsonb DEFAULT '[]' -- merged timeline (time/summary/characters)
  review_status text DEFAULT 'draft'  -- draft (awaiting DM review) | confirmed
                                      -- (wiki pages/events may be created from it)
  revision      int DEFAULT 1         -- 1 for the first draft, +1 per DM rewrite
  confirmed_at  timestamptz NULL      -- DM confirmation stamp
  confirmed_by  uuid NULL
  edit_history  jsonb DEFAULT '[]'    -- [{targets: [summary line, ...],
                                      --   instruction, requested_by, created_at}]
  confidence    numeric
  llm_provider  text
  llm_model     text
  prompt_version text
  created_at    timestamptz
  updated_at    timestamptz

wiki_change_sets                     -- the PROPOSED wiki changes of a session
                                     -- (the "git status": reviewable before anything
                                     -- is written, one row per session)
  id            uuid PK
  session_id    uuid UNIQUE NOT NULL
  summary_id    uuid NULL             -- the confirmed summary it was expanded from
  generation_job_id uuid NULL         -- the 'wiki' run that proposed it
  status        text DEFAULT 'draft'  -- draft (under review) | applying (being
                                      -- written) | applied (in the wiki)
  language      text NULL
  changes       jsonb DEFAULT '[]'    -- [{id, action: create|update, kind, title,
                                      --   page_id, before: {title, content_json} | null,
                                      --   after: {title, content_json, visibility,
                                      --   confidence}, timeline: {summary,
                                      --   in_world_date} | null, dropped}]
  relations     jsonb DEFAULT '[]'    -- [{id, from_title, to_title, to_page_id,
                                      --   relation_type, dropped}]
  skipped       jsonb DEFAULT '[]'    -- entities the campaign already documents
                                      -- (context for the DM, never changes)
  confirmed_at  timestamptz NULL      -- DM confirmation of the proposed changes
  confirmed_by  uuid NULL
  applied_at    timestamptz NULL      -- when the wiki accepted them
  error         text                  -- why a failed apply went back to review
  created_at    timestamptz
  updated_at    timestamptz

notifications
  id            uuid PK
  user_id       uuid NOT NULL
  campaign_id   uuid
  type          text   -- draft_ready|speaker_pending|session_published|wiki_published
  payload       jsonb
  read_at       timestamptz NULL
  created_at    timestamptz
```

---

## Cross-service invariants

- `campaign_members.role = 'dm'` is enforced by campaign-service; the DM of a
  campaign is the only one who can change visibility or approve drafts.
- `sessions.campaign_id` must reference a campaign the uploader belongs to
  (checked at upload time via campaign-service API).
- A page is **never** hard-deleted; `archived` + `hidden` cover removal.
  Sanctioned exceptions: the debug reset endpoint and migration 0002 (which
  removed the event/session_note/article page kinds).
- A **session** may be deleted by the DM only while it has not generated its
  wiki updates (blocked from `applying_wiki` on; every session payload carries
  `can_delete`). Deletion purges the recording/artifacts, the content-service
  rows of the session (summary, jobs, change set) and the session itself — and
  is refused (409) if wiki-service still reports pages or timeline entries
  attributed to it.
- All MinIO URIs are `bucket/key` pairs; services resolve them against
  `MINIO_ENDPOINT` + presigned URLs only.