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
"""

from __future__ import annotations

import json

PROMPT_VERSION = "v10"

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
        "session_summary": {"type": "string"},
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "description": {"type": "string"},
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

    [00:12:34] ARAGORN: ...spoken text...

Speakers may be real names (already resolved) or raw diarization labels such as
SPEAKER_00 when a voice could not be matched to a player. A chunk may open
with a 'Party (player characters)' line listing the party's PCs.

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

Rules:
- language: code of the transcript's dominant language (see above).
- session_summary: 2-4 sentences summarizing what happens in this chunk.
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