"""The campaign roster: who plays whom, and who is not in the story.

The attributed path is handed a roster with every utterance. A diarized transcript
is handed nothing, and the pipeline read it as if every name in the room were a
candidate for the wiki. Measured on a real session, the draft said

    "Giulia si avvicina alle guardie e chiede perche trattengano l'uomo"
    "Tommy si copre per non farsi riconoscere"

and the merger drafted CHARACTER PAGES for Giulia and Tommy, tagged as player
characters. Giulia plays Dalia and Tommy plays Letho: nothing in the recording
says so, and the campaign does.
"""

import pytest
from conftest import (
    SESSION_ID,
    FakeCampaignClient,
    FakeStorage,
    FakeUserClient,
    make_event,
)

from app.merger import merge_extractions, rename_characters
from app.roster import CampaignRoster, describe, roster_from_members
from app.workers.generate import _build_chunk_views

MEMBERS = [
    {"role": "dm", "player_name": "Matt"},
    {"role": "player", "player_name": "Gianandrea", "character_name": "Galgith"},
    {"role": "player", "player_name": "Giulia", "character_name": "Dalia"},
]


# --------------------------------------------------------------------------
# reading the roster
# --------------------------------------------------------------------------


def test_a_roster_pairs_each_player_with_their_character():
    roster = roster_from_members(MEMBERS)
    assert roster.players == {"Gianandrea": "Galgith", "Giulia": "Dalia"}
    assert roster.dm_names == ["Matt"]
    assert roster.character_names == ["Galgith", "Dalia"]
    assert roster.rename_map() == {"Gianandrea": "Galgith", "Giulia": "Dalia"}


@pytest.mark.parametrize(
    "members",
    [
        None,
        [],
        [{"role": "player"}],
        [{"role": "player", "player_name": "Giulia"}],
        [{"role": "player", "character_name": "Dalia"}],
        ["not a dict"],
    ],
)
def test_a_malformed_roster_is_skipped_not_guessed(members):
    """A wrong name is applied to everything that person says, so a half-row is
    dropped rather than completed."""
    assert roster_from_members(members).empty


def test_the_note_states_the_rule_it_exists_for():
    note = CampaignRoster(players={"Giulia": "Dalia"}).note()
    assert "Dalia (played by Giulia)" in note
    assert "never write one as a character" in note
    assert note.endswith("\n\n")
    assert CampaignRoster().note() == ""
    assert describe(CampaignRoster()) == "no roster"


def test_the_physical_description_is_parsed_but_kept_out_of_the_prompt():
    """campaign-service's 'character_description', the field the refiner feeds to
    its own LLM pass. Rendering it in the '[Table]' note was measured and made the
    draft WORSE - contradictions 2.2 -> 5.6, name coverage 95% -> 85% over five runs
    against five - because material that lets a model place a name makes it place
    names it cannot support (see CampaignRoster.descriptions)."""
    roster = roster_from_members(
        [
            {
                "role": "player",
                "player_name": "Gianandrea",
                "character_name": "Galgith",
                "character_description": "  fanciulla   bionda ",
            }
        ]
    )
    assert roster.descriptions == {"Galgith": "fanciulla bionda"}
    note = roster.note()
    assert "fanciulla bionda" not in note
    assert "Galgith (played by Gianandrea)" in note


def test_the_note_claims_nothing_about_who_spoke():
    """It says who exists and who is a person. An extra sentence spelling that out
    was tried and removed: over the runs it was in, the fixture's mean
    contradictions sat at 4.75 against 2.2 for the note it was added to, which is
    inside the session's noise but is not evidence FOR keeping it. Every sentence
    in this note should be one that was measured."""
    note = CampaignRoster(players={"Giulia": "Dalia"}).note()
    assert "spoke" not in note.lower()


# --------------------------------------------------------------------------
# the prose: a player's name stands for their character
# --------------------------------------------------------------------------


def _chunk(summary: str) -> dict:
    return {
        "language": "en",
        "session_summary": summary,
        "characters": [],
        "locations": [],
        "events": [],
        "timeline_entries": [],
    }


def test_prose_that_named_a_player_is_rewritten_to_their_character():
    merged = merge_extractions(
        [_chunk("Giulia si avvicina alle guardie. Tommy resta indietro.")]
    )
    renamed = rename_characters(merged, {"Giulia": "Dalia", "Tommy": "Letho"})
    assert renamed["session_summary"] == (
        "Dalia si avvicina alle guardie. Letho resta indietro."
    )
    assert renamed["summary_beats"][0]["text"].startswith("Dalia")


def test_a_draft_with_no_player_names_is_returned_untouched():
    merged = merge_extractions([_chunk("Dalia si avvicina alle guardie.")])
    assert rename_characters(merged, {"Giulia": "Dalia"}) is merged


def test_a_player_name_inside_a_longer_word_is_not_touched():
    merged = merge_extractions([_chunk("La Dalia di Giuliava non esiste.")])
    renamed = rename_characters(merged, {"Giulia": "Dalia"})
    assert renamed is merged, "whole words only"


# --------------------------------------------------------------------------
# the worker: the roster reaches the view, and never fails a session
# --------------------------------------------------------------------------


async def _views(settings, *, campaign=None, llm=None):
    return await _build_chunk_views(
        make_event(speakers=[]),
        SESSION_ID,
        "22222222-2222-2222-2222-222222222222",
        settings,
        FakeStorage(),
        FakeUserClient(),
        campaign,
        llm,
    )


async def test_the_view_carries_the_roster_and_the_party_line(settings):
    settings.attribution_enabled = False
    views, party, dm_names, _, roster = await _views(
        settings, campaign=FakeCampaignClient(members=MEMBERS)
    )
    assert "[Table]" in views[0]
    assert "Dalia (played by Giulia)" in views[0]
    # the note comes BEFORE the lines it is about
    assert views[0].index("[Table]") < views[0].index("[00:00:00]")
    assert "Party (player characters): Dalia, Galgith" in views[0]
    assert party == ["Galgith", "Dalia"], "the roster is the party when nothing else is"
    assert "Matt" in dm_names, "the DM is out of the world"
    assert not roster.empty


async def test_an_unreachable_campaign_leaves_the_session_alone(settings):
    settings.attribution_enabled = False
    campaign = FakeCampaignClient(members_error=RuntimeError("campaign-service down"))
    views, party, dm_names, _, roster = await _views(settings, campaign=campaign)
    assert "[Table]" not in views[0]
    assert party == [] and dm_names == []
    assert roster.empty


async def test_no_campaign_client_means_no_roster(settings):
    settings.attribution_enabled = False
    views, _, _, _, roster = await _views(settings, campaign=None)
    assert "[Table]" not in views[0]
    assert roster.empty
