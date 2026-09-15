"""Worker tests: relabel + identification + enrollment flows (all I/O faked)."""

import uuid
from types import SimpleNamespace

import pytest
from dnd_common.events import Event

from app.clients.session_service import ConflictTransition
from app.core.config import ServiceSettings
from app.embedding import WindowAnalysis
from app.workers.identify import _finish, process_assigned, process_identified

SESSION_ID = "11111111-1111-1111-1111-111111111111"
CAMPAIGN_ID = "22222222-2222-2222-2222-222222222222"
USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def vec(i):
    """192-d unit vector with a single 1.0 at index i (orthogonal to others)."""
    v = [0.0] * 192
    v[i] = 1.0
    return v


VA = vec(0)
VB = vec(1)


def segments():
    """Two distinct speakers in one chunk (canonical raw labels)."""
    return [
        {"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00", "text": "hi", "chunk": 0},
        {"start": 5.0, "end": 9.0, "speaker": "SPEAKER_01", "text": "yo", "chunk": 0},
    ]


def transcript_segments():
    return [
        {"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00", "text": "hi", "words": [], "chunk": 0},
        {"start": 5.0, "end": 9.0, "speaker": "SPEAKER_01", "text": "yo", "words": [], "chunk": 0},
    ]


def cross_chunk_segments():
    """Same voice across two chunks, with per-chunk (untrusted) raw labels."""
    return [
        {"start": 0.0, "end": 3.0, "speaker": "Speaker A", "text": "hi", "chunk": 0},
        {"start": 300.0, "end": 303.0, "speaker": "Speaker B", "text": "hi again", "chunk": 1},
    ]


def completed_event(**overrides):
    payload = {
        "session_id": SESSION_ID,
        "campaign_id": CAMPAIGN_ID,
        "language": "en",
        "audio_uri": f"recordings/{SESSION_ID}/raw.m4a",
        "transcript_uri": f"transcripts/{SESSION_ID}/transcript.json",
        "diarization_uri": f"transcripts/{SESSION_ID}/diarization.json",
        "segments": segments(),
    }
    payload.update(overrides)
    return Event(type="transcription.completed", payload=payload)


class FakeStorage:
    def __init__(self, diarization=None, transcript=None, history_diarizations=None):
        self.downloads = []
        self.reads = []
        self.writes = []
        self.diarization = diarization if diarization is not None else {"segments": segments()}
        self.transcript = transcript
        # session_id -> diarization doc, for the sessions a history entry names
        self.history_diarizations = history_diarizations or {}

    async def download_file(self, bucket, key, dest_path):
        self.downloads.append((bucket, key, dest_path))
        with open(dest_path, "wb") as f:  # noqa: ASYNC230 - test fake
            f.write(b"fake-audio")

    async def read_json(self, bucket, key):
        self.reads.append((bucket, key))
        if key.endswith("diarization.json"):
            session_id = key.split("/")[1]
            if session_id in self.history_diarizations:
                return self.history_diarizations[session_id]
            if self.diarization is None:
                raise RuntimeError("missing diarization")
            return self.diarization
        if key.endswith("transcript.json"):
            if self.transcript is None:
                raise RuntimeError("missing transcript")
            return self.transcript
        raise RuntimeError(f"unexpected key {key}")

    async def put_json(self, bucket, key, obj):
        self.writes.append((bucket, key, obj))


class FakeClient:
    def __init__(self, conflict=False, history=None, history_error=False):
        self.status_calls = []
        self.upserts = []
        self.conflict = conflict
        self.history = history or []
        self.history_calls = []
        self.history_error = history_error

    async def update_status(self, session_id, status, error=None):
        if self.conflict and status == "identifying_speakers":
            raise ConflictTransition("already moved on")
        self.status_calls.append((session_id, status, error))
        return {"id": session_id, "status": status}

    async def upsert_speakers(self, session_id, items):
        self.upserts.append((session_id, items))
        return items

    async def speaker_history(self, campaign_id, exclude_session_id=None, limit_sessions=5):
        self.history_calls.append((campaign_id, exclude_session_id, limit_sessions))
        if self.history_error:
            raise RuntimeError("session-service down")
        return self.history


class FakeVoiceprints:
    def __init__(self, hits=None, anchors=None, stored=None):
        self.hits = hits or []
        self.searches = []
        self.upserted = []
        self.anchors = anchors or []
        # extra points of the campaign (payload-only ones seed history keys)
        self.stored = stored or []
        self.list_calls = []

    async def search(self, embedding, campaign_id, limit=1):
        self.searches.append((list(embedding), campaign_id, limit))
        return self.hits

    async def list_campaign(self, campaign_id):
        self.list_calls.append(campaign_id)
        return self.anchors + self.stored

    async def upsert(self, point_id, vector, payload):
        self.upserted.append((point_id, list(vector), payload))


