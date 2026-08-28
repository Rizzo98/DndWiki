"""Worker tests: relabel + identification + enrollment flows (all I/O faked)."""

from types import SimpleNamespace

import pytest
from dnd_common.events import Event

from app.clients.session_service import ConflictTransition
from app.core.config import ServiceSettings
from app.workers.identify import process_assigned, process_identified

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
    def __init__(self, diarization=None, transcript=None):
        self.downloads = []
        self.reads = []
        self.writes = []
        self.diarization = diarization if diarization is not None else {"segments": segments()}
        self.transcript = transcript

    async def download_file(self, bucket, key, dest_path):
        self.downloads.append((bucket, key, dest_path))
        with open(dest_path, "wb") as f:  # noqa: ASYNC230 - test fake
            f.write(b"fake-audio")

    async def read_json(self, bucket, key):
        self.reads.append((bucket, key))
        if key.endswith("diarization.json"):
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
    def __init__(self, conflict=False):
        self.status_calls = []
        self.upserts = []
        self.conflict = conflict

    async def update_status(self, session_id, status, error=None):
        if self.conflict and status == "identifying_speakers":
            raise ConflictTransition("already moved on")
        self.status_calls.append((session_id, status, error))
        return {"id": session_id, "status": status}

    async def upsert_speakers(self, session_id, items):
        self.upserts.append((session_id, items))
        return items


class FakeVoiceprints:
    def __init__(self, hits=None, anchors=None):
        self.hits = hits or []
        self.searches = []
        self.upserted = []
        self.anchors = anchors or []
        self.list_calls = []

    async def search(self, embedding, campaign_id, limit=1):
        self.searches.append((list(embedding), campaign_id, limit))
        return self.hits

    async def list_campaign(self, campaign_id):
        self.list_calls.append(campaign_id)
        return self.anchors

    async def upsert(self, point_id, vector, payload):
        self.upserted.append((point_id, list(vector), payload))


class FakeEmbedder:
    def __init__(self, vectors=None, duration=5.0, raise_on=False):
        self.vectors = vectors
        self.duration = duration
        self.raise_on = raise_on
        self.windows_calls = []

    def embed_windows_sync(self, audio_path, windows):
        self.windows_calls.append((audio_path, list(windows)))
        if self.raise_on:
            raise RuntimeError("embed failed")
        if self.vectors is not None:
            return list(self.vectors)
        return [[0.1] * 192 for _ in windows]

    async def embed_bytes(self, data, suffix=".wav"):
        if self.raise_on:
            raise RuntimeError("embed failed")
        return [0.1] * 192, self.duration


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

    assert [c[1] for c in client.status_calls] == ["identifying_speakers", "speakers_identified"]
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
    assert client.status_calls[-1][1] == "speakers_identified"


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

    assert client.status_calls[-1][1] == "speakers_identified"
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

    assert [c[1] for c in client.status_calls] == ["identifying_speakers", "speakers_identified"]
    # matched directly, no artifact rewrite (the refiner already wrote them);
    # each label is embedded from its pooled audio window and cosine-searched
    assert storage.writes == []
    assert len(voiceprints.searches) == 2
    assert client.upserts[0][1] == [
        {"speaker_label": "SPEAKER_00", "user_id": USER_A, "confidence": 0.93, "status": "auto"},
        {"speaker_label": "SPEAKER_01", "user_id": USER_A, "confidence": 0.93, "status": "auto"},
    ]


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


async def test_enroll_userless_member_skips(tmp_path):
    """A member without a linked user is named, but there is no user-keyed
    voiceprint to enroll — the assignment stands, enrollment is skipped."""
    s = settings(tmp_path)
    storage = FakeStorage()
    voiceprints = FakeVoiceprints()
    embedder = FakeEmbedder(duration=8.0)

    await process_assigned(
        assigned_event(user_id=None, member_id="44444444-4444-4444-4444-444444444444"),
        s, storage, voiceprints, embedder,
    )
    assert voiceprints.upserted == []
    assert storage.downloads == []


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


async def test_enroll_too_short_clip_skips(tmp_path):
    s = settings(tmp_path)
    storage = FakeStorage()
    voiceprints = FakeVoiceprints()
    embedder = FakeEmbedder(duration=1.0)

    await process_assigned(assigned_event(), s, storage, voiceprints, embedder)
    assert voiceprints.upserted == []