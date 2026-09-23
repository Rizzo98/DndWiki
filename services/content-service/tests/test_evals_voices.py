"""What a recording says about its own voices, and whether it agrees with itself.

This is the diagnosis behind the residual defect class - the wrong character on an
action - and it is why every attempt to fix that class inside the summariser
failed. A label has to mean one person before a summariser can be told which
character a voice is; on the benchmark sessions it does not.
"""

from evals import fixtures as store
from evals.voices import VoiceReport, analyse, collect, render


def _transcript(*lines):
    """(label, text) pairs -> the shape transcript.json has."""
    return {
        "segments": [
            {"start": index * 10.0, "speaker": label, "text": text}
            for index, (label, text) in enumerate(lines)
        ]
    }


def test_the_recording_s_own_statements_are_collected():
    found = collect(
        _transcript(
            ("SPEAKER_02", "Piuttosto... io sono Rendar."),
            ("SPEAKER_05", "Io sono Letho."),
            ("SPEAKER_01", "Shiran, piacere."),
            ("SPEAKER_05", "Shiran: non appena sento la parola indaco mi irrigidisco"),
        )
    )
    assert [(item.label, item.name, item.kind) for item in found] == [
        ("SPEAKER_02", "Rendar", "self_intro"),
        ("SPEAKER_05", "Letho", "self_intro"),
        ("SPEAKER_01", "Shiran", "pleasure"),
        ("SPEAKER_05", "Shiran", "self_label"),
    ]


def test_a_match_is_not_a_name():
    """A blanket re.IGNORECASE turns "Io sono l'unico" into a character called
    l'unico; scoping the flag to the keyword keeps it out."""
    assert collect(_transcript(("SPEAKER_06", "Io sono l'unico che si fa i cazzi suoi."))) == []
    assert collect(_transcript(("SPEAKER_05", "Domanda: siamo tutti allo stesso tavolo?"))) == []


def _report(*lines) -> VoiceReport:
    report = VoiceReport(fixture="synthetic")
    for item in collect(_transcript(*lines)):
        report.evidence.append(item)
        report.by_label.setdefault(item.label, {}).setdefault(item.name, []).append(item)
        report.by_name.setdefault(item.name, set()).add(item.label)
    return report


def test_one_label_speaking_as_two_people_is_a_conflict():
    report = _report(
        ("SPEAKER_00", "io sono Rendar."),
        ("SPEAKER_00", "Il mio nome è Miles Falco, sono il vice sceriffo."),
    )
    assert not report.usable
    assert report.conflicting_labels == {"SPEAKER_00": {"Rendar", "Miles"}}


def test_one_person_speaking_under_two_labels_is_a_conflict():
    report = _report(
        ("SPEAKER_04", "Shiran, cin!"),
        ("SPEAKER_05", "Shiran: non appena sento la parola indaco..."),
    )
    assert not report.usable
    assert report.split_names == {"Shiran": {"SPEAKER_04", "SPEAKER_05"}}


def test_consistent_labels_are_usable():
    report = _report(
        ("SPEAKER_00", "Io sono Letho."),
        ("SPEAKER_01", "Il mio nome è Miles Falco."),
    )
    assert report.usable
    assert "can be told which label is which" in render(report)


def test_a_recording_that_never_names_a_voice_says_so():
    report = _report(("SPEAKER_00", "The doors are sealed."))
    assert report.usable, "nothing claimed is not a conflict"
    assert "never states who any voice is" in render(report)


def test_the_report_names_the_pass_that_fixes_it():
    report = _report(("SPEAKER_00", "io sono Rendar."), ("SPEAKER_00", "Mi chiamo Miles."))
    text = render(report)
    assert "do NOT agree" in text
    assert "refiner-service's speaker mode" in text


def test_the_installed_fixtures_are_reported_on():
    """Skips itself when the fixtures are removed, like every other eval test."""
    for name in ("s1e1_elfi_e_indaco", "s1e2_bugie_inutili"):
        if not (store.FIXTURE_ROOT / name / store.META_NAME).is_file():
            continue  # the fixture is optional: deleting it is the removal path
        report = analyse(name)
        assert report.evidence, f"{name}: the recording states who somebody is"
