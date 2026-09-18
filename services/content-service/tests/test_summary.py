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
