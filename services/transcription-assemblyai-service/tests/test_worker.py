"""Tests for the transcription worker end-to-end flow (process_job).

storage / clients / transcriber / publisher are all fakes; no broker, MinIO,
campaign-service or AssemblyAI involved. decode_to_wav / split_wav are
monkeypatched (the real ones are covered in test_audio.py); offset_segments
and the artifact builders are the real pure code, so chunk splicing is
validated end-to-end.
"""

import json
import os

import pytest
from dnd_common.events import Event

from app import workers
from app.audio import AudioChunk
from app.clients.assemblyai_transcriber import DiarizedTranscription
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


class FakeCampaignClient:
    """Campaign context: language, roster, name/description."""

    def __init__(self, campaign=None, members=None, error=None):
        self.campaign = campaign or {
            "id": CAMPAIGN_ID,
            "name": "The Fellowship",
            "description": "Quest for the Ring.",
            "language": "it",
            "dm_user_id": "u1",
        }
        self.members = members if members is not None else [
            {"id": "m1", "role": "dm", "character_name": "", "player_name": "Alice"},
            {"id": "m2", "role": "player", "character_name": "Aragorn", "player_name": "Bob"},
            {"id": "m3", "role": "player", "character_name": "Legolas", "player_name": "Carol"},
        ]
        self.error = error
        self.get_calls = []
        self.members_calls = []

    async def get_campaign(self, campaign_id):
        self.get_calls.append(campaign_id)
        if self.error is not None:
            raise self.error
        return self.campaign

    async def list_members(self, campaign_id):
        self.members_calls.append(campaign_id)
        if self.error is not None:
            raise self.error
        return self.members


class FakeTranscriber:
    """Per-chunk diarized output; records the campaign params per call."""

    def __init__(self, error=None):
        self.error = error
        self.calls = []  # (wav_path, language, speakers_expected, prompt, keyterms)

    async def transcribe(
        self,
        wav_path,
        *,
        language=None,
        speakers_expected=None,
        prompt=None,
        keyterms=None,
    ):
        self.calls.append((wav_path, language, speakers_expected, prompt, list(keyterms or [])))
        if self.error is not None:
            raise self.error
        if wav_path == "chunk_0000.wav":
            return DiarizedTranscription(
                language="it",
                segments=[
                    {
                        "start": 1.0,
                        "end": 2.0,
                        "speaker": "SPEAKER_00",
                        "text": "uno",
                        "words": [{"word": "uno", "start": 1.0, "end": 2.0}],
                        "speaker_confidence": 0.97,
                        "confidence": 0.97,
                    }
                ],
            )
        return DiarizedTranscription(
            language=None,  # the campaign language is reused by the worker
            segments=[
                {
                    "start": 0.5,
                    "end": 1.5,
                    "speaker": "SPEAKER_01",
                    "text": "due",
                    "words": [{"word": "due", "start": 0.5, "end": 1.5}],
                    "speaker_confidence": 0.72,  # low -> the UI flags this label
                    "confidence": 0.72,
                }
            ],
        )


class FakePublisher:
    def __init__(self):
        self.events = []

    async def __call__(self, event):
        self.events.append(event)


CHUNKS = [AudioChunk(0.0, 5.0, "chunk_0000.wav"), AudioChunk(5.0, 10.0, "chunk_0001.wav")]


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    settings = ServiceSettings(work_dir=str(tmp_path))
    storage = FakeStorage()
    client = FakeClient()
    transcriber = FakeTranscriber()
    campaign_client = FakeCampaignClient()
    publisher = FakePublisher()
    decoded = []

    monkeypatch.setattr(
        workers.transcribe, "decode_to_wav", lambda src, dst: decoded.append((src, dst))
    )
    monkeypatch.setattr(
        workers.transcribe,
        "split_wav",
        lambda wav, chunk_seconds, max_bytes, out_dir: CHUNKS,
    )
    return settings, storage, client, transcriber, campaign_client, publisher, decoded


