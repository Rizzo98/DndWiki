"""Tests for the transcription worker end-to-end flow (process_job).

storage / client / pipeline / publisher are all fakes; no broker or MinIO
involved. whisperx is never imported (load_audio / split_audio /
transcribe_audio are monkeypatched; offset_segments and the artifact
builders are real, pure code).
"""

import json
import os

import pytest
from dnd_common.events import Event

from app import workers
from app.clients.session_service import ConflictTransition
from app.core.config import ServiceSettings
from app.workers.transcribe import process_job

SESSION_ID = "11111111-1111-1111-1111-111111111111"
CAMPAIGN_ID = "22222222-2222-2222-2222-222222222222"


def make_event(**overrides) -> Event:
    payload = {
        "session_id": SESSION_ID,
        "campaign_id": CAMPAIGN_ID,
        "recorded_by": "33333333-3333-3333-3333-333333333333",
        "audio_uri": f"recordings/{SESSION_ID}/raw.m4a",
        "duration_sec": 3724.0,
    }
    payload.update(overrides)
    return Event(type="session.recorded", payload=payload)


class FakeStorage:
    """Records downloads/uploads; creates the staged file on download."""

    def __init__(self):
        self.downloads = []
        self.uploads = []

    async def download_file(self, bucket, key, dest_path):
        self.downloads.append((bucket, key, dest_path))
        with open(dest_path, "wb") as f:  # noqa: ASYNC230 - test fake
            f.write(b"fake-audio")

    async def put_bytes(self, bucket, key, data, content_type):
        self.uploads.append((bucket, key, data, content_type))


class FakeClient:
    """Records status/artifact calls; can be configured to 409."""

    def __init__(self, conflict_on_transcribing=False):
        self.status_calls = []
        self.artifact_calls = []
        self.conflict_on_transcribing = conflict_on_transcribing

    async def update_status(self, session_id, status, error=None):
        if self.conflict_on_transcribing and status == "transcribing":
            raise ConflictTransition("already moved on")
        self.status_calls.append((session_id, status, error))
        return {"id": session_id, "status": status}

    async def update_artifacts(self, session_id, transcript_uri, diarization_uri, duration_sec):
        self.artifact_calls.append(
            (session_id, transcript_uri, diarization_uri, duration_sec)
        )
        return {"id": session_id}


class FakePublisher:
    def __init__(self):
        self.events = []

    async def __call__(self, event):
        self.events.append(event)


CHUNK0 = "CHUNK0"
CHUNK1 = "CHUNK1"


def chunk_result(chunk):
    """Per-chunk whisperx-style results (timestamps relative to chunk start)."""
    if chunk == CHUNK0:
        return {
            "language": "it",
            "segments": [
                {
                    "start": 1.0,
                    "end": 2.0,
                    "text": "uno",
                    "speaker": "SPEAKER_00",
                    "words": [{"word": "uno", "start": 1.0, "end": 2.0}],
                }
            ],
        }
    return {
        "language": "it",
        "segments": [
            {
                "start": 0.5,
                "end": 1.5,
                "text": "due",
                "speaker": "SPEAKER_01",
                "words": [],
            }
        ],
    }


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    settings = ServiceSettings(work_dir=str(tmp_path))
    storage = FakeStorage()
    client = FakeClient()
    publisher = FakePublisher()
    calls = {"languages": []}

    monkeypatch.setattr(workers.transcribe, "load_audio", lambda path: "AUDIO")
    monkeypatch.setattr(
        workers.transcribe,
        "split_audio",
        lambda audio, chunk_seconds: [(0.0, 5.0, CHUNK0), (5.0, 10.0, CHUNK1)],
    )

    def fake_transcribe_audio(audio_chunk, settings, language=None):
        calls["languages"].append(language)
        return chunk_result(audio_chunk)

    monkeypatch.setattr(workers.transcribe, "transcribe_audio", fake_transcribe_audio)
    return settings, storage, client, publisher, calls


