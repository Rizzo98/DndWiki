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

---

## dnd_campaigns (campaign-service)

```
campaigns
  id            uuid PK
  name          text NOT NULL
  slug          text UNIQUE NOT NULL
  description   text
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
                -- generating_wiki|content_ready|reviewed|published|failed
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
  id            uuid PK
  session_id    uuid FK -> sessions
  speaker_label text NOT NULL       -- SPEAKER_00, ...
  member_id     uuid NULL           -- campaign member this voice belongs to
                                    -- (may lack a user account)
  user_id       uuid NULL           -- mirrors member.user_id when linked;
                                    -- voiceprint enrollment/matching keys on
                                    -- this; null until assigned
  confidence    numeric             -- cosine similarity of best match
  status        text DEFAULT 'pending'  -- pending | auto | confirmed
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
      "chunk": 0, "speaker": "SPEAKER_00", "words": [{"word": "Welcome", "start": 0.0, "end": 0.5}]
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
                -- draft|pending_review|published|archived
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
  llm_provider  text
  llm_model     text
  prompt_version text
  draft_ids     uuid[]                  -- pages created (pending_review)
  confidence    numeric
  error         text
  created_at    timestamptz
  updated_at    timestamptz

session_summaries                    -- merged LLM extraction, shown on the session page
  id            uuid PK
  session_id    uuid UNIQUE NOT NULL  -- one row per session (regeneration overwrites)
  generation_job_id uuid NULL         -- the run that produced this summary
  summary       text NOT NULL         -- session_summary paragraph
  characters    jsonb DEFAULT '[]'    -- merged characters (name/aliases/description/facts/session_facts/mentions/confidence)
  locations     jsonb DEFAULT '[]'    -- merged locations (same shape)
  events        jsonb DEFAULT '[]'    -- merged events (title/description/participants/confidence)
  timeline_entries jsonb DEFAULT '[]' -- merged timeline (time/summary/characters)
  confidence    numeric
  llm_provider  text
  llm_model     text
  prompt_version text
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
- All MinIO URIs are `bucket/key` pairs; services resolve them against
  `MINIO_ENDPOINT` + presigned URLs only.