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
| `content.generate` | `speakers.identified`, `speakers.assigned` | content-service | Generate wiki drafts from the named transcript |
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
> labels party speakers by their character in the wiki; the synthetic
> `speakers.identified` published by the debug regenerate endpoint carries
> the same fields (plus `regenerated: true`).

## speakers.assigned

Emitted when the DM names a previously-unknown speaker (and optionally enrolls
their voiceprint).

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
    "draft_ids": ["uuid", "uuid"],
    "confidence": 0.78,
    "language": "it",
    "skipped_duplicates": [
      {"title": "Città", "kind": "location", "matched_page_id": "uuid", "matched_title": "Fatumastra"}
    ]
  }
}
```

> `language` is the majority transcript language the drafts were written in
> (null when unknown). Drafts are CHARACTER and LOCATION pages, plus EVENT
> pages for world-significant events (each event page gets a pending campaign
> timeline entry via the internal wiki upsert endpoint; events already on the
> timeline are updated, not duplicated). `skipped_duplicates` lists extracted
> entities that were NOT drafted because the campaign already documents them
> (exact title/alias match) — their new facts remain visible on the persisted
> session summary. Fuzzy look-alikes still get a draft plus a
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
- `speaker.pending` — payload `{campaign_id, session_id, pending_labels: []}` → DM.
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