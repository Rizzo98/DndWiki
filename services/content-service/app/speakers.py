"""Who is who, read ONCE from the recording (the diarized path's roster).

THE GAP THIS FILLS. The attributed path is handed a roster: every utterance says
who spoke, and the extraction knows which names are player characters and which
speaker is the narrator. A DIARIZED transcript is handed nothing - the view shows
`SPEAKER_00`..`SPEAKER_06` - so every chunk decides for itself who is who, on a
slice of the session, with no memory of what the previous chunk decided.

Measured on two real sessions, that is the defect class that survives everything
else. One wrong decision is applied to EVERY beat the voice speaks in, and the
decisions disagree with each other:

    "Shiran afferra l'uomo per la collottola"      (it was Galgith's line)
    "il vicesceriffo Shiran sta interrogando..."   (the deputy is Miles Falco)
    "Giulia si avvicina alle guardie"              (a PLAYER's name, not a character)

None of those is a summary problem. They are all one question - *who is speaking* -
answered seven times per session, independently, by a model that cannot see the
rest of the session.

WHAT THIS IS, AND IS NOT. It is a READING, made from the recording's own words:
an introduction, a line the table addresses by name, a line where a player
announces their character ("Shiran: non appena sento la parola indaco..."), or a
first-person line the narration immediately describes in the second person
("mi calo il brandy ed esco" followed by "vedete uscire la graziosa fanciulla
bionda"). It is NOT speaker identification: no voiceprint is involved, a voice the
recording does not establish stays unnamed, and the note says so.

WHAT IS USED, AND WHAT WAS MEASURED AND DROPPED. The reading produces both a
narrator list and character names per voice. ONLY THE NARRATOR LIST IS HANDED TO
THE EXTRACTION.

The first version handed over the names too, as "[Cast] SPEAKER_01 = Shiran - use
this name for what that voice says". On the two benchmark sessions it was a large
regression, in the same direction both times:

    contradictions vs the ground truth   1-4 per run  ->  16
    name coverage (S1E2)                 88-100%      ->  75%
    content coverage (S1E1)              100%         ->  73%

The failure is structural, not a bad answer to one prompt. A NAME is applied to
every beat that voice speaks in, so ONE wrong assignment rewrites the session (the
draft had Shiran threatening Letho, Shiran interrogating the patient, Hann meeting
Hann); a narrator assignment that is wrong costs only the lines it covers. And the
recording is not clean enough for the first: its diarization splits one voice
across two labels inside a single scene, so "which label said this" is not
reliable, while "does this label describe the world" survives that noise.

What the narrator half buys is the guarantee the diarized view cannot make for
itself: on a diarized view the rule "the DM narrates the world and is not a
character" can never fire, because the line says SPEAKER_00 and not "DM". With the
reading, those lines are read as FACTS about who did what instead of as the speech
of an unknown person - which is most of what a session summary is made of.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: How much of the session the reading is shown. The evidence is concentrated
#: where the table introduces itself (the head) and spread through the rest, so the
#: sample is the head plus evenly spaced lines up to the budget.
SAMPLE_HEAD_LINES = 120
SAMPLE_MAX_CHARS = 24000

#: A line that opens with its own character name - "Shiran: non appena sento la
#: parola indaco..." - is the strongest evidence there is: the player said who
#: they were. Detected here so a test can prove it is what the prompt is told.
SELF_LABEL = re.compile(r"^\s*([A-ZÀ-Ý][\w'à-ÿ]{2,})\s*:")

CAST_SYSTEM_PROMPT = """You read a tabletop RPG session transcript to find out WHO IS WHO.

You are given the session's lines, diarized: each line is
[HH:MM:SS] SPEAKER_NN: text. The labels are VOICES, not people, and the
diarization makes mistakes: one person can speak under two labels inside a single
scene, and one label can cover two people. LABEL CONTINUITY IS NOT EVIDENCE.

What IS evidence, strongest first:

