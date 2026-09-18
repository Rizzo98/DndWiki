"""Versioned prompt templates + strict JSON extraction schema.

'PROMPT_VERSION' is the version of this module's prompt/schema and is recorded
on every 'generation_jobs' row so the DM can see which prompt produced a draft.
Bump it (e.g. to "v3") whenever the schema or instructions change in a way that
affects output.

The schema describes what the LLM must return per chunk of the transcript;
merging across chunks happens in app/merger.py.

v2 changes (wiki-quality pass):
- 'language': the dominant language of the transcript; ALL output text must be
  written in it, so the wiki reads like the table talks (no forced English).
- entities carry two fact lists: 'facts' are durable, cross-session knowledge
  (who someone is, where a place is, who runs it) while 'session_facts' are
  one-session specifics ("a ranting man stood near the inn") that belong to
  the session record, not to the entity page body.
- locations/characters must be proper names; generic common nouns ("city",
  "la citta", "the inn") never become entities - their information is
  attributed to the nearest named entity instead.

v3 changes (wiki categories pass):
- characters gain 'is_party': True for one of the party's PLAYER characters.
  The wiki always names players by their CHARACTER, never the player name;
  chunk views may open with a "Party (player characters)" line listing them.
- locations gain 'place_type' and 'part_of', mapped by the merger into the
  location page's structured attributes (location_type / region).
- events and timeline entries feed ONLY the session summary (shown on the
  session page); they never become wiki pages anymore.

v4 changes (character page sections):
- characters gain 'physical_look' (appearance) and 'personality'
  (temperament/mannerisms); the merger routes them into the character page's
  Physical look / Personality sections instead of a generic Summary.

v5 changes (auto-filled character attributes + durable relationships):
- characters gain the static info fields of the page — 'race', 'class',
  'gender', 'height', 'weight', 'age' — filled ONLY when the chunk states or
  unambiguously implies them (e.g. "he is a man" -> gender "Male").
- characters gain 'relationships': PERSISTENT ties only (membership,
  alliance, leadership) mapped to wiki relations. One-off events ("met at
  the inn", "fought together") must NOT appear here.

v6 changes (cross-session wiki quality + narrator + location naming):
- the wiki is CROSS-SESSION: no field may reference the chunk, the session,
  the fragment or the transcription ("non interviene in questo frammento"),
  and no filler ("member of the group", "not much is known") is allowed —
  entities without durable info are omitted, never padded.
- the game master (Dungeon Master / DM / Narratore) narrates but is NOT part
  of the world: never a character.
- personality must be durable temperament/mannerisms, never a reaction to a
  single scene or an unexplained reference ("affascinato dalla creatura").
- generically-named locations get UNIQUE proper names composed with their
  named anchor ("Ospedale" -> "Ospedale di Fatumastra"); without an anchor
  the location is not emitted.

v7 changes (ownership relations + linkable cross-references):
- characters gain 'relationships.owner': the proper names of places, items
  or organizations the character durably owns ("È il proprietario della
  Locanda del Fumo Aspro" -> owner ["Locanda del Fumo Aspro"]).
- wiki text becomes linkable: every reference to a named entity must use
  its exact proper name (never only a pronoun or a generic descriptor), so
  the renderer can turn the name into a link to that entity's page.

v10 changes (extraction robustness):
- explicit instruction that EVERY schema key must be present in the output
  JSON, using [] for empty categories: models that omit a key for an empty
  category (most often 'events' / 'timeline_entries') made the parser reject
  the chunk; the parser now tolerates the omission, and this rule makes it
  rarer at the source.

v9 changes (world timeline events):
- events become WIKI PAGES + campaign timeline entries (not just session
  facts): the pipeline drafts an event page and a pending timeline entry per
  event. To keep the timeline meaningful, events are STRICTLY limited to
  world-significant moments — events that change the state of the world, the
  campaign or major characters. Routine session activity (travel, shopping,
  idle talk, minor fights) must NOT be emitted.
- events gain 'in_world_date' (the campaign calendar date when the table
  states one, else empty) and 'event_type' (battle / negotiation /
  discovery / quest / catastrophe / political / other).

v8 changes (location page structure):
- locations gain type-specific detail fields so the wiki page can render
  structured sections instead of a bare summary: settlements carry
  population/government/ruler/demographics/economy/defenses/religion/
  districts/notable_locations, regions carry capital/terrain/climate,
  worlds carry pantheon/planes, buildings carry owner/purpose, dungeons
  carry entrance/levels/hazards, wilderness carries terrain/climate/hazards/
  flora_fauna — fill ONLY what the chunk states.
- locations gain 'founded' (era/date the place was founded) and 'history'
  (durable past recounted in the chunk: founding, wars, famous events).
  'history' is a narrative section of the page, never a session fact.

v11 changes (reviewable session summary + DM feedback loop):
- 'session_summary' became a LIST OF LINES: 1-3 short beats per chunk, one
  beat per line, newline separated (no bullets, no numbering). The merger
  concatenates the chunks' lines into the whole-session summary the DM reads
  on the session page, where each line is individually selectable and
  correctable - a single long paragraph could not be reviewed line by line.
- the module also ships SUMMARY_REVISION_PROMPT, the second LLM entry point:
  it applies the DM's corrections (select some lines + describe the change)
  to an already extracted session, so a fixed attribution propagates to the
  summary, the entities, the events and the timeline entries at once.

v14 changes (the revision answers with a PATCH, not with the extraction):
- SUMMARY_REVISION_PROMPT no longer asks for the complete corrected extraction.
  Echoing it cost ~8k output tokens on a real session against a 4096 cap, so
  the provider cut the response off mid-JSON, json_repair closed the tail into a
  syntactically valid partial extraction, and everything after the cut (the
  locations, the events, the timeline entries) kept the PREVIOUS revision's
  text: a correction reached the summary and never the events.
- the revision now returns 'session_summary' (always, in full) plus
  'updates'/'additions'/'removals' addressed by the per-item 'id' the model was
  given ('e1' = the second event). Everything the patch does not mention keeps
  its current value, which is what makes an unpropagated correction impossible
  instead of silent (app/revision.py).

v15 changes (the summary is a narrative, reviewed by portion):
- the summary was a list of one-beat lines, each written by a pass that could
  not see the others: the DM read "Hann Caleto si risveglia in una gabbia" next
  to "il gruppo libera una creatura piumata" as if the creature were somebody
  else. It is a NARRATIVE now: SUMMARY_COMPOSE_PROMPT writes the session as one
  continuous story in scene blocks ({location, text}), so the sentences carry
  over from one to the next and the same being keeps its name.
- the DM no longer ticks lines: they highlight an ARBITRARY PORTION of that
  text with the mouse and say what must change. The revision prompt quotes the
  highlighted passage (which can start and end mid-sentence) and returns the
  whole corrected narrative as blocks again.
- the blocks are where the "where this session happens" reading lives now: each
  block carries the place that part of the story happens in, and the session
  page renders it as a place chip instead of a separate panel.
"""