class FakeEmbedder:
    def __init__(
        self, vectors=None, duration=5.0, raise_on=False, snr_db=None, overlap_ratio=0.0
    ):
        self.vectors = vectors
        self.duration = duration
        self.raise_on = raise_on
        self.snr_db = snr_db
        self.overlap_ratio = overlap_ratio
        self.windows_calls = []
        self.full_calls = []

    def embed_windows_sync(self, audio_path, windows):
        return [a.embedding for a in self.embed_windows_full_sync(audio_path, windows)]

    def embed_windows_full_sync(self, audio_path, windows):
        self.windows_calls.append((audio_path, list(windows)))
        self.full_calls.append((audio_path, list(windows)))
        if self.raise_on:
            raise RuntimeError("embed failed")
        vectors = (
            list(self.vectors) if self.vectors is not None else [[0.1] * 192] * len(windows)
        )
        return [
            WindowAnalysis(embedding=v, snr_db=self.snr_db, overlap_ratio=self.overlap_ratio)
            for v in vectors
        ]

    async def embed_bytes(self, data, suffix=".wav"):
        if self.raise_on:
            raise RuntimeError("embed failed")
        return [0.1] * 192, self.duration


class FakeMemberModels:
    def __init__(self, hits=None):
        self.hits = hits or []
        self.searches = []
        self.upserted = []

    async def search(self, embedding, campaign_id, limit=8):
        self.searches.append((list(embedding), campaign_id, limit))
        return self.hits

    async def upsert(self, point_id, vector, payload):
        self.upserted.append((point_id, list(vector), payload))


class FakeObservations:
    def __init__(self):
        self.batches = []

    async def upsert_many(self, points):
        self.batches.append(list(points))


class FakePublisher:
    def __init__(self):
        self.events = []

    async def __call__(self, event):
        self.events.append(event)


def settings(tmp_path, **overrides):
    # Existing tests exercise the direct transcription.completed path
    # (refiner disabled); refiner-enabled behavior has its own tests below.
    kwargs = {
        "work_dir": str(tmp_path),
        "speaker_match_threshold": 0.75,
        "refiner_enabled": False,
        # The per-observation evidence pass is opt-in here: the legacy tests
        # below assert the pooled-window behaviour the redesign replaces, and
        # the observation path has its own tests further down.
        "observations_enabled": False,
    }
    kwargs.update(overrides)
    return ServiceSettings(**kwargs)


@pytest.fixture(autouse=True)
def fake_slicer(monkeypatch):
    """slice_wav_bytes needs torch; replace it for the enrollment path."""
    monkeypatch.setattr(
        "app.workers.identify.slice_wav_bytes",
        lambda audio_path, start, end: b"wav-data",
    )


# ------------------------------------------------------------------ identify


async def test_identify_relabels_and_auto_matches(tmp_path):
    s = settings(tmp_path)
    storage = FakeStorage(transcript={"segments": transcript_segments()})
    client = FakeClient()
    voiceprints = FakeVoiceprints(
        hits=[SimpleNamespace(score=0.93, payload={"user_id": USER_A})]
    )
    embedder = FakeEmbedder(vectors=[VA, VB])
    publisher = FakePublisher()

    await process_identified(
        completed_event(), s, storage, client, voiceprints, embedder, publisher
    )

    # an auto match is a proposal: the session waits for the DM to accept it
    assert [c[1] for c in client.status_calls] == ["identifying_speakers", "speaker_pending"]
    assert client.upserts == [
        (
            SESSION_ID,
            [
                {"speaker_label": "SPEAKER_00", "user_id": USER_A, "confidence": 0.93, "status": "auto"},
                {"speaker_label": "SPEAKER_01", "user_id": USER_A, "confidence": 0.93, "status": "auto"},
            ],
        )
    ]
    assert len(voiceprints.searches) == 2
    assert len(embedder.windows_calls) == 1
    assert embedder.windows_calls[0][1] == [(0.0, 4.0), (5.0, 9.0)]

    # both artifacts were rewritten with the (relabeled) canonical labels
    dkey = f"transcripts/{SESSION_ID}/diarization.json"
    tkey = f"transcripts/{SESSION_ID}/transcript.json"
    assert [w[1] for w in storage.writes] == [dkey, tkey]
    assert [seg["speaker"] for seg in storage.writes[0][2]["segments"]] == [
        "SPEAKER_00", "SPEAKER_01",
    ]
    assert [seg["speaker"] for seg in storage.writes[1][2]["segments"]] == [
        "SPEAKER_00", "SPEAKER_01",
    ]


async def test_identify_pending_below_threshold(tmp_path):
    s = settings(tmp_path)
    storage = FakeStorage()
    client = FakeClient()
    voiceprints = FakeVoiceprints(
        hits=[SimpleNamespace(score=0.40, payload={"user_id": USER_A})]
    )
    embedder = FakeEmbedder(vectors=[VA, VB])
    publisher = FakePublisher()

    await process_identified(
        completed_event(), s, storage, client, voiceprints, embedder, publisher
    )

    assert client.status_calls[-1][1] == "speaker_pending"
    pending = next(e for e in publisher.events if e.type == "speaker.pending")
    assert pending.payload["pending_labels"] == ["SPEAKER_00", "SPEAKER_01"]
    identified = next(e for e in publisher.events if e.type == "speakers.identified")
    assert identified.payload["pending_assignment"] is True
    assert identified.payload["speakers"][0]["confidence"] == 0.40