async def test_process_job_chunks_and_publishes_progress(fakes):
    settings, storage, client, publisher, calls = fakes

    await process_job(make_event(), settings, storage, client, publisher)

    # pipeline state: transcribing once at the start, transcribed at the end
    assert client.status_calls == [
        (SESSION_ID, "transcribing", None),
        (SESSION_ID, "transcribed", None),
    ]

    # audio downloaded from the recordings bucket into the work dir
    assert storage.downloads[0][0] == "recordings"
    assert storage.downloads[0][1] == f"recordings/{SESSION_ID}/raw.m4a"
    staged = storage.downloads[0][2]
    assert staged.startswith(str(settings.work_dir))
    assert staged.endswith(".m4a")

    # artifacts (transcript + diarization) written after every chunk, same URIs
    keys = [u[1] for u in storage.uploads]
    assert keys == [
        f"transcripts/{SESSION_ID}/transcript.json",
        f"transcripts/{SESSION_ID}/diarization.json",
        f"transcripts/{SESSION_ID}/transcript.json",
        f"transcripts/{SESSION_ID}/diarization.json",
    ]
    # the final transcript carries both chunks spliced into the session timeline
    final_transcript = json.loads(storage.uploads[2][2])
    assert final_transcript["language"] == "it"
    assert [(s["start"], s["end"], s["text"]) for s in final_transcript["segments"]] == [
        (1.0, 2.0, "uno"),
        (5.5, 6.5, "due"),
    ]
    assert final_transcript["segments"][0]["words"][0]["start"] == 1.0

    # progress updates: after each chunk + one final authoritative duration
    t_uri = f"transcripts/{SESSION_ID}/transcript.json"
    d_uri = f"transcripts/{SESSION_ID}/diarization.json"
    assert client.artifact_calls == [
        (SESSION_ID, t_uri, d_uri, 2.0),
        (SESSION_ID, t_uri, d_uri, 6.5),
        (SESSION_ID, t_uri, d_uri, 3724.0),
    ]

    # one transcription.progress event per chunk, cumulative segments
    progress = [ev for ev in publisher.events if ev.type == "transcription.progress"]
    assert [p.payload["chunk_index"] for p in progress] == [0, 1]
    assert [p.payload["total_chunks"] for p in progress] == [2, 2]
    assert progress[0].payload["segments"] == [
        {"start": 1.0, "end": 2.0, "speaker": "SPEAKER_00", "text": "uno", "chunk": 0},
    ]
    assert [s["text"] for s in progress[1].payload["segments"]] == ["uno", "due"]

    # the completed event carries the full offset timeline
    completed = [ev for ev in publisher.events if ev.type == "transcription.completed"]
    assert len(completed) == 1
    assert completed[0].payload["language"] == "it"
    assert [(s["start"], s["end"]) for s in completed[0].payload["segments"]] == [
        (1.0, 2.0),
        (5.5, 6.5),
    ]

    # language detected on the first chunk is reused on the second
    assert calls["languages"] == [None, "it"]

    # staged audio cleaned up
    assert not os.path.exists(staged)


async def test_process_job_duration_fallback(fakes):
    """No duration in the event -> derived from the last segment end."""
    settings, storage, client, publisher, _calls = fakes
    await process_job(make_event(duration_sec=None), settings, storage, client, publisher)
    assert client.artifact_calls[-1][3] == 6.5


async def test_process_job_marks_failed_and_reraises(fakes):
    settings, storage, client, publisher, _calls = fakes
    import app.workers.transcribe as worker_mod

    def failing_transcribe_audio(audio_chunk, settings, language=None):
        raise RuntimeError("cuda oom")

    original = worker_mod.transcribe_audio
    worker_mod.transcribe_audio = failing_transcribe_audio
    try:
        with pytest.raises(RuntimeError, match="cuda oom"):
            await process_job(make_event(), settings, storage, client, publisher)
    finally:
        worker_mod.transcribe_audio = original

    assert client.status_calls[-1] == (SESSION_ID, "failed", "cuda oom")
    # no completed event
    assert publisher.events == []


async def test_process_job_marks_failed_on_cancellation(fakes):
    """Cancellation (broker channel close / shutdown) must not leave the
    session stuck in 'transcribing': mark failed, then re-raise."""
    import asyncio

    import app.workers.transcribe as worker_mod

    settings, storage, client, publisher, _calls = fakes

    def cancelling_transcribe_audio(audio_chunk, settings, language=None):
        raise asyncio.CancelledError()

    original = worker_mod.transcribe_audio
    worker_mod.transcribe_audio = cancelling_transcribe_audio
    try:
        with pytest.raises(asyncio.CancelledError):
            await process_job(make_event(), settings, storage, client, publisher)
    finally:
        worker_mod.transcribe_audio = original

    assert client.status_calls[-1] == (SESSION_ID, "failed", "cancelled")
    # no completed event
    assert publisher.events == []


async def test_process_job_idempotent_redelivery(fakes):
    """409 on entering transcribing -> ack, no work done."""
    settings, storage, client, publisher, _calls = fakes
    client.conflict_on_transcribing = True

    await process_job(make_event(), settings, storage, client, publisher)

    assert storage.downloads == []
    assert storage.uploads == []
    assert client.status_calls == []
    assert publisher.events == []
