# speaker-service

Speaker identification: turns diarized `SPEAKER_00…` labels into player
names, reusing names already learned from previous sessions.

## How naming works

1. **Identify** (`transcription.refined`, or `transcription.completed` when
   `REFINER_ENABLED=false`): download the recording,
   - **learn the campaign first**: the DM's manual namings in earlier
     sessions of the same campaign are turned into voice samples (see
     *Learning from earlier sessions* below), then
   - when the labels come from refiner-service (the LLM contextual pass
     already fixed text + speaker attribution), they are matched **directly**;
   - otherwise (refiner disabled) the raw diarizer labels are re-clustered
     first:
   build speaker turns from the raw diarized segments (chunk boundaries are
   hard turn boundaries), embed each turn with **ECAPA-TDNN**
   (`speechbrain/spkrec-ecapa-voxceleb`, 192-d, same model as user-service
   enrollment), and **re-cluster** the turns with cosine agglomerative
   clustering (seeded by per-campaign enrollment centroids). The raw diarizer
   labels are overridden with canonical `SPEAKER_XX` labels in
   transcript/diarization.json. Each new label is then matched to the campaign
   voiceprints in Qdrant (`voiceprints`, filtered by `campaign_id`).
   - best score ≥ `SPEAKER_MATCH_THRESHOLD` (0.75) → **auto** — the label is
     assigned to that user (`speaker_assignments.status = auto`, with
     confidence);
   - below threshold (or no usable clip) → **pending** — the DM names it.
   The session moves `transcribed -> identifying_speakers -> speaker_pending`
   (after the refiner: `refined -> identifying_speakers`); assignments are
   upserted via the session-service internal API; `speakers.identified` /
   `speaker.pending` are published. It only reaches `speakers_identified`
   when *every* label is **confirmed** — the DM accepts each auto match and
   names the rest in the speaker panel (an auto match is a proposal, not a
   decision), which is also what makes those voices learnable. A session with
   no diarized labels closes the stage on its own.
2. **Enroll** (`speakers.assigned`): when the DM names a previously-unknown
   speaker, the worker slices that label's voice out of the session recording,
   embeds it and upserts it as a **session-derived voiceprint** for the
   assigned user in the campaign. Next session, the same voice auto-matches
   the name — this is what makes a name stick across sessions.

Both event types flow through the same queue (`speakers.identify` binds
`transcription.completed`, `transcription.refined` and `speakers.assigned`);
the handler dispatches on the event type. When `REFINER_ENABLED` is true,
`transcription.completed` is acked and skipped (the refiner consumes it and
emits `transcription.refined`), so raw labels never race the LLM pass.

## Learning from earlier sessions (the DM's manual namings)

Naming a speaker by hand is the best label the platform ever gets: the DM
listened and said "this is Anna". Before matching a session, the worker asks
session-service for the campaign's **speaker history**
(`GET /internal/campaigns/{id}/speaker-history`): the labels the DM confirmed
in the campaign's earlier sessions, with the recording they were named in.
Each of those labels is turned into voice samples:

1. its audio is read from that session's `transcript/diarization.json`,
2. only turns of **at least `HISTORY_SAMPLE_MIN_SEC` (3 s)** whose diarization
   confidence is **at least `HISTORY_SAMPLE_MIN_CONFIDENCE` (0.8)** are kept
   (a backend that reports no confidence is trusted — the DM did confirm the
   label), consecutive segments merge into one turn, and a turn is embedded
   from its first `HISTORY_SAMPLE_MAX_SEC` (20 s),
3. at most `HISTORY_MAX_WINDOWS_PER_LABEL` (3) turns per named label and
   `HISTORY_MAX_WINDOWS_PER_RUN` (24) per identification run are embedded
   (one decode per past session, longest turns first),
4. each embedding is stored as a campaign voiceprint with
   `source=history` and a `history_key` marker `session#label#window`, so the
   sample is enrolled **once** and later runs reuse it for free.

The current session itself is always excluded (`exclude_session_id`), so a
session never becomes a reference for itself; `HISTORY_MAX_SESSIONS` (5)
bounds how far back a run looks. This is additive: profile enrollment and the
voiceprints written on `speakers.assigned` stay untouched, and a campaign with
no history behaves exactly as before. History is best effort — an unreadable
artifact, an unreachable session-service or a failed embedding is logged and
the identification continues.

## Owns

- Matching + session-derived voiceprint writes over Qdrant `voiceprints`
  (collection created/owned by user-service; enrollment rows live in
  `voice_profiles` there — session-derived points are not tracked in that
  table, see docs/data-model.md)
- `speaker_assignments` rows in `dnd_sessions` (schema owned by
  session-service — updated via its internal API)

## Interfaces

- Consumes (queue `speakers.identify`): `transcription.completed`, `transcription.refined`, `speakers.assigned`
- Publishes: `speakers.identified`, `speaker.pending`
- Calls: session-service `/internal/sessions/*` and
  `/internal/campaigns/{id}/speaker-history` (service token), MinIO
  `recordings`/`transcripts` buckets, Qdrant `voiceprints`

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `SESSION_SERVICE_URL` | `http://localhost:8003` | session-service internal API |
| `VOICE_EMBEDDING_MODEL` | `speechbrain/spkrec-ecapa-voxceleb` | ECAPA-TDNN model (cached under `HF_HOME` on the models volume) |
| `SPEAKER_MATCH_THRESHOLD` | `0.75` | min cosine similarity to auto-assign |
| `SPEAKER_POOL_SEC` | `20.0` | seconds of audio pooled per speaker |
| `VOICE_SAMPLE_MIN_SEC` | `2.0` | shorter clips are not embedded |
| `HISTORY_SAMPLES_ENABLED` | `true` | learn voice samples from the DM's namings in earlier sessions |
| `HISTORY_SAMPLE_MIN_SEC` | `3.0` | shorter named turns are not used as samples |
| `HISTORY_SAMPLE_MAX_SEC` | `20.0` | how much of a named turn is embedded (one window per turn) |
| `HISTORY_SAMPLE_MIN_CONFIDENCE` | `0.8` | named turns below this diarization confidence are skipped (no confidence = trusted) |
| `HISTORY_MAX_WINDOWS_PER_LABEL` | `3` | samples kept per named label |
| `HISTORY_MAX_WINDOWS_PER_RUN` | `24` | embeddings per identification run |
| `HISTORY_MAX_SESSIONS` | `5` | how many earlier sessions of the campaign are scanned |
| `RELABEL_ENABLED` | `true` | re-cluster raw labels before matching |
| `RELABEL_MODE` | `threshold` | `threshold` (cosine cut) or `count` (K = distinct raw labels) |
| `RELABEL_MERGE_THRESHOLD` | `0.5` | cosine similarity below which turns merge (threshold mode) |
| `RELABEL_ANCHOR_THRESHOLD` | `0.6` | cosine similarity to snap a cluster to an enrolled user |
| `RELABEL_MIN_TURN_SEC` | `1.0` | shorter turns are not embedded (inherit their neighbour's label) |
| `RELABEL_SPEAKER_COUNT` | `0` | force the number of speakers K (0 = auto) |
| `REFINER_ENABLED` | `true` | skip `transcription.completed` and match the refined labels from refiner-service |

## Local dev

```bash
pip install -e libs/python/dnd_common
pip install -e services/speaker-service
python -m app.workers.identify
uvicorn app.main:app --reload --port 8005
```

The worker embeds with torch/speechbrain; unit tests fake every I/O boundary
and run without the ML stack.