1. a line that announces its own character name - "Shiran: non appena sento la
   parola indaco mi irrigidisco";
2. a self-introduction adopted by the table - "Dalia, piacere." / "Shiran, cin!";
3. the table addressing someone by name and the answering line adopting it;
4. a FIRST-PERSON line the narration immediately describes in the second person -
   "mi calo il brandy ed esco" followed by "vedete uscire la graziosa fanciulla
   bionda" means that voice is the woman the narration just described;
5. the narration naming who acts ("Galgith estrae un pugnale").

You are looking for the CHARACTER (the fictional person), never the player. A
player's real name is not a character.

A voice you cannot establish stays null. This matters more than coverage: a wrong
name is applied to everything that voice says for the whole session, while a
missing name costs one vague sentence.

Report the voices that NARRATE the world - the game master describing places,
events and NPCs, and anyone co-narrating with them - separately. They are not
characters, and their lines are facts about the world.

Respond with a single JSON object:

{"voices": [{"label": "SPEAKER_03", "character": "Galgith", "narrator": false,
             "evidence": "the words that decided it, quoted briefly"}],
 "party": ["character names that are player characters"]}
"""


@dataclass
class SpeakerReading:
    """One session's voices, as far as its own words establish them."""

    #: label -> character name, for the voices the recording establishes.
    names: dict[str, str] = field(default_factory=dict)
    #: labels that narrate the world (the game master and any co-narrator).
    narrators: list[str] = field(default_factory=list)
    #: the player characters, when the reading could tell them apart.
    party: list[str] = field(default_factory=list)
    #: label -> the words that decided it, for the log and the note.
    evidence: dict[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.names and not self.narrators

    def note(self) -> str:
        """The '[Cast]' block prepended to every chunk view, or ''.

        NARRATORS ONLY, AND THAT IS A MEASUREMENT, NOT A PREFERENCE. The first
        version of this note also carried the character names the reading had
        assigned to voices - "SPEAKER_01 = Shiran" - and told the extraction to use
        them. On the two benchmark sessions that was a large, unambiguous
        regression: contradictions against the ground truth went from 1-4 per run
        to 16, name coverage fell to 75%, and on the other fixture content coverage
        fell from 100% to 73%.

        The reason is structural rather than a bad answer. A NAME is applied to
        every beat that voice speaks in, so one wrong assignment rewrites the whole
        session (the draft had Shiran threatening Letho, Shiran interrogating the
        patient, Hann finding Hann); a NARRATOR assignment that is wrong costs the
        lines it covers and nothing else. The recording's diarization is not clean
        enough for the first - it splits one voice across two labels inside a single
        scene - so names stay where they can be checked line by line: the rules for
        labels.

        What the narrator half buys is a guarantee the diarized view cannot make
        for itself: that the game master's lines are the world, not the speech of an
        unknown person.
        """
        if not self.narrators:
            return ""
        return (
            "[Cast] These voices of the recording NARRATE the world (a reading of "
            "the recording's own words, not an identification): "
            + ", ".join(sorted(self.narrators))
            + ". They are the game master describing places, events and NPCs, and "
            "are NOT characters. What their lines say about who did what is a FACT "
            "of the session, and the best material you have. Every other voice is a "
            "person at the table the recording does not name: for those, the rules "
            "for labels apply unchanged - name an actor only where the lines "
            "themselves name them.\n\n"
        )


def sample_lines(lines: list[str], *, max_chars: int = SAMPLE_MAX_CHARS) -> list[str]:
    """The lines the reading is shown: the head, then evenly spaced lines.

    The head is where a table introduces itself, and the rest of the session is
    where a voice is addressed by name; a budget-spread sample reaches both. A
    session that fits in the budget is shown whole.
    """
    if not lines:
        return []
    if sum(len(line) + 1 for line in lines) <= max_chars:
        return list(lines)
    head = lines[:SAMPLE_HEAD_LINES]
    used = sum(len(line) + 1 for line in head)
    rest = lines[SAMPLE_HEAD_LINES:]
    if not rest or used >= max_chars:
        return head
    # How many more lines fit is a guess about line length, so the fit is CHECKED
    # and the sample coarsened until it holds: a sample that overruns the budget is
    # a provider error, not a smaller sample.
    average = max(1, sum(len(line) + 1 for line in rest) // len(rest))
    take = max(2, (max_chars - used) // average)
    while take >= 2:
        sample = head + _spread(rest, take)
        if sum(len(line) + 1 for line in sample) <= max_chars:
            return sample
        take //= 2
    return head


def _spread(lines: list[str], take: int) -> list[str]:
    """'take' lines from 'lines', evenly spaced, FIRST AND LAST always included.

    The last line matters: a voice can be introduced in the final minutes, and a
    sample built by stepping from the start misses it without saying so.
    """
    if take >= len(lines):
        return list(lines)
    if take < 2:
        return [lines[0]]
    last = len(lines) - 1
    return [lines[round(index * last / (take - 1))] for index in range(take)]


def parse_reading(payload: Any) -> SpeakerReading:
    """Whatever the model returned -> a SpeakerReading (never raises).

    A malformed answer degrades to "nothing was established", which is exactly
    what the pipeline did before this module existed: the note is simply absent
    and the extraction falls back to the label rules.
    """
    if not isinstance(payload, dict):
        return SpeakerReading()
    reading = SpeakerReading()
    for entry in payload.get("voices") or []:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip()
        if not label:
            continue
        if entry.get("narrator"):
            reading.narrators.append(label)
        character = str(entry.get("character") or "").strip()
        # A model that answers "null"/"unknown"/"sconosciuto" means silence.
        if character and character.lower() not in {"null", "none", "unknown", "sconosciuto", "?"}:
            reading.names[label] = character
            evidence = str(entry.get("evidence") or "").strip()
            if evidence:
                reading.evidence[label] = evidence[:200]
    reading.party = [
        str(name).strip() for name in (payload.get("party") or []) if str(name).strip()
    ]
    # A narrator that was ALSO given a character name is a contradiction the note
    # cannot render; the narrator reading wins, because it is the safer one (it
    # keeps a world description from becoming a character's line).
    for label in reading.narrators:
        reading.names.pop(label, None)
        reading.evidence.pop(label, None)
    return reading


#: A view line: "[00:18:40] SPEAKER_05: text". What follows the label is the text,
#: and a self-announcement is a name at the START of that text.
_VIEW_LINE = re.compile(r"^\[[^\]]*\]\s+(?P<label>[^:]+):\s?(?P<text>.*)$")


def self_labelled_names(lines: list[str]) -> dict[str, str]:
    """Character names announced by their own line ("... SPEAKER_05: Shiran: ...").

    Not used to build the reading - the model does that - but kept here because it
    is the evidence the prompt's first rule is about, and a test asserts the prompt
    and this detector agree on what it looks like. The label at the start of the
    line is NOT a self-announcement: it is the voice the transcript detected.
    """
    found: dict[str, str] = {}
    for line in lines:
        view = _VIEW_LINE.match(line)
        text = view.group("text") if view else line
        match = SELF_LABEL.match(text)
        if match:
            found[line] = match.group(1)
    return found


def describe(reading: SpeakerReading) -> str:
    """One log line: what the reading established, without the transcript."""
    if reading.empty:
        return "no voice established"
    named = ", ".join(f"{label}={name}" for label, name in sorted(reading.names.items()))
    narrators = ", ".join(sorted(reading.narrators)) or "none"
    return f"named: {named or 'none'} | narrators: {narrators}"


__all__ = [
    "CAST_SYSTEM_PROMPT",
    "SAMPLE_HEAD_LINES",
    "SAMPLE_MAX_CHARS",
    "SpeakerReading",
    "describe",
    "parse_reading",
    "sample_lines",
    "self_labelled_names",
]