async def test_identify_merges_same_voice_across_chunks(tmp_path):
    s = settings(tmp_path)
    storage = FakeStorage(
        diarization={"segments": cross_chunk_segments()},
        transcript={"segments": cross_chunk_segments()},
    )
    client = FakeClient()
    voiceprints = FakeVoiceprints(
        hits=[SimpleNamespace(score=0.93, payload={"user_id": USER_A})]
    )
    embedder = FakeEmbedder(vectors=[VA, VA])  # same voice in both chunks
    publisher = FakePublisher()

    await process_identified(
        completed_event(segments=cross_chunk_segments()),
        s, storage, client, voiceprints, embedder, publisher,
    )

    # two raw labels (Speaker A + Speaker B) collapsed into one identity
    assert client.upserts[0][1] == [
        {"speaker_label": "SPEAKER_00", "user_id": USER_A, "confidence": 0.93, "status": "auto"},
    ]
    assert len(voiceprints.searches) == 1
    assert [seg["speaker"] for seg in storage.writes[0][2]["segments"]] == [
        "SPEAKER_00", "SPEAKER_00",
    ]


async def test_identify_anchors_shortcut_search(tmp_path):
    s = settings(tmp_path)
    storage = FakeStorage()
    client = FakeClient()
    voiceprints = FakeVoiceprints(
        anchors=[
            SimpleNamespace(payload={"user_id": USER_A}, vector=VA),
            SimpleNamespace(payload={"user_id": USER_B}, vector=VB),
        ]
    )
    embedder = FakeEmbedder(vectors=[VA, VB])
    publisher = FakePublisher()

    await process_identified(
        completed_event(), s, storage, client, voiceprints, embedder, publisher
    )

    # anchored clusters resolve directly to enrolled users, no cosine search
    assert voiceprints.searches == []
    assert client.upserts[0][1] == [
        {"speaker_label": "SPEAKER_00", "user_id": USER_A, "confidence": 1.0, "status": "auto"},
        {"speaker_label": "SPEAKER_01", "user_id": USER_B, "confidence": 1.0, "status": "auto"},
    ]
    # anchored matches are proposals too: the DM accepts them in the panel
    assert client.status_calls[-1][1] == "speaker_pending"


async def test_identify_falls_back_to_event_segments(tmp_path):
    """diarization.json missing -> relabel from the event's segments instead."""
    s = settings(tmp_path)
    storage = FakeStorage(diarization=None)  # read_json raises for diarization
    client = FakeClient()
    voiceprints = FakeVoiceprints(
        hits=[SimpleNamespace(score=0.93, payload={"user_id": USER_A})]
    )
    embedder = FakeEmbedder(vectors=[VA, VB])
    publisher = FakePublisher()

    await process_identified(
        completed_event(), s, storage, client, voiceprints, embedder, publisher
    )

    assert client.status_calls[-1][1] == "speaker_pending"
    # diarization was still written (from the fallback segments); no transcript
    assert [w[1] for w in storage.writes] == [
        f"transcripts/{SESSION_ID}/diarization.json"
    ]


async def test_identify_embed_failure_marks_failed(tmp_path):
    s = settings(tmp_path)
    storage = FakeStorage()
    client = FakeClient()
    voiceprints = FakeVoiceprints()
    embedder = FakeEmbedder(raise_on=True)
    publisher = FakePublisher()

    with pytest.raises(RuntimeError, match="embed failed"):
        await process_identified(
            completed_event(), s, storage, client, voiceprints, embedder, publisher
        )

    assert client.status_calls[-1] == (SESSION_ID, "failed", "embed failed")
    assert publisher.events == []


async def test_identify_redelivery_skips(tmp_path):
    s = settings(tmp_path)
    storage = FakeStorage()
    client = FakeClient(conflict=True)
    voiceprints = FakeVoiceprints()
    embedder = FakeEmbedder()
    publisher = FakePublisher()

    await process_identified(
        completed_event(), s, storage, client, voiceprints, embedder, publisher
    )

    assert storage.downloads == []
    assert storage.writes == []
    assert client.upserts == []
    assert publisher.events == []
    assert client.status_calls == []


async def test_identify_missing_audio_uri_falls_back_to_pending(tmp_path):
    """No audio_uri: voice matching is impossible, but the session must not
    fail - every label lands on 'pending' so the DM can still name speakers."""
    s = settings(tmp_path)
    storage = FakeStorage()
    client = FakeClient()
    voiceprints = FakeVoiceprints()
    embedder = FakeEmbedder()
    publisher = FakePublisher()

    await process_identified(
        completed_event(audio_uri=None), s, storage, client, voiceprints, embedder, publisher
    )

    assert [c[1] for c in client.status_calls] == ["identifying_speakers", "speaker_pending"]
    assert storage.downloads == []
    assert storage.writes == []
    assert voiceprints.searches == []
    assert client.upserts[0][1] == [
        {"speaker_label": "SPEAKER_00", "user_id": None, "confidence": None, "status": "pending"},
        {"speaker_label": "SPEAKER_01", "user_id": None, "confidence": None, "status": "pending"},
    ]
    pending = next(e for e in publisher.events if e.type == "speaker.pending")
    assert pending.payload["pending_labels"] == ["SPEAKER_00", "SPEAKER_01"]
    identified = next(e for e in publisher.events if e.type == "speakers.identified")
    assert identified.payload["pending_assignment"] is True


