# DnD Wiki — Event Contracts

All events flow through the RabbitMQ topic exchange **`dnd.events`** (durable).
Queues and bindings are declared **idempotently at service startup** by
`dnd_common.events` — the `infrastructure/rabbitmq/definitions.json` file is the
same topology for reference/ops.

## Topology

| Queue | Binds (routing keys) | Consumer | Purpose |
|---|---|---|---|
| `transcription.jobs` | `session.recorded` | transcription-service | Run WhisperX on a recording |
| `transcripts.refine` | `transcription.completed` | refiner-service | LLM contextual pass: fix transcription errors + speaker attribution, rewrite artifacts, emit `transcription.refined` |
| `speakers.identify` | `transcription.refined`, `transcription.completed`, `speakers.assigned` | speaker-service | Match diarized labels to users (identify) + enroll the named voice (assign) |
| `content.generate` | `speakers.identified`, `speakers.assigned`, `summary.regenerate`, `summary.confirmed`, `plan.confirmed` | content-service | Review layers: draft the session summary from the named transcript, rewrite it from the DM's feedback, propose the wiki changes once the summary is confirmed, and write them once the DM confirms the proposal |
| `search.events` | `wiki.published`, `wiki.updated`, `wiki.archived` | search-service | Keep Meilisearch in sync |
| `notification.events` | `wiki.draft_ready`, `speaker.pending`, `session.published`, `wiki.published` | notification-service | Email/webhook/push |

Messages are JSON with a `schema_version`, `event_id`, `occurred_at`, and a typed
`payload`. All payloads carry `campaign_id` and `session_id` where applicable.

---

## session.recorded

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "occurred_at": "2025-01-01T12:00:00Z",
  "type": "session.recorded",
  "payload": {
    "session_id": "uuid",
    "campaign_id": "uuid",
    "recorded_by": "uuid",
    "audio_uri": "recordings/<session_id>/raw.m4a",
    "duration_sec": 3724
  }
}
```

## transcription.completed

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "occurred_at": "2025-01-01T12:05:00Z",
  "type": "transcription.completed",
  "payload": {
    "session_id": "uuid",
    "campaign_id": "uuid",
    "language": "en",
    "audio_uri": "recordings/<session_id>/raw.m4a",
    "transcript_uri": "transcripts/<session_id>/transcript.json",
    "diarization_uri": "transcripts/<session_id>/diarization.json",
    "segments": [
      {"start": 0.0, "end": 4.2, "chunk": 0, "speaker": "SPEAKER_00", "speaker_confidence": 0.97, "confidence": 0.97, "text": "Welcome back."}
    ]
  }
}
```

> `segments` is included for immediate processing; the full payload (with word
> timings) lives in MinIO. `speaker_confidence` (optional) is the diarization
> confidence reported by the transcription backend (Deepgram Nova 3); the web
> UI highlights labels whose confidence is low so the DM re-checks them.
>
> `confidence` (optional, 0..1) is the ASR text probability per utterance
> (AssemblyAI reports it; other backends omit the key). refiner-service reads
> it so the LLM can decide whether context can raise a sentence's confidence.

## transcription.progress

Emitted after every transcription chunk (default 5 minutes) so consumers can
track progress. The `transcript_uri` / `diarization_uri` point at the *same*
objects as the final artifacts — they are overwritten as the job progresses.
The web session page deliberately does NOT render these partial/raw objects:
it waits for the refiner's in-place rewrite (status `refined`) so only the
refined transcript is ever shown to the DM/players.

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "occurred_at": "2025-01-01T12:02:00Z",
  "type": "transcription.progress",
  "payload": {
    "session_id": "uuid",
    "campaign_id": "uuid",
    "language": "it",
    "audio_uri": "recordings/<session_id>/raw.m4a",
    "transcript_uri": "transcripts/<session_id>/transcript.json",
    "diarization_uri": "transcripts/<session_id>/diarization.json",
    "segments": [
      {"start": 0.0, "end": 4.2, "chunk": 0, "speaker": "SPEAKER_00", "text": "Benvenuti."}
    ],
    "chunk_index": 0,
    "total_chunks": 8
  }
}
```

> `segments` covers everything transcribed so far. `chunk_index` is 0-based;
> `total_chunks` is known up front (the recording is split into
> `WHISPER_CHUNK_SECONDS`-long pieces). The final full result arrives on
> `transcription.completed`; speaker labels are per-chunk and may not be
> consistent across chunks (each segment carries an optional `chunk` field —
> speaker-service re-clusters these labels into canonical `SPEAKER_XX` values
> before matching, so cross-chunk identity is restored downstream).

## transcription.refined

Emitted by refiner-service after the LLM contextual pass (only when
`REFINER_ENABLED` is true). The payload mirrors `transcription.completed`;
`segments` carry the refined text and canonical `SPEAKER_XX` labels (timings
and `chunk` are unchanged), and the artifacts at `transcript_uri` /
`diarization_uri` were rewritten in place.

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "occurred_at": "2025-01-01T12:06:00Z",
  "type": "transcription.refined",
  "payload": {
    "session_id": "uuid",
    "campaign_id": "uuid",
    "language": "en",
    "audio_uri": "recordings/<session_id>/raw.m4a",
    "transcript_uri": "transcripts/<session_id>/transcript.json",
    "diarization_uri": "transcripts/<session_id>/diarization.json",
    "segments": [
      {"start": 0.0, "end": 4.2, "chunk": 0, "speaker": "SPEAKER_00", "text": "Welcome back, heroes."}
    ],
    "refined": true,
    "refiner": {
      "provider": "deepseek",
      "model": "deepseek/deepseek-chat",
      "prompt_version": "v3",
      "cast": {"injected": true, "members": 4, "with_description": 3, "table_size": 5}
    }
  }
}
```