from __future__ import annotations

import json

from app.revision import indexed_for_prompt
from app.summary import text_to_blocks

#: v14 = the summary revision answers with a PATCH (summary + the items the
#: correction touches) instead of echoing the whole extraction, which did not
#: fit the completion cap and silently lost everything after the cut
#: (see the v14 note in the module docstring and app/revision.py).
#: v15 = the summary is a narrative in scene blocks and the DM reviews it by
#: highlighting portions of the text instead of ticking lines (see the v15 note
#: in the module docstring and app/summary.py).
PROMPT_VERSION = "v15"

#: Every extracted item must say WHICH transcript lines produced it.
#:
#: This single field is what makes the whole pipeline auditable: a fact on a
#: character page can be traced back to the utterance that produced it, and from
#: there to a timestamp and an audio span. It is also what the attribution gate
#: reads: a fact whose references do not resolve to a confident attribution
#: never reaches a page (docs/attribution-model.md S14.3, S14.4).
_SOURCE_REFS: dict = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "The [u_XXXXX] ids of the transcript lines this item was derived "
        "from, exactly as they appear at the start of those lines. At least "
        "one. An item with no reference cannot be checked against the "
        "attribution and is discarded."
    ),
}

#: Strict JSON schema given to the LLM (OpenAI-style; LiteLLM passes it through
#: to providers that support response_format; others just follow instructions).
EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "language": {
            "type": "string",
            "description": (
                "BCP-47-ish code of the transcript's dominant language, "
                "e.g. 'en', 'it'."
            ),
        },
        "session_summary": {
            "type": "string",
            "description": (
                "The chunk's story as 1-3 SHORT lines, one beat per line, "
                "separated by newline characters. Chronological, no bullets, "
                "no numbering, no blank lines."
            ),
        },
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "description": {"type": "string"},
                    "source_refs": _SOURCE_REFS,
                    "physical_look": {
                        "type": "string",
                        "description": (
                            "ONLY the character's physical appearance grounded "
                            "in the chunk: build, height, hair, eyes, "
                            "distinctive features, attire. Never role, group "
                            "membership or behavior; empty when the chunk "
                            "gives no appearance details."
                        ),
                    },
                    "personality": {
                        "type": "string",
                        "description": (
                            "The character's DURABLE personality: temperament, "
                            "mannerisms, values, quirks. Never a reaction to a "
                            "single scene or an unexplained reference; empty "
                            "when the chunk gives none."
                        ),
                    },
                    "race": {
                        "type": "string",
                        "description": (
                            "The character's race as stated in the chunk "
                            "(e.g. 'Human', 'Half-Elf'). Empty unless stated."
                        ),
                    },
                    "class": {
                        "type": "string",
                        "description": (
                            "The character's D&D class as stated in the chunk "
                            "(e.g. 'Ranger', 'Wizard'). Empty unless stated."
                        ),
                    },
                    "gender": {
                        "type": "string",
                        "description": (
                            "The character's gender as stated or unambiguously "
                            "implied in the chunk (e.g. 'Male' for 'a man'). "
                            "Empty when not determinable."
                        ),
                    },
                    "height": {
                        "type": "string",
                        "description": (
                            "Stated height, e.g. '1.78 m' or '5'11\"'. "
                            "Empty unless stated."
                        ),
                    },
                    "weight": {
                        "type": "string",
                        "description": (
                            "Stated weight, e.g. '82 kg'. Empty unless stated."
                        ),
                    },
                    "age": {
                        "type": "string",
                        "description": (
                            "Stated or clearly implied age, e.g. '27' or "
                            "'elderly'. Empty unless determinable."
                        ),
                    },
                    "relationships": {
                        "type": "object",
                        "properties": {
                            "member_of": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Proper names of factions, guilds, groups or "
                                    "places the character durably belongs to."
                                ),
                            },
                            "allied_with": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Proper names of characters or factions the "
                                    "character has a lasting alliance with."
                                ),
                            },
                            "led_by": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Proper names of characters or factions that "
                                    "durably lead or command this character."
                                ),
                            },
                            "owner": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Proper names of places, items or "
                                    "organizations the character durably owns "
                                    "or runs."
                                ),
                            },
                        },
                        "description": (
                            "PERSISTENT relationships ONLY: membership, alliance, "
                            "leadership, ownership - ties that remain true "
                            "after this session. Never put one-off events here "
                            "(met at the inn, fought together today); those are "
                            "session_facts."
                        ),
                    },
                    "facts": {"type": "array", "items": {"type": "string"}},
                    "session_facts": {"type": "array", "items": {"type": "string"}},
                    "is_party": {
                        "type": "boolean",
                        "description": (
                            "True when this character is one of the party's "
                            "PLAYER characters (listed in the optional "
                            "'Party (player characters)' line of the chunk)."
                        ),
                    },
                    "mentions": {"type": "integer"},
                },
                "required": ["name", "description", "mentions"],
            },
        },
        "locations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "description": {"type": "string"},
                    "source_refs": _SOURCE_REFS,
                    "facts": {"type": "array", "items": {"type": "string"}},
                    "session_facts": {"type": "array", "items": {"type": "string"}},
                    "place_type": {
                        "type": "string",
                        "description": (
                            "Kind of place as spoken: city, town, village, "
                            "region, continent, world, building "
                            "(inn/tower/temple/castle...), structure "
                            "(bridge/gate/wall...), dungeon, wilderness."
                        ),
                    },
                    "part_of": {
                        "type": "string",
                        "description": (
                            "Named larger area containing this place "
                            "(region, kingdom, ...), if mentioned."
                        ),
                    },
                    "history": {
                        "type": "string",
                        "description": (
                            "The place's DURABLE past as recounted in the "
                            "chunk: founding, wars, famous events. A narrative "
                            "section of the wiki page. Empty when the chunk "
                            "gives none."
                        ),
                    },
                    "founded": {
                        "type": "string",
                        "description": (
                            "Era/date the place was founded or first appears "
                            "('1290 DR', 'Age of Myth'). Empty unless stated."
                        ),
                    },
                    "population": {
                        "type": "string",
                        "description": (
                            "Population of a settlement as stated, e.g. "
                            "'12,000' or '~12.000 abitanti'. Empty unless "
                            "stated."
                        ),
                    },
                    "government": {
                        "type": "string",
                        "description": (
                            "Form of government of a settlement or region "
                            "('Council of Elders', 'feudal lordship'). Empty "
                            "unless stated."
                        ),
                    },
                    "ruler": {
                        "type": "string",
                        "description": (
                            "Proper name of the place's ruler or head "
                            "(settlement, region). Empty unless stated."
                        ),
                    },
                    "demographics": {
                        "type": "string",
                        "description": (
                            "Who lives there: races, groups, notable mix "
                            "(settlement). Empty unless stated."
                        ),
                    },
                    "economy": {
                        "type": "string",
                        "description": (
                            "Main trade, industries or wealth of a settlement. "
                            "Empty unless stated."
                        ),
                    },
                    "defenses": {
                        "type": "string",
                        "description": (
                            "Walls, garrison, guard or other defenses of a "
                            "settlement. Empty unless stated."
                        ),
                    },
                    "religion": {
                        "type": "string",
                        "description": (
                            "Dominant faith or places of worship of a "
                            "settlement. Empty unless stated."
                        ),
                    },
                    "districts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Named districts/quarters of a city or town "
                            "('Porto', 'Alto Quartiere'). Empty unless stated."
                        ),
                    },
                    "notable_locations": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Proper names of landmarks or notable places "
                            "within this place (plazas, monuments, taverns...). "
                            "Empty unless stated."
                        ),
                    },
                    "capital": {
                        "type": "string",
                        "description": (
                            "Proper name of the capital city of a region or "
                            "continent. Empty unless stated."
                        ),
                    },
                    "terrain": {
                        "type": "string",
                        "description": (
                            "Landscape of a region/continent/world/wilderness "
                            "('rolling hills', 'desert'). Empty unless stated."
                        ),
                    },
                    "climate": {
                        "type": "string",
                        "description": (
                            "Climate of a region/continent/world/wilderness "
                            "('temperate', 'arctic'). Empty unless stated."
                        ),
                    },
                    "pantheon": {
                        "type": "string",
                        "description": (
                            "Deities/gods worshipped in a world. Empty unless "
                            "stated."
                        ),
                    },
                    "planes": {
                        "type": "string",
                        "description": (
                            "Cosmology/planes of existence of a world. Empty "
                            "unless stated."
                        ),
                    },
                    "owner": {
                        "type": "string",
                        "description": (
                            "Proper name of who owns or manages a "
                            "building/structure. Empty unless stated."
                        ),
                    },
                    "purpose": {
                        "type": "string",
                        "description": (
                            "Function of a building/structure ('inn', "
                            "'toll gate'). Empty unless stated."
                        ),
                    },
                    "entrance": {
                        "type": "string",
                        "description": (
                            "How one enters a dungeon ('secret door under "
                            "the altar'). Empty unless stated."
                        ),
                    },
                    "levels": {
                        "type": "string",
                        "description": (
                            "Depth/levels of a dungeon ('3 floors', 'one "
                            "cavern level'). Empty unless stated."
                        ),
                    },
                    "hazards": {
                        "type": "string",
                        "description": (
                            "Dangers of a dungeon or wilderness ('pit traps', "
                            "'hostile tribes'). Empty unless stated."
                        ),
                    },
                    "flora_fauna": {
                        "type": "string",
                        "description": (
                            "Plants and animals of a wilderness. Empty unless "
                            "stated."
                        ),
                    },
                    "mentions": {"type": "integer"},
                },
                "required": ["name", "description", "mentions"],
            },
        },
        "events": {
            "type": "array",
            "description": (
                "WORLD-SIGNIFICANT events only: they get their own wiki page "
                "and a campaign timeline entry. See the prompt rules."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": (
                            "Short proper-name phrase: 'The Siege of "
                            "Fatumastra', 'Death of Captain Marta'."
                        ),
                    },
                    "description": {"type": "string"},
                    "source_refs": _SOURCE_REFS,
                    "actor": {
                        "type": "string",
                        "description": (
                            "The character who performed the event, exactly "
                            "as named in the transcript; EMPTY when the "
                            "transcript does not attribute it. Never guess: an "
                            "event with no named actor is described at party "
                            "level, and a guessed actor is a fact the session "
                            "does not contain."
                        ),
                    },
                    "participants": {"type": "array", "items": {"type": "string"}},
                    "in_world_date": {
                        "type": "string",
                        "description": (
                            "Campaign calendar date of the event as stated by "
                            "the table, in a short compact form (e.g. '17 Ches "
                            "1492 DR'); empty when no date is mentioned."
                        ),
                    },
                    "event_type": {
                        "type": "string",
                        "description": (
                            "One of: battle, negotiation, discovery, quest, "
                            "catastrophe, political, other."
                        ),
                    },
                },
                "required": ["title", "description"],
            },
        },
        "timeline_entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "time": {"type": "string"},
                    "summary": {"type": "string"},
                    "source_refs": _SOURCE_REFS,
                    "characters": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["time", "summary"],
            },
        },
    },
    "required": [
        "language",
        "session_summary",
        "characters",
        "locations",
        "events",
        "timeline_entries",
    ],
}