async def test_identify_refined_without_audio_uri_falls_back_to_pending(tmp_path):
    """Same fallback on the refined path: no recording -> pending labels."""
    s = settings(tmp_path, refiner_enabled=True)
    storage = FakeStorage()
    client = FakeClient()
    voiceprints = FakeVoiceprints()
    embedder = FakeEmbedder()
    publisher = FakePublisher()

    await process_identified(
        refined_event(audio_uri=None), s, storage, client, voiceprints, embedder, publisher
    )

    assert [c[1] for c in client.status_calls] == ["identifying_speakers", "speaker_pending"]
    assert storage.downloads == []
    assert client.upserts[0][1][0]["status"] == "pending"


async def test_identify_no_segments_closes_stage(tmp_path):
    s = settings(tmp_path)
    storage = FakeStorage(diarization={"segments": []})
    client = FakeClient()
    voiceprints = FakeVoiceprints()
    embedder = FakeEmbedder()
    publisher = FakePublisher()

    await process_identified(
        completed_event(segments=[]), s, storage, client, voiceprints, embedder, publisher
    )
    assert client.status_calls == [
        (SESSION_ID, "identifying_speakers", None),
        (SESSION_ID, "speakers_identified", None),
    ]
    identified = next(e for e in publisher.events if e.type == "speakers.identified")
    assert identified.payload["speakers"] == []
    assert identified.payload["pending_assignment"] is False


def refined_event(**overrides):
    """transcription.refined: labels already fixed by the LLM contextual pass."""
    payload = {
        "session_id": SESSION_ID,
        "campaign_id": CAMPAIGN_ID,
        "language": "en",
        "audio_uri": f"recordings/{SESSION_ID}/raw.m4a",
        "transcript_uri": f"transcripts/{SESSION_ID}/transcript.json",
        "diarization_uri": f"transcripts/{SESSION_ID}/diarization.json",
        "segments": segments(),
        "refined": True,
    }
    payload.update(overrides)
    return Event(type="transcription.refined", payload=payload)


async def test_identify_skips_completed_when_refiner_enabled(tmp_path):
    """transcription.completed must be acked and skipped: the refiner will
    emit transcription.refined after its LLM pass."""
    s = settings(tmp_path, refiner_enabled=True)
    storage = FakeStorage()
    client = FakeClient()
    voiceprints = FakeVoiceprints()
    embedder = FakeEmbedder()
    publisher = FakePublisher()

    await process_identified(
        completed_event(), s, storage, client, voiceprints, embedder, publisher
    )

    assert storage.downloads == []
    assert storage.writes == []
    assert client.status_calls == []
    assert voiceprints.searches == []
    assert publisher.events == []


async def test_identify_refined_matches_without_relabel(tmp_path):
    """transcription.refined skips re-clustering: labels come from the LLM
    contextual pass and are matched directly (one window per label)."""
    s = settings(tmp_path, refiner_enabled=True)  # relabel_enabled still True
    storage = FakeStorage()
    client = FakeClient()
    voiceprints = FakeVoiceprints(
        hits=[SimpleNamespace(score=0.93, payload={"user_id": USER_A})]
    )
    embedder = FakeEmbedder(vectors=[VA, VB])
    publisher = FakePublisher()

    await process_identified(
        refined_event(), s, storage, client, voiceprints, embedder, publisher
    )

    assert [c[1] for c in client.status_calls] == ["identifying_speakers", "speaker_pending"]
    # matched directly, no artifact rewrite (the refiner already wrote them);
    # each label is embedded from its pooled audio window and cosine-searched
    assert storage.writes == []
    assert len(voiceprints.searches) == 2
    assert client.upserts[0][1] == [
        {"speaker_label": "SPEAKER_00", "user_id": USER_A, "confidence": 0.93, "status": "auto"},
        {"speaker_label": "SPEAKER_01", "user_id": USER_A, "confidence": 0.93, "status": "auto"},
    ]


# ------------------------------------------------- voice samples from the DM

PAST_SESSION = "33333333-3333-3333-3333-333333333333"


def history_entry(session_id=PAST_SESSION, label="SPEAKER_00", user_id=USER_A, **overrides):
    entry = {
        "session_id": session_id,
        "campaign_id": CAMPAIGN_ID,
        "session_status": "content_ready",
        "speaker_label": label,
        "user_id": user_id,
        "audio_uri": f"recordings/{session_id}/raw.mp3",
    }
    entry.update(overrides)
    return entry


def named_turn(start=0.0, end=9.0, confidence=0.95):
    return {"start": start, "end": end, "speaker": "SPEAKER_00", "text": "named", "speaker_confidence": confidence}