> The `refiner.cast` object records how much campaign context (character names
> + physical descriptions, fetched from campaign-service) was injected into
> the LLM prompt; `injected` is false when no cast was available.
> `table_size` is the campaign roster count (DM + players) used as the
> MAXIMUM number of distinct speakers in the prompt.

> speaker-service binds both `transcription.completed` and
> `transcription.refined` on `speakers.identify`; when `REFINER_ENABLED` is
> true it skips `transcription.completed` (the refiner consumes it) and
> matches the refined labels directly — no re-clustering, since the LLM
> already fixed cross-chunk identity. `audio_uri` is forwarded unchanged so
> speaker-service can still download the recording for voice windows. When
> disabled, `transcription.completed` is processed as before (raw labels
> re-clustered by embeddings).

> If a refined event ever arrives without `audio_uri`, speaker-service does
> NOT fail the session: every label is marked `pending` so the DM can still
> name the speakers manually (voice auto-matching is simply skipped).

## speakers.identified

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "occurred_at": "2025-01-01T12:07:00Z",
  "type": "speakers.identified",
  "payload": {
    "session_id": "uuid",
    "campaign_id": "uuid",
    "speakers": [
      {"label": "SPEAKER_00", "user_id": "uuid", "confidence": 0.91, "status": "auto"},
      {"label": "SPEAKER_01", "user_id": null, "confidence": 0.42, "status": "pending"}
    ],
    "pending_assignment": true
  }
}
```

> Entries may also carry `display_name` (member player name for userless
> members) and `character_name` (the member's CHARACTER). content-service
> labels party speakers by their character in the wiki.

## speakers.assigned

Emitted when the DM names a previously-unknown speaker, or confirms the match
the pipeline proposed (`POST /api/sessions/{id}/speakers/{label}/confirm`), and
optionally enrolls their voiceprint.

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "occurred_at": "2025-01-01T14:00:00Z",
  "type": "speakers.assigned",
  "payload": {
    "session_id": "uuid",
    "campaign_id": "uuid",
    "label": "SPEAKER_01",
    "member_id": "uuid",
    "user_id": "uuid | null",
    "display_name": "player name | null",
    "character_name": "character name | null",
    "assigned_by": "uuid",
    "enrolled_voiceprint": true,
    "audio_uri": "recordings/<session_id>/raw.m4a"
  }
}
```

> The speaker is identified by the campaign `member_id` (the DM picks a
> member, which may lack a user account). `user_id` mirrors the member's user
> link when present: `audio_uri` then lets speaker-service slice the named
> speaker's voice out of the session recording and enroll it as a
> session-derived voiceprint for `user_id` in the campaign, so future
> sessions auto-assign the name. `display_name` (the member player name) is
> carried for userless members so content generation can name the speaker in
> the transcript view, while `character_name` makes it label party speakers
> by their CHARACTER on the wiki. Naming the last pending speaker of a
> session also moves it `speaker_pending -> speakers_identified`.

## content.summary.drafted

Published by content-service every time a session summary DRAFT is (re)built:
the first distillation of the transcript and each rewrite driven by the DM's
review feedback. Nothing is written to the wiki at this point — the session
parks on `summary_ready` until the DM confirms it.

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "occurred_at": "2025-01-01T12:12:00Z",
  "type": "content.summary.drafted",
  "payload": {
    "session_id": "uuid",
    "campaign_id": "uuid",
    "generation_job_id": "uuid",
    "summary_id": "uuid",
    "revision": 2,
    "confidence": 0.78,
    "language": "it"
  }
}
```

> `revision` is 1 for the first draft and increases with every DM-driven
> rewrite. The summary itself lives in `session_summaries` and is read by the
> session page through `GET /api/content/summaries/{session_id}`, together
> with its `review_status` (`draft`|`confirmed`).

## summary.regenerate

Published by content-service when the DM sends review feedback from the
session page: the summary lines they selected plus what must change. The
worker applies it to the WHOLE persisted extraction (summary lines, entities,
events, timeline entries) and publishes `content.summary.drafted` with the new
revision.

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "occurred_at": "2025-01-01T12:18:00Z",
  "type": "summary.regenerate",
  "payload": {
    "session_id": "uuid",
    "campaign_id": "uuid",
    "summary_id": "uuid",
    "revision": 1,
    "requested_by": "uuid",
    "edits": [
      {
        "targets": ["Character A was going to the city center."],
        "instruction": "It wasn't Character A, it was Character B"
      }
    ],
    "summary_lines": ["Character A was going to the city center."]
  }
}
```