SYSTEM_PROMPT = f"""You are a campaign wikifier for a tabletop RPG (Dungeons & Dragons).

You are given one chunk of a session transcript. The transcript lines look like:

    [u_00412 00:41:15] Aramil: I cast fireball on the three goblins
    [u_00413 00:41:19] Aramil?: wait, do I still get my attack?
    [u_00414 00:41:24] (unattributed): ok so I move up to the door

The reference (u_00412) identifies the line; the time is when it was said; the
name is who the attribution engine believes was speaking. A chunk may open with
a 'Party (player characters)' line listing the party's PCs, and with a
'[Stretches]' note saying where those lines happen and who the record puts there.

THE THREE LINE FORMS MEAN THREE DIFFERENT THINGS. Respect them:

* "Aramil:" - the attribution is confident. Treat the line as a fact.
* "Aramil?" - the attribution is UNCERTAIN. You may record factual content that
  appears in these lines, but you must NEVER attribute it to Aramil: no fact may
  claim Aramil did, said, decided or owns anything on the strength of a "?" line.
  Describe it at party level ("the party ...") or leave it out.
* "(unattributed):" - the engine could not tell who spoke. NEVER attribute this
  content to a named character. Describe it at party level or omit it.

* "[Stretches]" — the note in front of the lines, not a transcript line. It is
  what the record knows about WHERE this part of the session happens, and it is
  the only statement you have about who was present. When it says that ONE party
  member is present and the others are elsewhere, every party member who acts or
  experiences something in these lines IS that member: write their name as the
  actor. That is the difference between a beat the wiki can use ("Hann Caleto si
  risveglia in una gabbia") and a beat with a hole in it ("un personaggio si
  risveglia in una gabbia"). When it says a member is elsewhere, attribute
  nothing in these lines to them.

EVERY item you extract must carry "source_refs": the [u_XXXXX] ids of the lines
it came from. This is not optional bookkeeping - a fact that cannot be traced
back to the lines that produced it is discarded before it reaches a page, and so
is a fact whose lines were not confidently attributed. When in doubt, extract
less and cite precisely.

Never write a raw diarization label (SPEAKER_00, Speaker A, V3) as a character
name or an actor. A label is a measurement of audio, not a person.

Extract, ONLY from the given chunk, a single JSON object matching EXACTLY this
schema (no markdown, no commentary outside the JSON):

{json.dumps(EXTRACTION_SCHEMA)}

Language:
- Detect the dominant natural language actually spoken at the table and report
  it in 'language' (e.g. "it", "en").
- Write EVERY text field you output ('session_summary', descriptions, facts,
  session_facts, event titles/descriptions, timeline summaries) in that same
  language. The wiki must read exactly like the players talk.
- Never translate proper names: keep character and location names as spoken.

Proper names only:
- characters and locations must have a PROPER NAME. Never create an entry for
  a generic common noun such as "the city" / "città", "the village" /
  "paese", "the inn" / "osteria", "the tavern", "the guard" / "guardia".
- If something notable happens at a generically-named place, attribute the
  information to the nearest NAMED place mentioned with it (e.g. screams at
  "the inn" right after the party reached "Fatumastra" belong to Fatumastra),
  otherwise drop it entirely.
- Locations need UNIQUE PROPER names. When the table refers to a place only
  by a common noun or generic descriptor ("ospedale", "strada maestra",
  "l'osteria", "the hospital", "main street"), compose the name with the
  nearest NAMED place that contains it: "Ospedale di Fatumastra",
  "Strada Maestra di Fatumastra", and set 'part_of' to that named place.
  If the chunk names no such anchor, do NOT emit the location at all.
- If the players use both a generic word and a proper name for the same
  place/person, use ONLY the proper name as the entity name (you may keep the
  generic word among 'aliases').

Player characters vs NPCs:
- A party member speaks under their CHARACTER's name, never their own. When a
  character is one of the party's player characters (see the 'Party' line),
  set 'is_party' to true and always refer to them by the character name -
  even when the transcript mentions the player's real name out of game.
- Everyone else (innkeepers, villains, strangers) is an NPC: leave 'is_party'
  false or omit it.

The narrator is not a character:
- Speakers such as "Dungeon Master", "DM", "Master", "Narratore", "Narrator"
  are the game master narrating the world — they are OUT of the world. NEVER
  create a character entry for them; their speech describes the world, the
  places and the other characters.

The wiki is CROSS-SESSION:
- Every text field must read like a standalone wiki entry. NEVER mention the
  chunk, the fragment, the session, the episode or the transcription itself
  ("in questo frammento", "in this fragment", "in questa sessione").
- No filler: never write "membro del gruppo" / "member of the group",
  "non interviene" / "does not intervene", "non si sa molto" / "not much is
  known". If an entity yields no durable information, omit it entirely (or
  leave the field empty).
- Never leave dangling references: if you cannot NAME the thing ("la
  creatura", "the creature", "la città", "the city", "l'uomo urlante"), do
  not refer to it — either name it or drop the statement. Wiki text must
  stand alone without the transcript.
- NEVER write "un personaggio" / "a character" / "someone" / "una persona" as
  the actor of a beat. It is never the right answer: either the '[Stretches]'
  note names the party member (use the name), or the actor is an NPC you can
  name from these lines (use that name), or the beat has no actor you can
  support — in which case describe it at party level ("il gruppo...") or drop
  it. An unnamed actor is a beat the wiki cannot use and the reader cannot
  correct.

Rules:
- language: code of the transcript's dominant language (see above).
- session_summary: the story of this chunk as 1-3 SHORT lines, one beat per
  line, separated by newline characters (\n). Chronological order, no bullet
  characters, no numbering, no blank lines: the session page shows one line
  per row so the DM can correct it line by line.
- characters: named characters that appear or are mentioned. Keep the name as
  spoken (capitalize properly). aliases: other names used for the same person,
  including generic descriptors ("the innkeeper"). description: who they are /
  what they do, grounded in the chunk. physical_look: their physical appearance
  (build, height, hair, eyes, distinctive features, attire) grounded in the
  chunk; only appearance - never role, membership or behavior - and leave
  empty when the chunk gives no appearance details. personality: DURABLE
  temperament, mannerisms, values or quirks that characterize them; never a
  reaction to one scene or an unexplained reference ("affascinato dalla
  creatura"); leave empty when the chunk gives none.
  Static info (race, class, gender, height, weight, age): fill ONLY what the
  chunk states or makes unambiguous — "a man" -> gender "Male", "half-elf" ->
  race "Half-Elf". Leave every other field empty; never guess.
  relationships: PERSISTENT ties only, as proper names under the matching
  type — 'member_of' (faction/guild/group the character belongs to),
  'allied_with' (lasting alliance), 'led_by' (who durably commands them),
  'owner' (places, items or organizations the character durably owns or
  runs: "È il proprietario della Locanda del Fumo Aspro" -> owner
  ["Locanda del Fumo Aspro"]). Leave a type empty when the chunk gives no
  such tie. mentions: how many times the character is referenced in this
  chunk.
- locations: named places that appear or are mentioned (same shape as
  characters). Names must be UNIQUE and proper: a generic noun alone
  ("Ospedale", "Strada Maestra") is never enough — compose it with the named
  place that contains it ("Ospedale di Fatumastra") or omit the location.
  Also fill 'place_type' with the kind of place as spoken and 'part_of' with
  the named larger area it belongs to, when either is clear from the chunk.
  Type-specific detail fields (population, government, ruler, demographics,
  economy, defenses, religion, districts, notable_locations, capital,
  terrain, climate, pantheon, planes, owner, purpose, entrance, levels,
  hazards, flora_fauna, founded): fill ONLY what the chunk explicitly states
  or makes unambiguous, keeping the players' phrasing; leave every other
  field empty, never guess. 'districts' and 'notable_locations' are lists of
  PROPER names only. 'history' is a narrative of the place's durable past
  (founding, wars, famous events) told in the chunk; empty when the chunk
  does not recount the past. Facts that fit a specific field go there and
  NOT also into 'facts' (e.g. a stated population goes to 'population',
  not to 'facts').
- descriptions must state what the entity IS (durable identity, position,
  ownership); one-shot happenings ("dove portano l'uomo urlante") go to
  'session_facts', never into descriptions or 'facts'.
- facts vs session_facts (both characters and locations):
  * 'facts' are DURABLE truths that stay valid after this session and will
    live on the entity's wiki page: identity, appearance, where the place is,
    who owns/manages it, standing relationships. Example for a location:
    "È la città principale della costa", "L'osteria è gestita da Marta".
  * 'session_facts' are ONE-SHOT things that happened in THIS chunk involving
    the entity; they belong to the session record, not the wiki page. Example:
    "Un uomo urlante era vicino all'osteria", "Urli provenivano dall'osteria".
  * When unsure whether a statement is durable, put it in 'session_facts'.
- relationships vs session_facts: a relationship ('member_of', 'allied_with',
  'led_by', 'owner') must be a tie that is TRUE ACROSS SESSIONS — membership,
  a lasting alliance, a chain of command, a durable ownership. Never put a
  sporadic event there: "they met at the inn", "they fought together today",
  "he helped them once" are session_facts, NOT relationships. When unsure,
  prefer session_facts.
- linkable cross-references: when text mentions a named entity, use its exact
  proper name every time (never only "lui", "l'oste", "la locanda", "quel
  posto" once the name is known) — the wiki renders entity names as links to
  their pages, and only the exact name resolves.
- events: ONLY WORLD-SIGNIFICANT events — the ones that belong on a
  campaign-wide timeline because they change the state of the world, the
  campaign or its major characters. Each emitted event becomes its own wiki
  page, so be strict: a session has many happenings but few true events.
  INCLUDE: major battles or sieges, deaths, betrayals, alliances or enmities
  formed/broken, quests given or completed, important discoveries, catastrophes,
  political changes, the arrival/departure of important figures, powerful magic
  that alters things. EXCLUDE routine session activity even when memorable:
  shopping, travel, rest, idle conversation, combat with unnamed or minor
  enemies, tavern drinking, exploring with no consequence. When in doubt,
  EXCLUDE — the session summary and timeline_entries still capture the scene.
  title: a short proper-name phrase ("The Siege of Fatumastra", "Death of
  Captain Marta"). description: 1-3 sentences in the table's language.
  participants: character names involved. in_world_date: the campaign calendar
  date when the table states one, otherwise empty; keep it SHORT (a compact
  calendar notation like "17 Ches 1492 DR" or "33esimo giorno del primo mese,
  settima era" — never a long prose description of the date). event_type: one
  of battle / negotiation / discovery / quest / catastrophe / political / other.
- timeline_entries: notable beats with their transcript time in HH:MM:SS
  format and a one-line summary; characters involved. These stay session-scoped
  (they are NOT events and never become pages).
- If a category has nothing in this chunk, return an empty array for it.
- ALWAYS include every key of the schema in your JSON: language,
  session_summary, characters, locations, events, timeline_entries — never
  omit a key just because its category is empty (use [] instead).
- Only include what the chunk actually supports; do not invent. When unsure
  about a fact, keep the statement short and unprejudiced.
"""


