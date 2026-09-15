"""Refiner worker tests (all I/O faked): refine, passthrough, failure paths."""

import pytest
from dnd_common.events import Event

from app.clients.session_service import ConflictTransition
from app.core.config import ServiceSettings
from app.workers.refine import process_job

SESSION_ID = "11111111-1111-1111-1111-111111111111"
CAMPAIGN_ID = "22222222-2222-2222-2222-222222222222"


def seg(start, end, speaker="SPEAKER_00", chunk=0, text="hello there"):
    return {"start": start, "end": end, "speaker": speaker, "chunk": chunk, "text": text}


def segments():
    return [
        seg(0.0, 4.0, "Speaker A", 0, "we need to find the artifact"),
        seg(5.0, 9.0, "Speaker B", 0, "I agree let us go"),
    ]


def transcript_segments():
    return [
        {"start": 0.0, "end": 4.0, "speaker": "Speaker A", "text": "we need to find the artifact", "words": [], "chunk": 0},
        {"start": 5.0, "end": 9.0, "speaker": "Speaker B", "text": "I agree let us go", "words": [], "chunk": 0},
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
        self.reads = []
        self.writes = []
        self.diarization = diarization if diarization is not None else {"segments": segments()}
        self.transcript = transcript

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
        self.conflict = conflict

    async def update_status(self, session_id, status, error=None):
        if self.conflict and status == "refining":
            raise ConflictTransition("already moved on")
        self.status_calls.append((session_id, status, error))
        return {"id": session_id, "status": status}


class FakeLLM:
    """Returns canned per-window decisions; records window metadata."""

    def __init__(self, decisions=None):
        self.decisions = decisions or {
            0: ("SPEAKER_01", "we must find the relic"),
            1: ("SPEAKER_00", "agreed let us go"),
        }
        self.calls = []

    async def refine_window(
        self, turns, *, context_blocks, fixed_count, window_index, total_windows,
        cast_lines=None, member_count=None, include_speakers=True
    ):
        self.calls.append(
            (
                list(turns),
                list(context_blocks),
                fixed_count,
                window_index,
                total_windows,
                list(cast_lines or []),
                member_count,
                include_speakers,
            )
        )
        if not include_speakers:
            # A text-only response has no speaker field; the labels must survive
            # the whole pass untouched.
            return {index: ("", text) for index, (_, text) in self.decisions.items()}
        return dict(self.decisions)


class FakeCampaignClient:
    """Returns a canned roster; records fetch calls, optionally failing."""

    def __init__(self, members=None, error=None):
        self.members = members or [
            {
                "role": "dm",
                "player_name": "Gandalf",
                "character_name": "Dungeon Master",
                "character_description": None,
            },
            {
                "role": "player",
                "player_name": "Alice",
                "character_name": "Rowan",
                "character_description": "Tall half-elf rogue with a silver braid",
            },
        ]
        self.error = error
        self.calls = []

    async def list_members(self, campaign_id):
        self.calls.append(campaign_id)
        if self.error is not None:
            raise self.error
        return list(self.members)


class FakePublisher:
    def __init__(self):
        self.events = []

    async def __call__(self, event):
        self.events.append(event)


def settings(**overrides):
    kwargs = {
        "refiner_enabled": True,
        "refiner_window_turns": 100,
        "refiner_window_overlap": 15,
    }
    kwargs.update(overrides)
    return ServiceSettings(**kwargs)


# ------------------------------------------------------------------- refine


async def test_refine_happy_path(tmp_path):
    s = settings()
    storage = FakeStorage(transcript={"segments": transcript_segments()})
    client = FakeClient()
    llm = FakeLLM()
    publisher = FakePublisher()

    await process_job(completed_event(), s, storage, client, llm, publisher)

    assert [c[1] for c in client.status_calls] == ["refining", "refined"]
    assert len(llm.calls) == 1
    assert llm.calls[0][3] == 0 and llm.calls[0][4] == 1  # window 1/1

    refined = next(e for e in publisher.events if e.type == "transcription.refined")
    assert refined.payload["refined"] is True
    # audio_uri is forwarded so speaker-service can embed voice windows
    assert refined.payload["audio_uri"] == f"recordings/{SESSION_ID}/raw.m4a"
    assert refined.payload["refiner"]["model"] == "deepseek/deepseek-chat"
    assert [seg["speaker"] for seg in refined.payload["segments"]] == [
        "SPEAKER_01", "SPEAKER_00",
    ]
    assert refined.payload["segments"][0]["text"] == "we must find the relic"

    # artifacts rewritten in place, preserving timings and other keys
    dkey = f"transcripts/{SESSION_ID}/diarization.json"
    tkey = f"transcripts/{SESSION_ID}/transcript.json"
    assert [w[1] for w in storage.writes] == [dkey, tkey]
    dseg, tseg = storage.writes[0][2], storage.writes[1][2]
    assert dseg["refiner"]["enabled"] is True
    assert [s["speaker"] for s in dseg["segments"]] == ["SPEAKER_01", "SPEAKER_00"]
    assert tseg["segments"][0]["speaker"] == "SPEAKER_01"
    assert tseg["segments"][0]["words"] == []  # other keys preserved


async def test_refine_injects_campaign_cast(tmp_path):
    s = settings()
    storage = FakeStorage(transcript={"segments": transcript_segments()})
    client = FakeClient()
    llm = FakeLLM()
    campaign = FakeCampaignClient()
    publisher = FakePublisher()

    await process_job(
        completed_event(), s, storage, client, llm, publisher, campaign_client=campaign
    )

    assert campaign.calls == [CAMPAIGN_ID]
    # cast lines reach every window call (here: the single window)
    assert llm.calls[0][5] == [
        "- Dungeon Master (Gandalf) (dm, narrator)",
        "- Rowan (Alice) — Tall half-elf rogue with a silver braid",
    ]
    # the roster size is forwarded as the max-speaker hint
    assert llm.calls[0][6] == 2
    refined = next(e for e in publisher.events if e.type == "transcription.refined")
    assert refined.payload["refiner"]["cast"]["injected"] is True
    assert refined.payload["refiner"]["cast"]["members"] == 2
    assert refined.payload["refiner"]["cast"]["with_description"] == 1
    assert refined.payload["refiner"]["cast"]["table_size"] == 2


async def test_refine_cast_failure_is_best_effort(tmp_path):
    """A campaign-service outage never fails refinement."""
    s = settings()
    storage = FakeStorage(transcript={"segments": transcript_segments()})
    client = FakeClient()
    llm = FakeLLM()
    campaign = FakeCampaignClient(error=RuntimeError("campaign-service down"))
    publisher = FakePublisher()

    await process_job(
        completed_event(), s, storage, client, llm, publisher, campaign_client=campaign
    )

    assert [c[1] for c in client.status_calls] == ["refining", "refined"]
    assert llm.calls[0][5] == []  # no cast lines, still refined
    refined = next(e for e in publisher.events if e.type == "transcription.refined")
    assert refined.payload["refiner"]["cast"]["injected"] is False
    assert refined.payload["segments"][0]["speaker"] == "SPEAKER_01"


async def test_refine_no_campaign_client_refines_without_cast(tmp_path):
    s = settings()
    storage = FakeStorage(transcript={"segments": transcript_segments()})
    client = FakeClient()
    llm = FakeLLM()
    publisher = FakePublisher()

    await process_job(completed_event(), s, storage, client, llm, publisher)

    assert llm.calls[0][5] == []
    refined = next(e for e in publisher.events if e.type == "transcription.refined")
    assert refined.payload["refiner"]["cast"]["injected"] is False


async def test_refine_disabled_passthrough(tmp_path):
    s = settings(refiner_enabled=False)
    storage = FakeStorage()
    client = FakeClient()
    llm = FakeLLM()
    publisher = FakePublisher()

    await process_job(completed_event(), s, storage, client, llm, publisher)

    assert client.status_calls == []
    assert storage.writes == []
    assert llm.calls == []
    assert publisher.events == []


async def test_refine_redelivery_skips(tmp_path):
    s = settings()
    storage = FakeStorage()
    client = FakeClient(conflict=True)
    llm = FakeLLM()
    publisher = FakePublisher()

    await process_job(completed_event(), s, storage, client, llm, publisher)

    assert storage.writes == []
    assert llm.calls == []
    assert publisher.events == []
    assert client.status_calls == []


async def test_refine_failure_marks_failed(tmp_path):
    s = settings()

    class BoomLLM(FakeLLM):
        async def refine_window(self, turns, **kwargs):
            raise RuntimeError("llm down")

    storage = FakeStorage()
    client = FakeClient()
    llm = BoomLLM()
    publisher = FakePublisher()

    with pytest.raises(RuntimeError, match="llm down"):
        await process_job(completed_event(), s, storage, client, llm, publisher)

    assert client.status_calls[-1] == (SESSION_ID, "failed", "llm down")
    assert publisher.events == []
    assert storage.writes == []


async def test_refine_no_turns_still_publishes(tmp_path):
    s = settings()
    storage = FakeStorage(diarization={"segments": []})
    client = FakeClient()
    llm = FakeLLM()
    publisher = FakePublisher()

    await process_job(completed_event(segments=[]), s, storage, client, llm, publisher)

    assert [c[1] for c in client.status_calls] == ["refining", "refined"]
    assert llm.calls == []
    refined = next(e for e in publisher.events if e.type == "transcription.refined")
    assert refined.payload["segments"] == []
    assert refined.payload["refined"] is True


async def test_refine_falls_back_to_event_segments(tmp_path):
    s = settings()
    storage = FakeStorage(diarization=None)  # read_json raises for diarization
    client = FakeClient()
    llm = FakeLLM()
    publisher = FakePublisher()

    await process_job(completed_event(), s, storage, client, llm, publisher)

    assert [c[1] for c in client.status_calls] == ["refining", "refined"]
    # diarization written from the event-segment fallback; no transcript object
    assert [w[1] for w in storage.writes] == [f"transcripts/{SESSION_ID}/diarization.json"]
    assert len(llm.calls) == 1
    assert len(llm.calls[0][0]) == 2  # views built from the fallback segments


async def test_refine_windowing_respects_fixed_overlap(tmp_path):
    s = settings(refiner_window_turns=2, refiner_window_overlap=1)
    three = [
        seg(0.0, 2.0, "A", 0, "first"),
        seg(3.0, 5.0, "B", 0, "second"),
        seg(6.0, 8.0, "A", 0, "third"),
    ]
    storage = FakeStorage(diarization={"segments": three})
    client = FakeClient()
    llm = FakeLLM(decisions={
        0: ("SPEAKER_00", "first fixed"),
        1: ("SPEAKER_01", "second fixed"),
        2: ("SPEAKER_00", "third fixed"),
    })
    publisher = FakePublisher()

    await process_job(completed_event(segments=three), s, storage, client, llm, publisher)

    # window 1 covers (0,2) with 0 fixed; window 2 covers (1,3) with 1 fixed
    assert [c[2] for c in llm.calls] == [0, 1]
    assert [len(c[0]) for c in llm.calls] == [2, 2]
    # window 2 reused the overlap context in the prompt
    assert llm.calls[1][1]  # context_blocks non-empty

    refined = next(e for e in publisher.events if e.type == "transcription.refined")
    assert [seg["speaker"] for seg in refined.payload["segments"]] == [
        "SPEAKER_00", "SPEAKER_01", "SPEAKER_00",
    ]
    # the fixed turn (index 1) kept the decision from window 1
    assert refined.payload["segments"][1]["text"] == "second fixed"


# ------------------------------------------------- text-only refinement mode
# REFINER_SPEAKERS=false: the LLM fixes the text and leaves every speaker label
# exactly as the diarizer produced it. The attribution engine consumes those
# labels as measurements (docs/attribution-model.md S6.5), so a label the LLM
# guessed would be a measurement thrown away.


async def test_text_only_mode_preserves_the_diarizer_labels(tmp_path):
    s = settings(refiner_speakers=False)
    storage = FakeStorage(transcript={"segments": transcript_segments()})
    llm = FakeLLM()
    publisher = FakePublisher()

    await process_job(completed_event(), s, storage, FakeClient(), llm, publisher)

    refined = next(
        obj for bucket, key, obj in storage.writes if key.endswith("diarization.json")
    )
    # 'Speaker A'/'Speaker B' would have been canonicalized to SPEAKER_00/01 in
    # speaker mode; text-only mode must hand them through untouched.
    assert [seg["speaker"] for seg in refined["segments"]] == ["Speaker A", "Speaker B"]
    assert [seg["text"] for seg in refined["segments"]] == [
        "we must find the relic",
        "agreed let us go",
    ]


async def test_text_only_mode_tells_the_model_nothing_about_speakers(tmp_path):
    s = settings(refiner_speakers=False)
    llm = FakeLLM()

    await process_job(
        completed_event(), s, FakeStorage(), FakeClient(), llm, FakePublisher()
    )

    views, context_blocks, _, _, _, _, _, include_speakers = llm.calls[0]
    assert include_speakers is False
    assert all("speaker" not in view for view in views)
    assert context_blocks == []  # the label rolodex is speaker mode only


async def test_text_only_mode_marks_the_event_and_the_artifact(tmp_path):
    s = settings(refiner_speakers=False)
    storage = FakeStorage(transcript={"segments": transcript_segments()})
    publisher = FakePublisher()

    await process_job(
        completed_event(), s, storage, FakeClient(), FakeLLM(), publisher
    )

    event = next(e for e in publisher.events if e.type == "transcription.refined")
    assert event.payload["speakers_refined"] is False
    assert event.payload["refiner"]["speakers_refined"] is False
    assert event.payload["refiner"]["prompt_version"] == "v4"


async def test_speaker_mode_still_canonicalizes_and_marks_itself(tmp_path):
    s = settings()  # refiner_speakers defaults to true
    storage = FakeStorage(transcript={"segments": transcript_segments()})
    publisher = FakePublisher()

    await process_job(
        completed_event(), s, storage, FakeClient(), FakeLLM(), publisher
    )

    refined = next(
        obj for bucket, key, obj in storage.writes if key.endswith("diarization.json")
    )
    assert [seg["speaker"] for seg in refined["segments"]] == ["SPEAKER_01", "SPEAKER_00"]
    event = next(e for e in publisher.events if e.type == "transcription.refined")
    assert event.payload["speakers_refined"] is True