async def test_identify_enrolls_voice_samples_from_named_sessions(tmp_path):
    """The DM named this voice in an earlier session: its audio becomes a
    campaign voiceprint, so the current session is matched against it."""
    s = settings(tmp_path)
    storage = FakeStorage(history_diarizations={PAST_SESSION: {"segments": [named_turn()]}})
    client = FakeClient(history=[history_entry()])
    voiceprints = FakeVoiceprints(hits=[SimpleNamespace(score=0.1, payload={"user_id": USER_B})])
    embedder = FakeEmbedder(vectors=[VA, VB])
    publisher = FakePublisher()

    await process_identified(
        completed_event(), s, storage, client, voiceprints, embedder, publisher
    )

    assert client.history_calls == [(CAMPAIGN_ID, SESSION_ID, 5)]
    assert len(voiceprints.upserted) == 1
    point_id, vector, payload = voiceprints.upserted[0]
    assert payload["user_id"] == USER_A
    assert payload["campaign_id"] == CAMPAIGN_ID
    assert payload["source"] == "history"
    assert payload["session_id"] == PAST_SESSION
    assert payload["speaker_label"] == "SPEAKER_00"
    assert payload["history_key"] == f"{PAST_SESSION}#SPEAKER_00#0"
    assert payload["sample_uri"] == f"recordings/{PAST_SESSION}#SPEAKER_00#0"
    assert payload["turn_confidence"] == 0.95
    # deterministic id: re-running the backfill overwrites instead of duplicating
    assert point_id == str(uuid.uuid5(uuid.NAMESPACE_URL, "dnd:history-sample:" + payload["history_key"]))
    assert vector in (VA, VB)
    # the labelled recording is downloaded and embedded once
    assert [d[1] for d in storage.downloads] == [
        f"recordings/{SESSION_ID}/raw.m4a",
        f"recordings/{PAST_SESSION}/raw.mp3",
    ]
    # the session itself is still identified as before (its fake search hits
    # stay below the threshold, so the labels wait for the DM)
    assert client.status_calls[-1][1] == "speaker_pending"


async def test_identify_skips_history_samples_already_enrolled(tmp_path):
    s = settings(tmp_path)
    storage = FakeStorage(history_diarizations={PAST_SESSION: {"segments": [named_turn()]}})
    client = FakeClient(history=[history_entry()])
    voiceprints = FakeVoiceprints(
        stored=[SimpleNamespace(payload={"history_key": f"{PAST_SESSION}#SPEAKER_00#0"}, vector=VA)]
    )
    embedder = FakeEmbedder(vectors=[VA, VB])

    await process_identified(
        completed_event(), s, storage, client, voiceprints, embedder, FakePublisher()
    )

    assert voiceprints.upserted == []
    # nothing to embed -> the past recording is not even downloaded
    assert [d[1] for d in storage.downloads] == [f"recordings/{SESSION_ID}/raw.m4a"]


async def test_identify_ignores_history_without_a_linked_user_or_audio(tmp_path):
    """A userless member has no user-keyed voiceprint to enroll against."""
    s = settings(tmp_path)
    storage = FakeStorage(history_diarizations={PAST_SESSION: {"segments": [named_turn()]}})
    client = FakeClient(
        history=[
            history_entry(user_id=None),
            history_entry(session_id="44444444-4444-4444-4444-444444444444", audio_uri=None),
        ]
    )
    voiceprints = FakeVoiceprints()
    embedder = FakeEmbedder(vectors=[VA, VB])

    await process_identified(
        completed_event(), s, storage, client, voiceprints, embedder, FakePublisher()
    )

    assert voiceprints.upserted == []


async def test_identify_survives_an_unreachable_history(tmp_path):
    """History is an enhancement: session-service hiccups must not fail a run."""
    s = settings(tmp_path)
    storage = FakeStorage()
    client = FakeClient(history_error=True)
    voiceprints = FakeVoiceprints(
        hits=[SimpleNamespace(score=0.93, payload={"user_id": USER_A})]
    )
    embedder = FakeEmbedder(vectors=[VA, VB])

    await process_identified(
        completed_event(), s, storage, client, voiceprints, embedder, FakePublisher()
    )

    # history is optional; the proposals still wait for the DM
    assert client.status_calls[-1][1] == "speaker_pending"
    assert voiceprints.upserted == []


async def test_identify_can_skip_the_history_backfill(tmp_path):
    s = settings(tmp_path, history_samples_enabled=False)
    storage = FakeStorage(history_diarizations={PAST_SESSION: {"segments": [named_turn()]}})
    client = FakeClient(history=[history_entry()])
    voiceprints = FakeVoiceprints()
    embedder = FakeEmbedder(vectors=[VA, VB])

    await process_identified(
        completed_event(), s, storage, client, voiceprints, embedder, FakePublisher()
    )

    assert client.history_calls == []
    assert voiceprints.upserted == []


async def test_identify_history_skips_short_or_unsure_turns(tmp_path):
    s = settings(tmp_path)
    storage = FakeStorage(
        history_diarizations={
            PAST_SESSION: {
                "segments": [
                    named_turn(0.0, 1.5),  # too short
                    named_turn(30.0, 39.0, confidence=0.4),  # diarizer was unsure
                    named_turn(60.0, 66.0, confidence=0.9),  # kept
                ]
            }
        }
    )
    client = FakeClient(history=[history_entry()])
    voiceprints = FakeVoiceprints()
    embedder = FakeEmbedder(vectors=[VA])

    await process_identified(
        completed_event(), s, storage, client, voiceprints, embedder, FakePublisher()
    )

    assert len(voiceprints.upserted) == 1
    assert voiceprints.upserted[0][2]["history_key"] == f"{PAST_SESSION}#SPEAKER_00#0"
    # the history windows are embedded first (one decode of the past session),
    # the current session's relabel call follows
    assert embedder.windows_calls[0][1] == [(60.0, 66.0)]