def build_chunk_message(
    chunk_view: str,
    chunk_index: int,
    total_chunks: int,
    out_of_world: list[str] | None = None,
) -> str:
    """User message for one chunk: index context + the transcript view.

    'out_of_world' lists resolved speaker names that narrate but are not part
    of the world (the DM); the model is reminded never to make characters of
    them.
    """
    note = ""
    if out_of_world:
        names = ", ".join(sorted({n.strip() for n in out_of_world if n.strip()}))
        note = (
            f"\n\nOut-of-world speakers (never characters): {names}. "
            "They narrate the world; do NOT create a character for them."
        )
    return (
        f"This is chunk {chunk_index + 1} of {total_chunks} of the session "
        f"transcript.\n\n{chunk_view}{note}"
    )

# ---------------------------------------------------------------------------
# Session-summary revision (the DM feedback loop)
# ---------------------------------------------------------------------------
#
# The session summary is the intermediate layer of the pipeline: it is drafted
# from the transcript, the DM reviews it line by line and asks for changes
# ("it wasn't Character A, it was Character B"), and only the confirmed
# summary becomes wiki pages and timeline events. This second prompt applies
# those corrections to the WHOLE extraction - not just to the summary text -
# so a corrected attribution cannot survive in the character page, the event
# description or the timeline while the summary says otherwise.



