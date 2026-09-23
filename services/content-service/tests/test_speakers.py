"""The session-wide cast reading (app/speakers.py).

A diarized transcript carries voices, not people: the view says SPEAKER_00 and
every chunk decides for itself who that is. Measured on two real sessions, that
is where the surviving summary defects come from - one NPC under three names, the
deputy sheriff called "Shiran", a player's real name in the narrative - because
ONE wrong decision is applied to every beat the voice speaks in.

These tests cover the reading, its two failure modes (a malformed answer and a
call that fails), and the wiring: a named voice reaches the extraction inside the
'[Cast]' note, a narrator becomes an out-of-world speaker, and a session with no
reading summarises exactly as it did before the module existed.
"""

import pytest
from conftest import SESSION_ID, FakeLLM, FakeStorage, FakeUserClient, make_event

from app.core.config import ServiceSettings
from app.speakers import (
    CAST_SYSTEM_PROMPT,
    SAMPLE_MAX_CHARS,
    SpeakerReading,
    describe,
    parse_reading,
    sample_lines,
    self_labelled_names,
)
from app.workers.generate import _build_chunk_views

# --------------------------------------------------------------------------
# parsing: what the model answered -> what the pipeline may rely on
# --------------------------------------------------------------------------


def test_a_reading_names_voices_and_finds_the_narrator():
    reading = parse_reading(
        {
            "voices": [
                {
                    "label": "SPEAKER_00",
                    "character": None,
                    "narrator": True,
                    "evidence": "describes the world",
                },
                {
                    "label": "SPEAKER_03",
                    "character": "Galgith",
                    "narrator": False,
                    "evidence": "mi calo il brandy ed esco",
                },
            ],
            "party": ["Galgith"],
        }
    )
    assert reading.names == {"SPEAKER_03": "Galgith"}
    assert reading.narrators == ["SPEAKER_00"]
    assert reading.party == ["Galgith"]
    assert not reading.empty


@pytest.mark.parametrize("answer", ["null", "None", "unknown", "sconosciuto", "?"])
def test_silence_is_not_a_name(answer):
    """The prompt says a voice it cannot establish stays null; a model that writes
    the word instead of the JSON null must not create a character called 'null'."""
    reading = parse_reading({"voices": [{"label": "SPEAKER_01", "character": answer}]})
    assert reading.names == {}
    assert reading.empty


def test_a_voice_cannot_be_both_a_narrator_and_a_character():
    """The safer half wins: a world description must not become a character's line."""
    reading = parse_reading(
        {
            "voices": [
                {"label": "SPEAKER_00", "character": "Antonikus", "narrator": True}
            ]
        }
    )
    assert reading.narrators == ["SPEAKER_00"]
    assert reading.names == {}


@pytest.mark.parametrize(
    "payload",
    [None, [], "no", {}, {"voices": None}, {"voices": ["SPEAKER_00"]}, {"voices": [{}]}],
)
def test_a_malformed_answer_degrades_to_nothing_established(payload):
    assert parse_reading(payload).empty


# --------------------------------------------------------------------------
# the note that reaches the extraction
# --------------------------------------------------------------------------


def test_the_note_carries_the_narrators_and_no_character_names():
    """The measured rule: a narrator line is world narration, and a character name
    is NOT handed to the extraction - a wrong name is applied to every beat that
    voice speaks in (see SpeakerReading.note for the numbers)."""
    reading = SpeakerReading(
        names={"SPEAKER_03": "Galgith"},
        narrators=["SPEAKER_00"],
        evidence={"SPEAKER_03": "mi calo il brandy ed esco"},
    )
    note = reading.note()
    assert "SPEAKER_00" in note and "NARRATE" in note
    assert "Galgith" not in note, "a name would be applied to the whole session"
    assert "mi calo il brandy ed esco" not in note
    assert note.endswith("\n\n")


def test_an_empty_reading_renders_no_note_at_all():
    """Which is exactly what the pipeline did before this module existed."""
    assert SpeakerReading().note() == ""
    assert SpeakerReading(names={"SPEAKER_00": "Aragorn"}).note() == "", (
        "character names alone are not a note any more"
    )
    assert describe(SpeakerReading()) == "no voice established"


def test_the_prompt_asks_for_the_evidence_the_note_claims():
    """The four kinds of evidence are in the prompt, and the parser agrees on the
    one it can check mechanically (a line announcing its own character name)."""
    for phrase in ("announces its own character name", "first-person", "narrate the world"):
        assert phrase.lower() in CAST_SYSTEM_PROMPT.lower(), phrase
    assert self_labelled_names(["[00:18:40] SPEAKER_05: Shiran: non appena sento la parola"]) == {
        "[00:18:40] SPEAKER_05: Shiran: non appena sento la parola": "Shiran"
    }


