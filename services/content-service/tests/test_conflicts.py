"""The conflict flag: beats that look like one moment the session read twice.

The detector is deliberately modest - it is precise and blind, and it reports
rather than fixes - so these tests are about the two properties that keep it
honest: it never compares beats written by the SAME reading, and it never says
the same sentence twice.
"""

from __future__ import annotations

from app.conflicts import (
    MAX_CONFLICTS,
    Conflict,
    conflicts_payload,
    content_words,
    find_conflicts,
    similarity,
)


def beat(text: str, span: tuple[str, str]) -> dict[str, str]:
    return {"text": text, "from": span[0], "to": span[1]}


#: The pair that started all of this, as two chunks really produced it: the girl
#: carrying lunch, described from both sides of the chunk 3/4 seam.
GIRL_FROM_PART_3 = beat(
    "Fuori dall'ospedale il gruppo nota una ragazza che porta una cesta di vini",
    ("u_00300", "u_00435"),
)
GIRL_FROM_PART_4 = beat(
    "Una nana porta il pranzo a suo zio, il primario dell'ospedale",
    ("u_00436", "u_00595"),
)

#: The duplication the detector DOES find, from two real runs: the group entering
#: the patient's room, written by the part before the seam and the part after it.
ROOM_FROM_PART_4 = beat(
    "Il gruppo entra nella stanza e trova il soggetto seduto sul letto in stato "
    "confusionario, mentre Shiran Konno prende nota",
    ("u_00436", "u_00595"),
)
ROOM_FROM_PART_5 = beat(
    "Entrati nella stanza, il gruppo trova l'uomo seduto sul letto in stato "
    "confusionario mentre Shiran prende nota su un taccuino",
    ("u_00596", "u_00649"),
)


# --- the ruler ---------------------------------------------------------------


def test_content_words_drop_stopwords():
    assert content_words("Il gruppo entra nella stanza con il gruppo") == {
        "entra",
        "stanza",
    }


def test_similarity_is_jaccard_over_content_words():
    assert similarity("il gruppo entra", "il gruppo esce") == 0.0
    assert similarity("", "anything") == 0.0
    assert similarity("porta il pranzo a suo zio", "porta il pranzo a suo zio") == 1.0


# --- what it finds -----------------------------------------------------------


def test_two_readings_of_one_moment_are_reported():
    (found,) = find_conflicts([ROOM_FROM_PART_4, ROOM_FROM_PART_5])
    assert found.first_text == ROOM_FROM_PART_4["text"]
    assert found.first_from == "u_00436"
    assert found.second_from == "u_00596"
    assert found.score >= 0.35


def test_beats_from_the_same_reading_are_never_compared():
    """Two beats carrying one span were written together by one reading: they are
    the normal shape of a summary, and the defect is always a boundary."""
    same_span = beat(ROOM_FROM_PART_4["text"], ("u_00436", "u_00595"))
    twin = beat(ROOM_FROM_PART_5["text"], ("u_00436", "u_00595"))
    assert find_conflicts([same_span, twin]) == []


def test_different_moments_about_one_person_are_not_reported():
    """The detector's own failure mode when it was a question generator: "same
    person" is not "same moment", and a summary is full of the first."""
    beats = [
        beat("Hann Caleto si presenta al gruppo senza ricordare nulla", ("u_00165", "u_00299")),
        beat("Nello studio Hann Caleto si guarda allo specchio", ("u_00300", "u_00435")),
    ]
    assert find_conflicts(beats) == []


def test_a_beat_is_quoted_once():
    """Two flags quoting one sentence read as one flag repeated: the real
    duplication produces exactly that, two pairs sharing the second beat."""
    beats = [
        ROOM_FROM_PART_4,
        beat(
            "Il vice sceriffo esce e fa entrare tutti: il soggetto è seduto sul "
            "letto in stato confusionario, con Shiran Konno davanti a lui",
            ("u_00436", "u_00595"),
        ),
        ROOM_FROM_PART_5,
    ]
    found = find_conflicts(beats)
    assert len(found) == 1
    quoted = [conflict.first_text for conflict in found] + [
        conflict.second_text for conflict in found
    ]
    assert len(set(quoted)) == 2


def test_the_list_is_capped():
    beats = [
        beat(f"il gruppo entra nella stanza numero {index} e prende nota", ("u_00001", "u_00010"))
        for index in range(MAX_CONFLICTS + 6)
    ]
    beats += [
        beat(
            f"entrati nella stanza numero {index} il gruppo prende nota",
            ("u_00011", "u_00020"),
        )
        for index in range(MAX_CONFLICTS + 6)
    ]
    assert len(find_conflicts(beats)) <= MAX_CONFLICTS


def test_nothing_to_report_is_the_normal_answer():
    assert find_conflicts([]) == []
    assert find_conflicts([beat("una sola frase", ("u_00001", "u_00010"))]) == []


def test_the_payload_is_what_the_page_renders():
    (found,) = find_conflicts([ROOM_FROM_PART_4, ROOM_FROM_PART_5])
    payload = conflicts_payload([found])
    assert payload == [
        {
            "score": round(found.score, 2),
            "first": {
                "text": ROOM_FROM_PART_4["text"],
                "from": "u_00436",
                "to": "u_00595",
            },
            "second": {
                "text": ROOM_FROM_PART_5["text"],
                "from": "u_00596",
                "to": "u_00649",
            },
        }
    ]


def test_conflicts_are_json_serializable():
    """They are stored in a JSONB column: a dataclass left in the payload would
    fail at commit time, on a real session, in production."""
    import json

    (found,) = find_conflicts([ROOM_FROM_PART_4, ROOM_FROM_PART_5])
    assert json.loads(json.dumps(conflicts_payload([found])))


def test_the_girl_is_exactly_the_case_this_detector_misses():
    """Not a bug to fix in this module - the documented limit. The two beats
    share too few content words, which is why the pipeline FLAGS rather than
    asks: a flag nobody must act on is allowed to be incomplete."""
    assert find_conflicts([GIRL_FROM_PART_3, GIRL_FROM_PART_4]) == []
    assert similarity(GIRL_FROM_PART_3["text"], GIRL_FROM_PART_4["text"]) < 0.35


def test_a_conflict_carries_both_halves_of_the_claim():
    (found,) = find_conflicts([ROOM_FROM_PART_4, ROOM_FROM_PART_5])
    assert isinstance(found, Conflict)
    assert found.first_to == "u_00595"
    assert found.second_to == "u_00649"