# --- composing the session summary ------------------------------------------
#
# The extraction pass runs PER CHUNK, and every chunk writes its own 1-3 beats
# without seeing the others. Concatenated, that is a list of separate moments
# with no thread - which is what the DM read: "a patch of sentences".
#
# So the merged beats go through one more pass that can see the WHOLE session
# at once, and writes the story from them: an opening line that sets the scene,
# then the beats joined into a narrative. The line-per-beat contract survives
# (the DM selects single lines and asks for changes), and so does the rule that
# nothing may be invented: the material is the beats, the events and the
# timeline, never the transcript.

SUMMARY_COMPOSE_SYSTEM_PROMPT = """You are the editor of a tabletop RPG (Dungeons & Dragons) session record.

You receive one session as it was distilled PART BY PART: a list of beats in
order, plus the places, the events and the timeline entries extracted from it.
Each beat was written by somebody who could see only their own part of the
session, so the material reads as a string of separate moments with no thread.
Your job is to write the session's STORY from it, for the Dungeon Master to
read and correct.

Respond with a single JSON object:

{"session_summary": [{"location": "<where this part happens, or empty>",
                      "text": "<that part of the story>"}, ...]}

The blocks are read one after the other as ONE CONTINUOUS TEXT: they are
paragraphs of the same story, not a list of statements. The Dungeon Master
highlights a passage of that text and asks for a change, so it has to read like
a story somebody tells - and the places are what tell the DM where each passage
happens.

How to write it:
- The FIRST block opens the session: where it starts, who is there, and what is
  already in motion or at stake. It is what makes the rest make sense.
- Then the session's course, in the order it happened.
- CONNECT everything. Every sentence continues the one before it and leads into
  the one after: "il gruppo", "la creatura", "l'uomo" refer to people already
  named, and something that happened is told ONCE, where it belongs in the
  story.
- ONE BEING, ONE NAME. The beats were written one at a time, so the SAME being
  can appear as "una creatura piumata" in one and by name in another, as if
  there were two. When two beats describe the same being in the same situation
  - the same cage, the same cart, the same room, the same moment - they are ONE
  being, and the story tells it once, with the name the material gives it: if a
  beat says Hann Caleto wakes up inside a covered cage and another says the
  party opens a cage on the cart and frees a feathered creature, then the
  creature IS Hann Caleto, and the story says so. The cast listed below is the
  session's own names: use them instead of "una creatura" / "un uomo" whenever
  the material has named the being.
- START A NEW BLOCK when the story moves to another place, and put that place in
  'location' EXACTLY as the table calls it ("Locanda del Fumo Aspro", "Ospedale
  di Fatumastra"). Leave 'location' empty when the material does not say where
  that part happens, and also when the new block stays in the place of the one
  before it: an empty label means "same place as before". Never invent a place
  name, and do not repeat in the text a place that is already the label of the
  block.
- Write PROSE, not a list of statements: beats that belong to the same moment
  become one passage, connectives carry the reader from one to the next ("nel
  frattempo", "poco dopo", "mentre"), and the sentences do not all open the same
  way (three passages in a row starting with "Il gruppo..." is not a story).
- Use the table's own language for everything: the session's language, the names
  as they were spoken, the places as they were called.
- Cover the whole session, including how it ends: around 15-30 sentences for a
  long session, an opening and a few sentences for a short one. It is a record
  the DM is reading in order to correct it, not a teaser.

What you must NOT do:
- Never invent a fact, a name, an outcome or a motive that the material does not
  contain. If a stretch is thin, say less about it.
- Do not write a list of separate one-sentence beats, and do not state the same
  thing twice in two blocks.
- Do not add a title, headings, bullets or numbering.
- Do not address the reader ("in questa sessione..."; "il gruppo poi..." is
  fine, "come potete vedere" is not).
"""