# ------------------------------------------------------------------ finish


def assignment(label, status, user_id=USER_A, confidence=0.9):
    return {
        "speaker_label": label,
        "user_id": user_id,
        "confidence": confidence,
        "status": status,
    }


async def test_finish_parks_the_session_until_every_label_is_decided():
    """An unaccepted auto match (or an unnamed label) keeps the DM in the loop:
    the session must not move on with speakers nobody validated."""
    client = FakeClient()
    publisher = FakePublisher()

    await _finish(
        SESSION_ID,
        CAMPAIGN_ID,
        [
            assignment("SPEAKER_00", "confirmed"),
            assignment("SPEAKER_01", "auto"),
            assignment("SPEAKER_02", "pending", user_id=None, confidence=None),
        ],
        client,
        publisher,
    )

    assert [c[1] for c in client.status_calls] == ["speaker_pending"]
    pending = next(e for e in publisher.events if e.type == "speaker.pending")
    # only the unnamed label needs a NAME; every unconfirmed one needs a decision
    assert pending.payload["pending_labels"] == ["SPEAKER_02"]
    assert pending.payload["unconfirmed_labels"] == ["SPEAKER_01", "SPEAKER_02"]
    identified = next(e for e in publisher.events if e.type == "speakers.identified")
    assert identified.payload["pending_assignment"] is True


async def test_finish_closes_the_stage_when_nothing_is_left_to_decide():
    client = FakeClient()
    publisher = FakePublisher()

    await _finish(
        SESSION_ID,
        CAMPAIGN_ID,
        [assignment("SPEAKER_00", "confirmed")],
        client,
        publisher,
    )

    assert [c[1] for c in client.status_calls] == ["speakers_identified"]
    assert [e.type for e in publisher.events] == ["speakers.identified"]
    assert publisher.events[0].payload["pending_assignment"] is False


# ------------------------------------------------------------------ enroll
def assigned_event(**overrides):
    payload = {
        "session_id": SESSION_ID,
        "campaign_id": CAMPAIGN_ID,
        "label": "SPEAKER_00",
        "user_id": USER_B,
        "assigned_by": USER_A,
        "enrolled_voiceprint": False,
        "audio_uri": f"recordings/{SESSION_ID}/raw.m4a",
    }
    payload.update(overrides)
    return Event(type="speakers.assigned", payload=payload)


MEMBER_A = "44444444-4444-4444-4444-444444444444"


async def test_enroll_userless_member_learns_a_member_keyed_centroid(tmp_path):
    """A member with no linked account used to be unlearnable: enrollment keyed
    on user_id, so the worker logged 'skipping voiceprint enrollment' and that
    voice could never auto-match in any later session, however often the DM
    named it (attribution-model S12.3 defect 4).

    Now the print is keyed on member_id, so it is written even with no user: the
    legacy user-keyed collection is skipped (there is no user to key it on), the
    member voice model is not.
    """
    s = settings(tmp_path)
    storage = FakeStorage()
    voiceprints = FakeVoiceprints()
    member_models = FakeMemberModels()
    embedder = FakeEmbedder(duration=8.0)

    await process_assigned(
        assigned_event(user_id=None, member_id=MEMBER_A),
        s, storage, voiceprints, embedder,
        member_models=member_models,
    )
    assert voiceprints.upserted == []  # nothing to key a legacy print on
    assert len(member_models.upserted) == 1
    _, _, payload = member_models.upserted[0]
    assert payload["member_id"] == MEMBER_A
    assert "user_id" not in payload
    assert storage.downloads != []


async def test_enroll_without_any_identity_link_skips(tmp_path):
    s = settings(tmp_path)
    storage = FakeStorage()
    voiceprints = FakeVoiceprints()
    member_models = FakeMemberModels()

    await process_assigned(
        assigned_event(user_id=None, member_id=None),
        s, storage, voiceprints, FakeEmbedder(),
        member_models=member_models,
    )
    assert voiceprints.upserted == [] and member_models.upserted == []
    assert storage.downloads == []


async def test_enroll_writes_both_the_member_model_and_the_legacy_print(tmp_path):
    """The transition needs no big-bang re-enrollment: everything that still
    reads the user-keyed voiceprints collection keeps seeing new samples."""
    s = settings(tmp_path)
    member_models = FakeMemberModels()
    voiceprints = FakeVoiceprints()

    await process_assigned(
        assigned_event(member_id=MEMBER_A),
        s, FakeStorage(), voiceprints, FakeEmbedder(),
        member_models=member_models,
    )
    assert len(member_models.upserted) == 1
    assert len(voiceprints.upserted) == 1
    assert member_models.upserted[0][2]["member_id"] == MEMBER_A