async def test_process_job_chunks_and_publishes(fakes):
    settings, storage, client, transcriber, campaign_client, publisher, decoded = fakes

    await process_job(
        make_event(),
        settings,
        storage,
        client,
        transcriber,
        publisher,
        campaign_client,
    )

    # campaign context fetched once per endpoint
    assert campaign_client.get_calls == [CAMPAIGN_ID]
    assert campaign_client.members_calls == [CAMPAIGN_ID]

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

    # decoded to a wav (ffmpeg) before splitting
    assert len(decoded) == 1
    assert decoded[0][0] == staged
    assert decoded[0][1].endswith(".wav")

    # one API call per chunk; campaign language + speaker count + prompt +
    # keyterms are passed through on every chunk
    expected_prompt = transcriber.calls[0][3]
    assert "Dungeons & Dragons" in expected_prompt
    assert "The campaign is named" in expected_prompt
    assert transcriber.calls == [
        ("chunk_0000.wav", "it", 3, expected_prompt, ["Alice", "Aragorn", "Legolas"]),
        ("chunk_0001.wav", "it", 3, expected_prompt, ["Alice", "Aragorn", "Legolas"]),
    ]

    # artifacts written after every chunk (2 chunks -> 4 uploads), same URIs
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
    assert final_transcript["model"] == "universal-3-5-pro"
    assert [(s["start"], s["end"], s["text"]) for s in final_transcript["segments"]] == [
        (1.0, 2.0, "uno"),
        (5.5, 6.5, "due"),  # chunk 2 offset by 5.0 s
    ]
    # word timings survive into the transcript
    assert final_transcript["segments"][0]["words"] == [
        {"word": "uno", "start": 1.0, "end": 2.0}
    ]
    # per-segment confidence survives into both artifacts
    assert final_transcript["segments"][0]["speaker_confidence"] == 0.97
    assert final_transcript["segments"][1]["speaker_confidence"] == 0.72
    diarization = json.loads(storage.uploads[3][2])
    assert diarization["segments"][1] == {
        "start": 5.5, "end": 6.5, "speaker": "SPEAKER_01", "text": "due", "chunk": 1,
        "speaker_confidence": 0.72,
        "confidence": 0.72,
    }

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
    assert [s["text"] for s in progress[1].payload["segments"]] == ["uno", "due"]

    # the completed event carries the full offset timeline
    completed = [ev for ev in publisher.events if ev.type == "transcription.completed"]
    assert len(completed) == 1
    assert completed[0].payload["language"] == "it"
    assert [(s["start"], s["end"]) for s in completed[0].payload["segments"]] == [
        (1.0, 2.0),
        (5.5, 6.5),
    ]

    # staged audio + decoded wav cleaned up
    assert not os.path.exists(staged)
    assert not os.path.exists(decoded[0][1])


async def test_process_job_without_campaign_context(fakes):
    """campaign-service down -> auto language/speakers, built-in prompt."""
    settings, storage, client, transcriber, campaign_client, publisher, _decoded = fakes
    campaign_client.error = RuntimeError("campaign-service unreachable")

    await process_job(
        make_event(),
        settings,
        storage,
        client,
        transcriber,
        publisher,
        campaign_client,
    )

    # no campaign language/speaker count/keyterms; built-in prompt only
    assert transcriber.calls[0][1] is None  # language
    assert transcriber.calls[0][2] is None  # speakers_expected
    assert "Dungeons & Dragons" in transcriber.calls[0][3]
    assert transcriber.calls[0][4] == []
    # still completes normally
    assert client.status_calls[-1] == (SESSION_ID, "transcribed", None)


async def test_process_job_duration_fallback(fakes):
    """No duration in the event -> derived from the last segment end."""
    settings, storage, client, transcriber, campaign_client, publisher, _decoded = fakes
    await process_job(
        make_event(duration_sec=None),
        settings,
        storage,
        client,
        transcriber,
        publisher,
        campaign_client,
    )
    assert client.artifact_calls[-1][3] == 6.5


async def test_process_job_marks_failed_and_reraises(fakes):
    settings, storage, client, _transcriber, campaign_client, publisher, _decoded = fakes
    failing = FakeTranscriber(error=RuntimeError("assemblyai 500"))

    with pytest.raises(RuntimeError, match="assemblyai 500"):
        await process_job(
            make_event(),
            settings,
            storage,
            client,
            failing,
            publisher,
            campaign_client,
        )

    assert client.status_calls[-1] == (SESSION_ID, "failed", "assemblyai 500")
    # no completed event
    assert publisher.events == []


async def test_process_job_marks_failed_on_cancellation(fakes):
    """Cancellation (broker channel close / shutdown) must not leave the
    session stuck in 'transcribing': mark failed, then re-raise."""
    import asyncio

    settings, storage, client, _transcriber, campaign_client, publisher, _decoded = fakes
    cancelling = FakeTranscriber(error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await process_job(
            make_event(),
            settings,
            storage,
            client,
            cancelling,
            publisher,
            campaign_client,
        )

    assert client.status_calls[-1] == (SESSION_ID, "failed", "cancelled")
    # no completed event
    assert publisher.events == []


async def test_process_job_idempotent_redelivery(fakes):
    """409 on entering transcribing -> ack, no work done."""
    settings, storage, client, transcriber, campaign_client, publisher, decoded = fakes
    client.conflict_on_transcribing = True

    await process_job(
        make_event(),
        settings,
        storage,
        client,
        transcriber,
        publisher,
        campaign_client,
    )

    assert storage.downloads == []
    assert storage.uploads == []
    assert transcriber.calls == []
    assert decoded == []
    assert client.status_calls == []
    assert publisher.events == []