def build_summary_compose_message(
    current: dict,
    *,
    language: str | None = None,
    scenes: list[dict] | None = None,
) -> str:
    """User message for the compose call.

    The material is the beats in order, plus everything that gives the story a
    thread and a place: the extracted locations, the events, the timeline, and
    the record's own reading of where the session happens (the scenes the
    attribution engine read, when it ran - 'scenes'). The places are what the
    model labels the blocks with, so they are stated explicitly instead of
    being left to be inferred from the beats.
    """
    beats = [
        line.strip()
        for line in str(current.get("session_summary") or "").splitlines()
        if line.strip()
    ]
    parts = [f"Session language: {language or current.get('language') or 'en'}", ""]
    parts.append("Beats, in the order they were distilled:")
    parts += [f"  {index}. {line}" for index, line in enumerate(beats, start=1)]

    places = [
        str(location.get("name") or "").strip()
        for location in current.get("locations") or []
        if isinstance(location, dict) and str(location.get("name") or "").strip()
    ]
    if places:
        parts += ["", "Places mentioned in this session:"]
        parts += [f"  - {name}" for name in places]

    cast = [
        str(character.get("name") or "").strip()
        for character in current.get("characters") or []
        if isinstance(character, dict) and str(character.get("name") or "").strip()
    ]
    if cast:
        # The session's own names, so the story can say "Hann Caleto" where a
        # beat written on its own said "una creatura": the cast is what makes
        # one being keep one name across the whole narrative.
        parts += [
            "",
            (
                "Characters in this session (use these names when the story "
                "mentions them; never list them):"
            ),
        ]
        parts += [f"  - {name}" for name in cast]

    if scenes:
        parts += [
            "",
            (
                "Where the session happens, as the record reads it (stretch by "
                "stretch: the place, who is there, who is somewhere else):"
            ),
        ]
        for scene in scenes:
            place = str(scene.get("place") or "").strip() or "place not stated"
            present = ", ".join(str(n) for n in scene.get("present") or [])
            absent = ", ".join(str(n) for n in scene.get("absent") or [])
            line = f"  - {place}"
            if present:
                line += f" — there: {present}"
            if absent:
                line += f"; somewhere else: {absent}"
            parts.append(line)

    events = current.get("events") or []
    if events:
        parts += ["", "Events:"]
        for event in events:
            title = str(event.get("title") or "").strip()
            summary = str(event.get("description") or event.get("summary") or "").strip()
            parts.append(f"  - {title}: {summary}" if summary else f"  - {title}")

    timeline = current.get("timeline_entries") or []
    if timeline:
        parts += ["", "Timeline:"]
        for entry in timeline:
            label = str(entry.get("label") or entry.get("title") or "").strip()
            when = str(entry.get("timestamp") or entry.get("start") or "").strip()
            parts.append(f"  - {when} {label}".rstrip())

    parts += [
        "",
        (
            "Write the session's story as the JSON object described in your "
            "instructions: one continuous narrative, opening with the scene the "
            "session starts in, then the session in order, split into blocks that "
            "follow the story from place to place - each block naming the place it "
            "happens in."
        ),
    ]
    return "\n".join(parts)