# --------------------------------------------------------------------------
# the sample: the evidence is at the head, the rest is spread
# --------------------------------------------------------------------------


def test_a_session_that_fits_is_shown_whole():
    lines = ["[00:00:01] SPEAKER_00: hello"] * 10
    assert sample_lines(lines) == lines


def test_a_long_session_keeps_its_head_and_spreads_the_rest():
    lines = [f"[{i:02d}:00:00] SPEAKER_0{i % 7}: line {i}" for i in range(4000)]
    sample = sample_lines(lines)
    assert len(sample) < len(lines)
    assert sample[:120] == lines[:120], "the head is where a table introduces itself"
    assert sum(len(line) + 1 for line in sample) <= SAMPLE_MAX_CHARS + 200
    # the tail must be reachable: a voice introduced late is still seen
    assert any(line in sample for line in lines[-40:])


# --------------------------------------------------------------------------
# wiring: the reading reaches the views, and never breaks a session
# --------------------------------------------------------------------------


async def _views(settings, llm, *, event=None, transcript=None, campaign=None):
    return await _build_chunk_views(
        event or make_event(speakers=[]),
        SESSION_ID,
        "22222222-2222-2222-2222-222222222222",
        settings,
        FakeStorage(transcript),
        FakeUserClient(),
        campaign,
        llm,
    )


async def test_a_diarized_session_is_summarised_with_the_reading_in_front(settings):
    settings.attribution_enabled = False
    llm = FakeLLM()
    llm.reading = SpeakerReading(
        names={"SPEAKER_00": "Aragorn"}, narrators=["SPEAKER_01"], party=["Aragorn"]
    )
    views, party, dm_names, _, _ = await _views(settings, llm)

    assert "[Cast]" in views[0]
    assert "SPEAKER_01" in views[0] and "NARRATE" in views[0]
    # the note comes BEFORE the lines it is about, like the '[Stretches]' one
    assert views[0].index("[Cast]") < views[0].index("[00:00:00] SPEAKER_00")
    assert "Party (player characters)" not in views[0], (
        "a reading does not decide who is a player character"
    )
    assert party == []
    assert dm_names == ["SPEAKER_01"], "a narrator is out of the world"
    assert llm.cast_lines, "the reading is made from the session's own lines"


async def test_the_narrator_reaches_the_extraction_as_out_of_world(session_factory, settings):
    """The half that a diarized view could never say: a line from the game master
    is world narration, not the speech of an unknown person. The names come back
    from _build_chunk_views as dm_names, and process_job hands them to the
    extraction as 'out_of_world', which the prompt forbids making characters of."""
    from test_worker import _run_phase1

    settings.attribution_enabled = False
    llm = FakeLLM()
    llm.reading = SpeakerReading(
        names={"SPEAKER_00": "Aragorn"}, narrators=["SPEAKER_01"]
    )
    await _run_phase1(
        session_factory,
        settings,
        event=make_event(speakers=[]),
        llm=llm,
        session_client=None,
    )
    assert llm.out_of_world == ["SPEAKER_01"]


async def test_a_session_with_no_reading_is_unchanged(settings):
    """The reading is a bonus, not a precondition: this is the view the pipeline
    produced before app/speakers.py existed, byte for byte."""
    settings.attribution_enabled = False
    llm = FakeLLM()
    llm.reading = SpeakerReading()
    views, party, dm_names, _, _ = await _views(settings, llm)
    assert "[Cast]" not in views[0]
    assert party == [] and dm_names == []


async def test_a_failed_reading_never_fails_the_session(settings):
    settings.attribution_enabled = False
    llm = FakeLLM()
    llm.reading_error = RuntimeError("provider down")
    views, party, dm_names, _, _ = await _views(settings, llm)
    assert "[Cast]" not in views[0]
    assert party == [] and dm_names == []


async def test_the_reading_can_be_turned_off(settings):
    settings.attribution_enabled = False
    settings.cast_reading_enabled = False
    llm = FakeLLM()
    llm.reading = SpeakerReading(names={"SPEAKER_00": "Aragorn"})
    views, _, _, _, _ = await _views(settings, llm)
    assert "[Cast]" not in views[0]
    assert llm.cast_lines == [], "no call is made when the flag is off"


async def test_a_named_speaker_map_means_no_reading_is_needed(settings):
    """When speaker identification HAS run, the labels are already names and the
    reading would be a second opinion about something already known."""
    settings.attribution_enabled = False
    llm = FakeLLM()
    llm.reading = SpeakerReading(names={"SPEAKER_00": "Wrong"})
    views, _, _, _, _ = await _views(settings, llm, event=make_event())
    assert "[Cast]" not in views[0]
    assert llm.cast_lines == []


def test_the_reading_is_on_by_default():
    assert ServiceSettings(_env_file=None).cast_reading_enabled is True
