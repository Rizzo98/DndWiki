"""The campaign's roster: who plays whom, and who is the game master.

WHY THE SUMMARISER NEEDS IT. The attributed path is handed a roster with every
utterance (who spoke, and which of them are player characters). A DIARIZED
transcript is handed nothing, and the pipeline reads it as if every name in the
room were a candidate for the wiki. Measured on two real sessions, that produced:

    "Giulia si avvicina alle guardie e chiede perche trattengano l'uomo"
    "Tommy si copre per non farsi riconoscere"

Giulia and Tommy are the PEOPLE at the table. Their characters are Dalia and
Letho, and a campaign wiki names characters. Nothing in the recording says which
is which - it is a fact about the campaign, not about the session, and it is
exactly what campaign-service knows: refiner-service already fetches this roster
to correct speaker attribution, and content-service's attributed path already
receives it inside the artifact.

Three things this fixes, all measured:

* a player's name never becomes a character page (the merger drafted pages for
  "Giulia" and "Tommy", both tagged as player characters);
* prose that used a player's name as a stand-in for their character is rewritten
  to the character - the action belongs to Dalia, whatever the table called her
  out of game;
* the extraction is told WHO THE PARTY IS, so it can tell a player character from
  an NPC, which is the tagging every drafted page carries.

It is BEST-EFFORT, like every other campaign lookup in this service: a campaign
that cannot be reached means a session summarised the way it was before this
existed, never a failed job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CampaignRoster:
    """The table: each member's character, and the people who are not characters."""

    #: player/member name -> character name (players only).
    players: dict[str, str] = field(default_factory=dict)
    #: the game master's name(s): at the table, outside the world.
    dm_names: list[str] = field(default_factory=list)
    #: character name -> the DM-curated PHYSICAL description of that character
    #: ('character_description' in campaign-service, the field refiner-service
    #: feeds to its own LLM pass). CARRIED BUT DELIBERATELY NOT PUT IN THE NOTE.
    #:
    #: MEASURED, AND IT HURT. Rendering "Galgith (played by Gianandrea) - fanciulla
    #: bionda" into the '[Table]' note was tried to let the summariser place a name
    #: from a physical detail ("signorina" answered by a woman is the woman in the
    #: list). Five runs against five without it, same code otherwise:
    #:
    #:     contradicted vs the ground truth   2.2  ->  5.6
    #:     name coverage                     95%   ->  85%
    #:     words                             703   ->  631
    #:
    #: The failures were all placement: "Rendar punta un pugnale al fianco di
    #: Letho", "Galgith gli prende un polso", "il mezzorco dottore lo porta nel suo
    #: studio". It is the same lesson as the cast reading that carried voice names
    #: (app/speakers.py): material that lets the model PLACE a name makes it place
    #: names it cannot support, and a wrong name is applied to everything that
    #: follows. The data stays parsed because it is campaign data a later feature
    #: may need; it does not go into the prompt.
    descriptions: dict[str, str] = field(default_factory=dict)

    @property
    def player_names(self) -> list[str]:
        return list(self.players)

    @property
    def character_names(self) -> list[str]:
        return list(self.players.values())

    @property
    def empty(self) -> bool:
        return not self.players and not self.dm_names

    def rename_map(self) -> dict[str, str]:
        """Player name -> character name, for prose that used the wrong one."""
        return dict(self.players)

    def note(self) -> str:
        """The '[Table]' block prepended to every chunk view, or ''.

        It states the two things the extraction cannot get from a diarized
        transcript: which names are PLAYER CHARACTERS, and which names are the
        people at the table rather than anyone in the story.
        """
        if not self.players:
            return ""
        pairs = ", ".join(
            f"{character} (played by {player})"
            for player, character in sorted(self.players.items(), key=lambda kv: kv[1])
        )
        return (
            "[Table] The campaign's roster, from the campaign itself - the "
            "characters this session is about, and the people who play them: "
            f"{pairs}. The names in brackets are the PLAYERS: they are real people "
            "at the table and never appear in the story, so never write one as a "
            "character and never as the actor of a beat - the character acted. "
            "Everyone else who speaks is the game master or an NPC.\n\n"
        )


def roster_from_members(members: list[dict[str, Any]]) -> CampaignRoster:
    """campaign-service members -> the roster the summariser uses.

    A member carries a player name and, for players, the character they play; the
    DM has a role instead of a character. Anything malformed is skipped rather
    than guessed - a roster with a wrong name in it is worse than a short one,
    because a wrong name is applied to everything that person says.
    """
    players: dict[str, str] = {}
    dm_names: list[str] = []
    descriptions: dict[str, str] = {}
    for member in members or []:
        if not isinstance(member, dict):
            continue
        role = str(member.get("role") or "").strip().lower()
        player = str(
            member.get("player_name") or member.get("display_name") or member.get("name") or ""
        ).strip()
        character = str(member.get("character_name") or "").strip()
        described = " ".join(str(member.get("character_description") or "").split())[:200]
        if role == "dm":
            if player:
                dm_names.append(player)
            if character and described:
                descriptions[character] = described
            continue
        if player and character:
            players[player] = character
            if described:
                descriptions[character] = described
    return CampaignRoster(
        players=players, dm_names=sorted(set(dm_names)), descriptions=descriptions
    )


def describe(roster: CampaignRoster) -> str:
    """One log line, without the payload."""
    if roster.empty:
        return "no roster"
    pairs = ", ".join(f"{player}->{character}" for player, character in roster.players.items())
    return f"{len(roster.players)} player(s): {pairs or 'none'}; dm: {', '.join(roster.dm_names) or 'unknown'}"