# --- applying the DM's corrections to the whole extraction ------------------
#
# v14: the revision answers with a PATCH, never with the extraction.
#
# It used to ask for the COMPLETE corrected extraction: every character (the
# whole field union), every location, every event and every timeline entry had
# to be echoed back. On a real session that is ~8k output tokens against the
# 4096 completion cap, so the provider cut the response off mid-JSON,
# json_repair closed the tail into a syntactically valid PARTIAL extraction and
# the worker restored the categories the cut had removed from the PREVIOUS
# revision: the corrected summary shipped next to stale events, silently.
#
# The patch keeps the response at a few hundred tokens - the summary (always
# complete: the DM reviews it line by line) plus the items the correction
# touches, addressed by the 'id' every item is given in the message. Nothing
# else moves, so a correction that never reached the events is now impossible
# rather than invisible (app/revision.py holds the merge and its errors).

SUMMARY_REVISION_SYSTEM_PROMPT = """You maintain the session record of a tabletop RPG (Dungeons & Dragons) campaign.

You receive the CURRENT extraction of one session - its summary lines, its
characters, its locations, its events and its timeline entries, each item
carrying an "id" - plus the Dungeon Master's correction requests.

You return a PATCH: only what the corrections change. You are NOT asked to
repeat the extraction, and everything you leave out keeps the value it already
has. A correction you leave out is a correction that did not happen.

Respond with a single JSON object (no markdown, no commentary outside it):

{
  "session_summary": [
    {"location": "<where this part happens, or empty>", "text": "<that part of the story>"}
  ],
  "updates": {
    "characters":       {"c3": {"description": "...", "session_facts": ["..."]}},
    "locations":        {"l0": {"description": "..."}},
    "events":           {"e1": {"description": "...", "participants": ["..."]}},
    "timeline_entries": {"t7": {"summary": "..."}}
  },
  "additions": {
    "characters": [], "locations": [], "events": [], "timeline_entries": []
  },
  "removals": {
    "characters": [], "locations": [], "events": [], "timeline_entries": []
  }
}

How the patch works:
- "session_summary" is ALWAYS the WHOLE narrative, block by block, in order,
  even when the correction concerns only one passage of it: the blocks the DM
  did not ask about are copied verbatim, labels included. The blocks are read
  one after the other as one continuous text, so the story has to keep reading
  as a story. Add a block when the correction adds a scene, drop one when it
  removes it, and leave a block with an empty "location" when the story does
  not say where that part happens.
- "updates" maps the id of an item (its "id" in the current extraction: "c3" is
  the fourth character, "e1" the second event, "t7" the eighth timeline entry)
  to the fields you change. Name ONLY those fields: every other field of that
  item keeps its value, and so does every item you do not mention. A list
  ("session_facts", "participants", "characters", ...) is replaced ENTIRELY by
  the list you return.
- "additions" holds complete new items, with the same field names as the
  current ones, when the correction introduces something the extraction missed.
- "removals" holds the ids of the items the correction says are not real.
- Leave a section out, or leave it empty, when the correction does not need it.

How to apply a correction:
- A request quotes the PASSAGE of the narrative it is about - the DM highlighted
  it in the text with the mouse, so it can begin and end in the middle of a
  sentence - and states what must change (for example the passage "Character A
  was going to the city center" with the request "It wasn't Character A, it
  was Character B"). Find that passage in the narrative, in the block that
  holds it.
- Apply it EVERYWHERE the same fact appears: the summary lines, the affected
  characters (description, facts, session_facts, relationships), the
  locations, the events (description, actor, participants) and the
  timeline entries (summary, characters). An attribution fixed in the summary
  but left wrong in the events is a bug: before answering, go through the
  items once more and patch every place the fact appears.
- Rewrite the passage the request is about so it says what the DM says, and
  smooth the sentences around it so the block still reads as one story. When
  the DM supplies the corrected wording, use their wording, in the session's
  language. Do not describe the correction itself ("the DM corrected...",
  "previously it was...") - the summary must read as the plain record of what
  happened.
- When the request is about WHERE something happened ("non erano a Fatumastra,
  erano all'ospedale"), fix the "location" label of the block that passage
  belongs to; when the correction moves the story to a place that has no block,
  split the block there and give the new one its place.
- When a correction moves an action from one character to another, drop the
  wrong character from that fact (including 'participants' lists) and add the
  right one.
- The DM may also ask for removals, additions, reorderings, a different
  level of detail or a different tone: follow the request, staying inside the
  information the session already contains.
- Never invent new events, characters or locations that the correction did
  not introduce, and never drop information the DM did not ask about.

Rules that always hold:
- Keep the extraction's language: every text field stays in the language of
  the table.
- Touch nothing the correction does not concern, and never repeat an item just
  to echo it: an unchanged item must not appear in "updates" at all.
- The narrative stays a story: connected prose, chronological, no headings, no
  bullets, no numbering, no blank lines inside a block, and no sentence that
  only exists to repeat the one before it. If the DM asked for a longer or a
  shorter version, rewrite it as a whole rather than padding single blocks.
- Keep the field names and the ids of the current extraction: an update that
  addresses an id you were not given is dropped, and the whole revision fails.
- Never change a 'confidence' number, a 'mentions' count, a 'source_refs' list
  or the language: they come from the extraction.
- Only include what the session supports: no invented facts, no filler.
"""