async def test_enroll_refuses_a_mixture_print(tmp_path):
    """A label holding two people used to enroll a pooled mixture that matched
    neither of them, poisoning every later session (S12.3 defect 1). Now the
    identity's purity gates the whole enrollment."""
    s = settings(tmp_path)
    member_models = FakeMemberModels()
    voiceprints = FakeVoiceprints()

    await process_assigned(
        assigned_event(member_id=MEMBER_A, identity_purity=0.2),
        s, FakeStorage(), voiceprints, FakeEmbedder(),
        member_models=member_models,
    )
    assert member_models.upserted == [] and voiceprints.upserted == []


async def test_enroll_takes_the_purity_hint_over_its_own_estimate(tmp_path):
    s = settings(tmp_path)
    member_models = FakeMemberModels()

    await process_assigned(
        assigned_event(member_id=MEMBER_A, identity_purity=0.95),
        s, FakeStorage(), FakeVoiceprints(), FakeEmbedder(),
        member_models=member_models,
    )
    assert len(member_models.upserted) == 1
    assert member_models.upserted[0][2]["identity_purity"] == 0.95


async def test_enroll_upserts_voiceprint(tmp_path):
    s = settings(tmp_path)
    storage = FakeStorage()
    voiceprints = FakeVoiceprints()
    embedder = FakeEmbedder(duration=8.0)

    await process_assigned(assigned_event(), s, storage, voiceprints, embedder)

    assert len(voiceprints.upserted) == 1
    _, vector, payload = voiceprints.upserted[0]
    assert len(vector) == 192
    assert payload["user_id"] == USER_B
    assert payload["campaign_id"] == CAMPAIGN_ID
    assert payload["source"] == "session"
    assert payload["session_id"] == SESSION_ID
    assert payload["speaker_label"] == "SPEAKER_00"
    assert storage.downloads[0][1] == f"recordings/{SESSION_ID}/raw.m4a"
    assert storage.reads == [
        ("transcripts", f"transcripts/{SESSION_ID}/diarization.json")
    ]


async def test_enroll_missing_fields_skips(tmp_path):
    s = settings(tmp_path)
    storage = FakeStorage()
    voiceprints = FakeVoiceprints()
    embedder = FakeEmbedder()

    await process_assigned(assigned_event(audio_uri=None), s, storage, voiceprints, embedder)
    assert voiceprints.upserted == []
    assert storage.downloads == []


async def test_enroll_unknown_label_skips(tmp_path):
    s = settings(tmp_path)
    storage = FakeStorage()
    voiceprints = FakeVoiceprints()
    embedder = FakeEmbedder()

    await process_assigned(assigned_event(label="SPEAKER_99"), s, storage, voiceprints, embedder)
    assert voiceprints.upserted == []


