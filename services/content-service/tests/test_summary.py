"""The narrative summary: scene blocks, and the text derived from them.

The summary is prose in blocks ([{location, text}], app/summary.py) and the DM
reviews the TEXT of it (they highlight portions). These tests pin the two
things that keep that safe: whatever the model returns becomes a usable block
list, and the text always agrees with the blocks.
"""

from __future__ import annotations

from app.summary import (
    MAX_SUMMARY_BLOCKS,
    blocks_to_text,
    blocks_with_places,
    describe_blocks,
    normalize_blocks,
    summary_from,
    text_to_blocks,
)


def test_blocks_pass_through():
    blocks = [
        {"location": "Locanda del Fumo Aspro", "text": "La sessione si apre."},
        {"location": "", "text": "Poi il gruppo esce."},
    ]
    assert normalize_blocks(blocks) == blocks


def test_a_missing_location_is_empty_not_absent():
    assert normalize_blocks([{"text": "Una scena."}]) == [
        {"location": "", "text": "Una scena."}
    ]


def test_a_plain_string_becomes_paragraph_blocks():
    assert normalize_blocks("Primo paragrafo.\n\nSecondo paragrafo.") == [
        {"location": "", "text": "Primo paragrafo."},
        {"location": "", "text": "Secondo paragrafo."},
    ]


def test_a_one_line_per_beat_text_still_reads_as_a_story():
    """The old format (one beat per line) has to render, not collapse into one
    enormous paragraph."""
    assert normalize_blocks("Un beat.\nUn altro beat.") == [
        {"location": "", "text": "Un beat."},
        {"location": "", "text": "Un altro beat."},
    ]


def test_a_list_of_strings_is_accepted():
    assert normalize_blocks(["Una scena.", "  Un'altra  "]) == [
        {"location": "", "text": "Una scena."},
        {"location": "", "text": "Un'altra"},
    ]


def test_a_single_block_object_is_accepted():
    assert normalize_blocks({"location": "Moria", "text": "Buio."}) == [
        {"location": "Moria", "text": "Buio."}
    ]


def test_empty_and_unusable_answers_give_nothing():
    assert normalize_blocks(None) == []
    assert normalize_blocks("") == []
    assert normalize_blocks([]) == []
    assert normalize_blocks([{"location": "Moria", "text": "   "}]) == []
    assert normalize_blocks([12, None, {"text": ""}]) == []
    assert normalize_blocks(42) == []


def test_whitespace_is_collapsed_and_bullets_dropped():
    assert normalize_blocks([{"text": "- uno\n\n  due  "}]) == [
        {"location": "", "text": "uno due"}
    ]
    assert normalize_blocks([{"location": "  Moria  ", "text": "* x"}]) == [
        {"location": "Moria", "text": "x"}
    ]


def test_the_block_list_is_capped():
    blocks = normalize_blocks([{"text": f"scena {i}"} for i in range(MAX_SUMMARY_BLOCKS + 10)])
    assert len(blocks) == MAX_SUMMARY_BLOCKS


def test_text_is_the_blocks_joined_by_blank_lines():
    blocks = [
        {"location": "Moria", "text": "Il gruppo entra."},
        {"location": "", "text": "Poi scende."},
    ]
    assert blocks_to_text(blocks) == "Il gruppo entra.\n\nPoi scende."
    assert blocks_to_text([]) == ""


def test_summary_from_returns_both_forms_of_one_answer():
    blocks, text = summary_from([{"location": "Moria", "text": "Il gruppo entra."}])
    assert blocks == [{"location": "Moria", "text": "Il gruppo entra."}]
    assert text == "Il gruppo entra."


def test_text_to_blocks_of_a_legacy_summary_has_no_labels():
    assert text_to_blocks("Un beat.\nUn altro.") == [
        {"location": "", "text": "Un beat."},
        {"location": "", "text": "Un altro."},
    ]


# --- carrying the place labels over a text the client sent back ---------------
#
# The DM's client sends the narrative as the plain text the page displays, so
# the labels have to be re-attached to the blocks rebuilt from it: a correction
# used to arrive with every place blanked out, because the revision call can
# only copy the labels it is given (and it was given none).


def _labeled() -> list[dict[str, str]]:
    return [
        {"location": "Locanda del Fumo Aspro", "text": "Il gruppo si ritrova."},
        {"location": "Strada fuori dalla locanda", "text": "Fuori si sentono urla."},
        {"location": "Ospedale di Fatumastra", "text": "All'ospedale lo visitano."},
    ]


def test_unchanged_text_keeps_every_label():
    stored = _labeled()
    assert blocks_with_places(blocks_to_text(stored), stored) == stored


def test_a_reworded_paragraph_keeps_the_place_it_stands_in_for():
    stored = _labeled()
    edited = blocks_to_text(stored).replace(
        "Fuori si sentono urla.", "Fuori si sentono delle urla."
    )
    assert blocks_with_places(edited, stored) == [
        stored[0],
        {"location": "Strada fuori dalla locanda", "text": "Fuori si sentono delle urla."},
        stored[2],
    ]


def test_a_paragraph_the_dm_added_carries_no_place_of_its_own():
    stored = _labeled()
    added = blocks_to_text(stored[:2]) + "\n\nUn dettaglio in più." + "\n\n" + stored[2]["text"]
    assert blocks_with_places(added, stored) == [
        stored[0],
        stored[1],
        # the format reads an empty label as "continues the previous scene"
        {"location": "", "text": "Un dettaglio in più."},
        {**stored[2], "text": stored[2]["text"]},
    ]


def test_a_deleted_paragraph_does_not_shift_the_labels_below_it():
    stored = _labeled()
    dropped = "\n\n".join([stored[0]["text"], stored[2]["text"]])
    assert blocks_with_places(dropped, stored) == [stored[0], stored[2]]


def test_text_with_no_blocks_to_carry_from_has_no_labels():
    assert blocks_with_places("Una storia nuova.", None) == [
        {"location": "", "text": "Una storia nuova."}
    ]
    assert blocks_with_places("Una storia nuova.", []) == [
        {"location": "", "text": "Una storia nuova."}
    ]
    # a legacy row holds its narrative as text: there is no label to carry
    assert blocks_with_places("Un beat.\nUn altro.", "Un beat.\nUn altro.") == [
        {"location": "", "text": "Un beat."},
        {"location": "", "text": "Un altro."},
    ]


def test_blank_text_yields_no_blocks_at_all():
    assert blocks_with_places("   ", _labeled()) == []


def test_describe_blocks_names_the_places():
    described = describe_blocks(
        [
            {"location": "Locanda", "text": "a"},
            {"location": "", "text": "b"},
        ]
    )
    assert described == "2 block(s) in Locanda, ?"
    assert describe_blocks([]) == "no summary"
    assert describe_blocks("una storia") == "1 block(s) in ?"