def build_summary_revision_message(
    current: dict,
    edits: list[dict] | None = None,
    summary_text_override: str | None = None,
) -> str:
    """User message for one summary-revision call.

    'current' is the extraction as persisted for the session (the narrative +
    characters/locations/events/timeline entries), sent to the model with an
    'id' on every item so the patch it returns can address them. 'edits' are the
    DM's correction requests, each {'targets': [passage, ...], 'instruction':
    str} - the passages are the portions of the narrative the DM highlighted,
    quoted verbatim, and an empty 'targets' means the request is about the
    summary as a whole. 'summary_text_override' lets the DM's client send the
    narrative exactly as displayed instead of the stored one; the blocks are
    then rebuilt from it (the labels come back from the model's own answer).
    """
    payload = dict(current)
    if summary_text_override:
        payload["session_summary"] = summary_text_override
        payload["summary_blocks"] = text_to_blocks(summary_text_override)

    requests: list[str] = []
    for index, edit in enumerate(edits or [], start=1):
        instruction = (edit.get("instruction") or "").strip()
        if not instruction:
            continue
        targets = [t.strip() for t in (edit.get("targets") or []) if isinstance(t, str) and t.strip()]
        if targets:
            quoted = "\n".join(f'    - "{t}"' for t in targets)
            requests.append(
                f"{index}. Passage(s) of the summary concerned:\n{quoted}\n"
                f"   Change requested: {instruction}"
            )
        else:
            requests.append(
                f"{index}. Concerned passage: the whole session summary.\n"
                f"   Change requested: {instruction}"
            )
    if not requests:
        requests.append(
            "1. Concerned passage: the whole session summary.\n"
            "   Change requested: tighten the story to the session's key moments."
        )

    return (
        "CURRENT EXTRACTION (JSON), every item carrying its 'id':\n"
        f"{json.dumps(indexed_for_prompt(payload), ensure_ascii=False)}\n\n"
        "CORRECTION REQUESTS FROM THE DUNGEON MASTER:\n"
        + "\n".join(requests)
        + "\n\nReturn the patch JSON object described in your instructions: the "
        "whole narrative in 'session_summary', plus only the items the "
        "corrections change."
    )