async def test_enroll_too_short_turn_skips(tmp_path):
    """A turn shorter than voice_sample_min_sec is not embedded at all: a short
    clip is not weak evidence, it is no evidence."""
    s = settings(tmp_path)
    storage = FakeStorage(
        diarization={"segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}]}
    )
    voiceprints = FakeVoiceprints()

    await process_assigned(assigned_event(), s, storage, voiceprints, FakeEmbedder())
    assert voiceprints.upserted == []


async def test_enroll_skips_observations_below_the_quality_floor(tmp_path):
    """Quality is per observation, not per label: a clean turn of a named label
    still enrolls when a noisy one beside it does not."""
    s = settings(tmp_path, enroll_min_quality=0.9)
    storage = FakeStorage()
    voiceprints = FakeVoiceprints()

    await process_assigned(
        assigned_event(), s, storage, voiceprints, FakeEmbedder(snr_db=None)
    )
    assert voiceprints.upserted == []


# ------------------------------------------------- observations and evidence


def observations_settings(tmp_path, **overrides):
    return settings(tmp_path, observations_enabled=True, **overrides)


def vpoint(pid, score, **payload):
    return SimpleNamespace(id=pid, score=score, payload=payload)


async def test_identify_publishes_per_turn_voice_evidence(tmp_path):
    """The evidence block replaces 'one pooled cosine, one verdict': every turn
    gets its own vector, quality and nearest-centroid list."""
    s = observations_settings(tmp_path)
    storage = FakeStorage()
    member_models = FakeMemberModels(
        [vpoint("p1", 0.83, member_id="m-member", source="session")]
    )
    observations = FakeObservations()

    await process_identified(
        completed_event(), s, storage, FakeClient(), FakeVoiceprints(),
        FakeEmbedder(vectors=[VA, VB], snr_db=25.0), FakePublisher(),
        member_models=member_models, observations_store=observations,
    )

    assert len(observations.batches) == 1
    points = observations.batches[0]
    # The observation id is '<label>#<turn position in the session>': the turn
    # index is global, so the key stays unique however the turns are labelled.
    assert [p[2]["observation_id"] for p in points] == ["SPEAKER_00#0", "SPEAKER_01#1"]
    # 4 s of a 6 s saturation ramp, clean SNR: high but not perfect.
    assert all(p[2]["quality"] > 0.7 for p in points)
    assert points[0][2]["session_id"] == SESSION_ID


async def test_identify_evidence_lists_nearest_centroids(tmp_path):
    s = observations_settings(tmp_path)
    member_models = FakeMemberModels(
        [
            vpoint("c1", 0.81, member_id="m-1", source="session"),
            vpoint("c2", 0.55, user_id="u-2", source="enrollment"),
        ]
    )
    publisher = FakePublisher()

    await process_identified(
        completed_event(), s, FakeStorage(), FakeClient(), FakeVoiceprints(),
        FakeEmbedder(vectors=[VA, VB]), publisher,
        member_models=member_models, observations_store=FakeObservations(),
    )
    identified = next(e for e in publisher.events if e.type == "speakers.identified")
    evidence = identified.payload["evidence"]
    assert evidence["model_version"] == s.voice_embedding_model
    assert len(evidence["observations"]) == 2
    nearest = evidence["observations"][0]["nearest_centroids"]
    assert nearest[0]["member_id"] == "m-1"
    assert nearest[0]["cosine"] == 0.81
    assert nearest[1]["user_id"] == "u-2"


async def test_identify_evidence_is_absent_when_the_flag_is_off(tmp_path):
    publisher = FakePublisher()
    await process_identified(
        completed_event(), settings(tmp_path), FakeStorage(), FakeClient(),
        FakeVoiceprints(), FakeEmbedder(vectors=[VA, VB]), publisher,
    )
    identified = next(e for e in publisher.events if e.type == "speakers.identified")
    assert "evidence" not in identified.payload


async def test_identify_survives_an_unreadable_recording(tmp_path):
    """No embeddings is a degraded run, not a failed one: the text channels must
    still get their chance (attribution-model S16)."""
    publisher = FakePublisher()
    # The refined path is the one that tolerates an embedding failure (the raw
    # labels are matched directly, per label, best-effort); the re-clustering
    # path cannot, because clustering IS the embedding step.
    await process_identified(
        refined_event(), observations_settings(tmp_path), FakeStorage(),
        FakeClient(), FakeVoiceprints(), FakeEmbedder(raise_on=True), publisher,
        member_models=FakeMemberModels(), observations_store=FakeObservations(),
    )
    identified = next(e for e in publisher.events if e.type == "speakers.identified")
    evidence = identified.payload["evidence"]
    assert len(evidence["observations"]) == 2
    assert all(o["quality"] is None for o in evidence["observations"])
    assert all(o["nearest_centroids"] == [] for o in evidence["observations"])


async def test_identify_survives_an_unreachable_observation_store(tmp_path):
    class ExplodingObservations:
        async def upsert_many(self, points):
            raise RuntimeError("qdrant down")

    publisher = FakePublisher()
    await process_identified(
        completed_event(), observations_settings(tmp_path), FakeStorage(),
        FakeClient(), FakeVoiceprints(), FakeEmbedder(vectors=[VA, VB]), publisher,
        member_models=FakeMemberModels(), observations_store=ExplodingObservations(),
    )
    identified = next(e for e in publisher.events if e.type == "speakers.identified")
    assert len(identified.payload["evidence"]["observations"]) == 2


def test_anchor_match_below_the_threshold_stays_pending(tmp_path):
    """The anchor path used to emit 'auto' unconditionally, so a 0.60 snap was
    auto-assigned where the direct path demanded 0.75 (S12.3 defect 2)."""
    from app.identify import anchor_verdict

    s = settings(tmp_path)
    weak = anchor_verdict("SPEAKER_00", user_id=USER_A, score=0.62, settings=s)
    strong = anchor_verdict("SPEAKER_01", user_id=USER_A, score=0.80, settings=s)
    assert weak["status"] == "pending" and weak["user_id"] is None
    assert weak["confidence"] == 0.62  # the DM still sees how close it was
    assert strong["status"] == "auto" and strong["user_id"] == USER_A


async def test_observation_quality_penalises_overlap(tmp_path):
    s = observations_settings(tmp_path)
    observations = FakeObservations()
    await process_identified(
        completed_event(), s, FakeStorage(), FakeClient(), FakeVoiceprints(),
        FakeEmbedder(vectors=[VA, VB], snr_db=25.0, overlap_ratio=0.45),
        FakePublisher(),
        member_models=FakeMemberModels(), observations_store=observations,
    )
    # Down-weighted, not discarded: overlap is a prior, never a veto.
    qualities = [p[2]["quality"] for p in observations.batches[0]]
    assert all(0.0 < q < 0.7 for q in qualities)


async def test_observations_use_the_relabelled_segments(tmp_path):
    """Re-clustering overrides the raw labels; the observations must be filed
    under the new ones, or the engine's voice evidence points at labels nothing
    else uses."""
    s = observations_settings(tmp_path)
    observations = FakeObservations()
    await process_identified(
        completed_event(segments=cross_chunk_segments()), s,
        FakeStorage(diarization={"segments": cross_chunk_segments()}),
        FakeClient(), FakeVoiceprints(), FakeEmbedder(vectors=[VA, VA]), FakePublisher(),
        member_models=FakeMemberModels(), observations_store=observations,
    )
    ids = [p[2]["observation_id"] for p in observations.batches[0]]
    assert ids == ["SPEAKER_00#0", "SPEAKER_00#1"]
    assert {p[2]["label"] for p in observations.batches[0]} == {"SPEAKER_00"}