> `targets` lists the summary lines the request is about (empty = the whole
> summary); `summary_lines` carries the lines exactly as displayed by the
> client, so hand-edited text is honored.

## summary.confirmed

Published by content-service when the DM accepts the draft summary (the API
stamps `session_summaries.review_status='confirmed'` in the same request).
It unlocks the wiki phase, which turns the summary into a PROPOSED change set
(`content.plan.ready`) — nothing is written to the wiki yet.

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "occurred_at": "2025-01-01T12:19:00Z",
  "type": "summary.confirmed",
  "payload": {
    "session_id": "uuid",
    "campaign_id": "uuid",
    "summary_id": "uuid",
    "revision": 2,
    "confirmed_by": "uuid"
  }
}
```

## content.plan.ready

Published by content-service when the proposed wiki changes of a session are
stored and parked for review (`wiki_change_sets.status='draft'`, session on
`wiki_plan_ready`). The DM reads them through
`GET /api/content/sessions/{id}/plan`, edits one or more entries and confirms
the set.

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "occurred_at": "2025-01-01T12:19:30Z",
  "type": "content.plan.ready",
  "payload": {
    "session_id": "uuid",
    "campaign_id": "uuid",
    "generation_job_id": "uuid",
    "plan_id": "uuid",
    "summary_id": "uuid",
    "confirmed_by": "uuid",
    "create": 3,
    "update": 1,
    "relations": 4,
    "skipped": 2
  }
}
```

> `create`/`update` count the proposed pages, `relations` the proposed
> cross-references and `skipped` the entities the campaign already documents
> (they are not proposed again).

## plan.confirmed

Published by content-service when the DM confirms the proposed changes. The
worker then writes them through wiki-service's internal apply endpoint: the
new pages are created PUBLISHED and new timeline entries APPROVED (the
confirmation is the approval), so nothing pipeline-generated ever lands in a
"pending review" state.

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "occurred_at": "2025-01-01T12:19:50Z",
  "type": "plan.confirmed",
  "payload": {
    "session_id": "uuid",
    "campaign_id": "uuid",
    "plan_id": "uuid",
    "confirmed_by": "uuid",
    "create": 3,
    "update": 1,
    "pages": 4,
    "relations": 4,
    "dropped": 0
  }
}
```

## content.generated

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "occurred_at": "2025-01-01T12:20:00Z",
  "type": "content.generated",
  "payload": {
    "session_id": "uuid",
    "campaign_id": "uuid",
    "generation_job_id": "uuid",
    "summary_id": "uuid",
    "plan_id": "uuid",
    "draft_ids": ["uuid", "uuid"],
    "created": [{"change_id": "c1", "page_id": "uuid", "title": "Aragorn",
                 "kind": "character", "action": "create", "reason": null}],
    "updated": [],
    "skipped": [],
    "timeline_entries": 1,
    "relations_created": 2,
    "confidence": 0.78,
    "language": "it"
  }
}
```

> Published only after the DM confirmed the PROPOSED CHANGES (`plan_id` points
> at the confirmed change set, `summary_id` at the confirmed summary it was
> expanded from). `created`/`updated`/`skipped` report what the wiki did with
> each change — a create the campaign already documents is skipped rather than
> duplicated.

> `language` is the transcript language the drafted text was written in (null
> when unknown). Pages are CHARACTER and LOCATION pages, plus EVENT pages for
> world-significant events (each event page backs a campaign timeline entry,
> written approved because the DM confirmed it; events already on the timeline
> are updated, not duplicated). Fuzzy look-alikes still get a page plus a
> `possible_duplicate` page relation for the DM to merge.

## wiki.published / wiki.updated / wiki.archived

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "occurred_at": "2025-01-01T15:00:00Z",
  "type": "wiki.published",
  "payload": {
    "page_id": "uuid",
    "campaign_id": "uuid",
    "slug": "aragorn",
    "kind": "character",
    "visibility": "public"
  }
}
```

## notification events

- `wiki.draft_ready` — payload `{campaign_id, session_id, draft_count}` → DM.
- `speaker.pending` — payload `{campaign_id, session_id, pending_labels: [], unconfirmed_labels: []}` → DM.
  `pending_labels` are the labels that still need a NAME; `unconfirmed_labels`
  is everything the DM has not accepted yet (auto matches included).
- `session.published` — payload `{campaign_id, session_id, title}` → players.

---

## Delivery semantics

- All exchanges/queues are **durable**; consumers use **manual ack**.
- Failed processing: `nack(reject=false, requeue=false)` → dead-letter queue
  (`*.dlq`), plus `sessions.status = failed` with `error` recorded.
- Retry: a small retry counter header (`x-retries`) with exponential backoff
  (1 s / 10 s / 60 s) before the DLQ.
- Idempotency: consumers key on `event_id` (stored in a processed-events table)
  so redeliveries are safe.