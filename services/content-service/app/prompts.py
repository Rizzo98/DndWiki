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

v16 changes (each chunk narrates its own part of the session):
- the 10% chunk overlap is right for entities and wrong for the STORY. Two
  writers, each shown one half of the same scene, describe that scene twice in
  different words; merge_summary_lines de-duplicates on identical text, so both
  lines survive and the composer is handed two versions of one moment. In the
  v15 draft that produced a sentence introducing one NPC twice:
  "incontra una ragazza che porta il pranzo a suo zio, e ... una nana che porta
  il pranzo allo zio primario".
- the beats are PARTITIONED instead (chunking.owned_ranges): a chunk narrates
  from where the previous chunk stopped to where it stops itself, so the ranges
  tile the session exactly and every moment is written by exactly one call. The
  user message names the line the chunk's part starts at, and everything above
  it is context.
- nothing else changes: characters, locations, events and timeline entries are
  still extracted from every line the chunk can see, overlap included.

v17 changes (the beat budget is proportional to the part a chunk owns):
- v16's partition alone was not enough, and the fixture showed why: every chunk
  was still asked for "1-3 SHORT lines" however much of the session it owned, so
  a chunk whose part ended with a scene left that scene OUT rather than telling
  it twice. On the fixture session the scene of the girl carrying lunch to her
  uncle - the very scene that started all of this - disappeared from the summary
  in two runs out of two. Losing a scene is a different defect from splitting a
  person, not a fix for it.
- the user message now states how many transcript lines the chunk's part covers
  and asks for a number of beats proportional to it (chunking.beat_budget): ~25
  lines to a beat, never fewer than 3 and never more than 10. The fixture
  session goes from ~15 beats for 52 minutes to ~26.
- "one beat is one MOMENT" is spelled out, and so is the consequence that a part
  squeezed into three lines is a part that was not told.

v18 changes (the composer learns where each beat came from, and which language to
label places in):
- the extraction partition told each chunk which LINES it narrates, but that
  information stopped at the merger: the compose call received a flat list of
  beats and could not tell "one moment the session recorded twice" - the chunks
  overlap, so a scene falling on a boundary is described from both sides - from
  "two moments that look alike". It kept both, in one sentence, which is how
  "una ragazza che porta il pranzo a suo zio, e ... una nana che porta il pranzo
  allo zio primario" happened.
- every merged beat now carries [from-to], the span of the session its chunk
  narrated (merger.summary_beats). Owned spans never overlap across chunks, so
  "different spans" means "written by readings that could not see each other",
  and the compose prompt is given three rules built on that: same span, join
  freely; different spans, never write as one scene; different spans describing
  the same person or situation, tell it ONCE with the fuller description.
- the block labels had a language problem the prompt was making worse. The
  'where the session happens' section is the attribution engine's reading, written
  by an English-language prompt, and the compose prompt asked for table-language
  labels while handing over those strings verbatim. The section now says what it
  is and the label rule says labels name places the MATERIAL names, in the
  session's language, or stay empty.
"""

from __future__ import annotations

import json

from app.chunking import LINES_PER_BEAT, OwnedPart, beat_budget
from app.revision import indexed_for_prompt
from app.summary import blocks_with_places

#: v14 = the summary revision answers with a PATCH (summary + the items the
#: correction touches) instead of echoing the whole extraction, which did not
#: fit the completion cap and silently lost everything after the cut
#: (see the v14 note in the module docstring and app/revision.py).
#: v15 = the summary is a narrative in scene blocks and the DM reviews it by
#: highlighting portions of the text instead of ticking lines (see the v15 note
#: in the module docstring and app/summary.py).
#: v16 = the beats are partitioned across the overlapping chunks, so one moment is
#: narrated once instead of once per chunk that can see it (see the v16 note).
#: v17 = the beat budget is proportional to the part a chunk owns, because a flat
#: "1-3 lines" made a chunk leave out the last scene of its part rather than
#: describe it twice (see the v17 note).
#: v18 = every beat carries the span of the session it came from, so the compose
#: call can refuse to write two beats from different parts as one scene; and the
#: compose message says out loud that the record's "where the session happens"
#: reading is in another language and must not be copied into a block label.
#: v19 = the view contract covers BOTH inputs. It used to document only the
#: attributed view ([u_00412 ...] with "?" and "(unattributed)"), while the
#: diarized view renders [HH:MM:SS] SPEAKER_02: - so a run from a plain diarized
#: transcript was told to cite [u_XXXXX] ids that do not exist in it, and the model
#: answered by ENUMERATING INVENTED ONES (u_02896, u_02897, ...) until it ran out
#: of output tokens: the chunk was truncated and the whole session failed. v19
#: documents both forms, states which reference each one offers, and says what a
#: diarization label is (a voice, resolvable to a character only from the
#: transcript's own lines - never usable as a name).
#: v20 = the two rules a run from a DIARIZED transcript proved it needed. (1) A
#: diarization label never establishes who acted: a name goes on an action only
#: when the line's own words name the actor, address them by name, or have them
#: introduce themselves - everything else is party level. Measured on a real
#: session, the opposite behaviour produced a draft that had Rendar threatening
#: Letho with a knife (it was Galgith), Shiran offering the doctor gold, and the
#: deputy sheriff as "Shiran". (2) A beat must carry the SPECIFIC thing said or
#: learned; a beat that only says a conversation happened costs a beat and tells
#: the DM nothing, which is how the draft lost "li hanno seguiti" and "hanno
#: aperto qualcosa da cui e uscito qualcos'altro".
#: v21 = the composer stops settling a disagreement in the material by picking a
#: side. On a real session one beat had a being claim a name that another line in
#: the same scene refused ("io sono Galgith" answered by "conosco un Galgith, non
#: sei tu"), and the draft asserted the claim as fact in three runs out of four.
#:
#: MEASURED AND REVERTED IN THE SAME VERSION: v21 also asked the composer for the
#: published register - about 10-16 sentences rather than 15-30 - because the
#: draft ran two to four times longer than the summary the campaign wiki publishes
#: for the same session (455-964 words against 221-273). It did not work, and the
#: numbers say why: the length did not follow the instruction (515-738 words), and
#: on the shorter session it COST coverage, 100% of the benchmark's facts down to
#: 82%, with name coverage 69% down to 62%. The narrative's length follows the
#: number of beats, not the sentence budget - compressing prose that carries the
#: facts only drops facts. Asking for density is not how a summary becomes dense.
#: v22 = the diarized path is told which voices narrate the world (the '[Cast]'
#: note, app/speakers.py). On a diarized view the rule "the DM narrates the world
#: and is not a character" could never fire, because the line says SPEAKER_00 and
#: not "DM", so the session's own narration was read as the speech of an unknown
#: person.
#:
#: MEASURED AND NARROWED IN THE SAME VERSION: the first '[Cast]' note also carried
#: the character names the reading had assigned to voices, and told the extraction
#: to use them. Contradictions against the ground truth went from 1-4 per run to
#: 16, name coverage to 75%, and content coverage on the other fixture from 100% to
#: 73%. A name is applied to every beat that voice speaks in, so one wrong
#: assignment rewrites the session; a narrator assignment that is wrong costs only
#: its own lines. Names therefore stay where they can be checked line by line - the
#: label rules - and the note carries narrators only.
#: v23 = the extraction is given the CAMPAIGN'S ROSTER ('[Table]', app/roster.py):
#: which character each person at the table plays, and that those people are not in
#: the story. It is the one piece of context the attributed path gets for free and a
#: diarized session has not had at all, and two measured defects came straight out
#: of its absence: the draft wrote the PLAYERS into the fiction ("Giulia si
#: avvicina alle guardie", "Tommy si copre per non farsi riconoscere") and the
#: merger drafted character pages for both of them, tagged as player characters.
#: The roster is campaign data, not session data - campaign-service holds it,
#: refiner-service already fetches it for speaker attribution - and the worker
#: treats it as best-effort: an unreachable campaign summarises the session exactly
#: as v22 did.
#: v26 = a scene's CAST is not evidence of who acted in it. Measured: the scuffle
#: after the hospital arrival came out as "Rendar punta un pugnale al fianco di
#: Letho" in 7 runs of the last 10, because the model had two names it could
#: resolve (Rendar and Letho introduce themselves in that scene) and two people in
#: the fight, and paired them - while the knives belonged to a voice that never
#: named itself and Rendar had only walked in to mock them. The rules already said
#: a label establishes nothing; what they did not say is that the PRESENCE of a
#: name in a scene is not evidence either. (v24 tried a neighbouring phrasing -
#: "do not lend a name to another voice" - and changed nothing: 5 runs of 6. This
#: one names the actual inference the model was making.)
#:
#: v25 = the composer is told WHAT THE RECORD IS FOR, not just how long it is.
#: Measured against the summaries this campaign publishes for the same sessions,
#: the draft ran 555-684 words against 221-273 - and the extra length was extra
#: CONTENT, not padding: the food, the waiters, the dice rolls, the furniture, the
#: monkeys. The published summary carries the turning moments and nothing else,
#: because that is what a reader needs and what a DM can correct. v21 tried to fix
#: this with a sentence budget alone ("10-16 sentences") and failed: the draft
#: stayed long AND lost benchmark facts, 100% coverage down to 82%. A budget says
#: how much to cut and not what, so the model cut what it had just written rather
#: than the colour. v25 names both: which moments to keep, which to drop, and the
#: proportion that follows (one sentence per two or three beats).
#:
#: v24 = WHAT MAY BE A CHARACTER. A drafted character has to be ONE PERSON with a
#: name the table uses. A role is not a name ("lo sceriffo", "il vice sceriffo",
#: "il primario", "la nana", "il dottore"), a description is not a name ("l'uomo
#: urlante", "la creatura piumata"), and a people or a faction is not a person at
#: all ("gli elfi", "l'Indaco" - the two the session is ABOUT). All of them were
#: drafted as characters on the benchmark sessions, and every one becomes a wiki
#: page the DM has to delete.
#:
#: MEASURED AND REVERTED IN THE SAME VERSION: v24 first tried something else - a
#: rule that a name established by one line must not be lent to another voice in
#: the same scene, aimed at the single most persistent error left ("Rendar punta un
#: pugnale al fianco di Letho", 9 runs of 12, where the knife is a third voice's).
#: It did not work: the class survived in 5 runs of 6, and S1E2's contradictions
#: went from a mean of 3.4 to 5.5 while name coverage fell from 91% to 88%. The
#: error is in the BEATS, not the composer, and no wording changed it - the
#: recording gives one voice two labels and the model resolves the pair it can and
#: swaps their roles. That class needs the speaker map speaker-service produces
#: (evals/fixtures/s1e2_bugie_inutili/README.md), not another sentence here.
PROMPT_VERSION = "v26"

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
        "The reference at the start of EVERY transcript line this item was "
        "derived from, copied exactly as it appears there: the [u_XXXXX] id of "
        "the attributed view ([u_00412 00:41:15] Aramil: ...) or the [HH:MM:SS] "
        "timestamp of the diarized one ([00:41:15] SPEAKER_02: ...). At least "
        "one, and NEVER a reference you cannot see at the start of a line you "
        "were given: an invented reference is discarded, and so is the item "
        "that carries it."
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
                "The story of YOUR PART of the chunk, one SHORT line per beat, "
                "separated by newline characters. The user message says which "
                "line your part starts at and how many beats it asks for: write "
                "that many, one beat per moment. Chronological, no bullets, no "
                "numbering, no blank lines, and never a beat for a moment that "
                "begins before your part starts."
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

You are given one chunk of a session transcript. It arrives in ONE of two
forms, and WHICH FORM IT IS decides what you may cite and who you may name:

  ATTRIBUTED
    [u_00412 00:41:15] Aramil: I cast fireball on the three goblins
    [u_00413 00:41:19] Aramil?: wait, do I still get my attack?
    [u_00414 00:41:24] (unattributed): ok so I move up to the door

  DIARIZED
    [00:41:15] SPEAKER_02: I cast fireball on the three goblins
    [00:41:24] SPEAKER_05: ok so I move up to the door

The reference at the START of a line identifies it: the u_ id in the attributed
view, the TIMESTAMP in the diarized one. Cite the references you can see there
and nothing else. Never invent a reference, and never carry a u_ id into a view
that shows timestamps: an item whose references cannot be resolved is discarded,
so inventing them destroys the item it was meant to support.

A chunk may open with a 'Party (player characters)' line listing the party's PCs,
with a '[Table]' note, with a '[Cast]' note, and with a '[Stretches]' note saying
where those lines happen and who the record puts there.

'[Table]' is the CAMPAIGN'S OWN ROSTER: each player character and the person who
plays them. It settles the two things a transcript cannot: which names are player
characters, and which names are the real people at the table. Use the CHARACTER
names for everything, and NEVER write a player's name as a character or as the
actor of a beat - the character acted. A name the roster does not list is an NPC,
the game master, or a stranger: treat it as an NPC until the lines say otherwise.

'[Cast]' names the voices that NARRATE the world. It is a READING, not an
identification, and it lists NARRATORS ONLY: every other voice is a person at the
table the recording does not name, and the rules for labels apply to it unchanged.
For the voices it does name, their lines are the game master describing places,
events and NPCs - so what they say about who did what is a FACT of the session and
the best material you have. Extract the world from those lines instead of looking
for a character to attribute them to.

IN THE DIARIZED FORM THERE IS NO ATTRIBUTION AT ALL, and this is the rule that
matters most in it. A line's name is a label for a VOICE ("SPEAKER_02"), not a
person, and the diarization is not perfect: one voice can be split across two
labels inside one scene, and one label can cover two people. So a label NEVER
establishes who did something.

You may put a character's name on an action in these three cases ONLY:

1. the words of a line NAME the actor as the one acting, in the third person
   ("Galgith estrae un pugnale e glielo punta al fianco" - the narration says
   who acts, so write Galgith);
2. a line ADDRESSES someone by name and the answering line adopts it
   ("Galgith, che fai?" ... "Provo ad aprire la porta" - the answering voice is
   Galgith from here on);
3. a line has a character INTRODUCE themselves by name.

DO NOT WORK OUT WHO FROM WHO IS PRESENT. A scene that involves two party members
does not tell you which of them did what, and the two names you happen to have
resolved are not a pair to hand out to the two people in the fight: that is how a
scuffle between a woman and a man came out as "Rendar punta un pugnale al fianco di
Letho", when the knives belonged to a voice that never named itself and Rendar had
only walked in to mock them. If the lines do not say who acts, write the action
without a name - "uno dei protagonisti punta un pugnale al fianco di un altro" -
however obvious the inference feels. An action with the wrong name is worse than
an action with no name: the DM can add a name in one edit, but cannot tell which
of two names was the mistake.

Everything else stays at party level ("il gruppo...", "uno dei protagonisti").
This is not a hedge, it is the correct answer: a wrong name is worse than no
name, because the DM can add a name in one edit but cannot tell which of two
names was the mistake. If two of your beats could swap the same name without
changing what the lines say, the name is a guess and must not be written.

THE THREE LINE FORMS OF THE ATTRIBUTED VIEW MEAN THREE DIFFERENT THINGS:

* "Aramil:" - the attribution is confident. Treat the line as a fact.
* "Aramil?" - the attribution is UNCERTAIN. You may record factual content that
  appears in these lines, but you must NEVER attribute it to Aramil: no fact may
  claim Aramil did, said, decided or owns anything on the strength of a "?" line.
  Describe it at party level ("the party ...") or leave it out.
* "(unattributed):" - the engine could not tell who spoke. NEVER attribute this
  content to a named character. Describe it at party level or omit it.

A "[Stretches]" note is not a transcript line and carries no reference of its
own: it is the record's reading of where the lines below it happen.

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

What may be a CHARACTER (this is narrower than it looks):
- ONE PERSON, with a name the table actually uses. Everything else belongs in the
  text, not in an entry. Do NOT create a character for:
  * a ROLE or a title, however specific it sounds: "lo sceriffo", "il vice
    sceriffo", "il primario", "il dottore", "la guardia", "il nobile" - the role
    is what the table calls them, not who they are;
  * a DESCRIPTION: "l'uomo urlante", "la creatura piumata", "la nana", "il
    mezzelfo possente" - a phrase built out of what somebody looked like or did
    is not a name, and a page titled with it is a page the DM has to delete;
  * a PEOPLE, a faction or a group: "gli elfi", "l'Indaco", "le guardie". A group
    is not a person. These belong in the story and in the events, not in
    'characters'.
  If a person is only ever described that way, leave the entry out and let the
  text say what the lines say. An entry you cannot title with a name the table
  used is an entry the wiki cannot file.

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
- session_summary: the story of YOUR PART of this chunk as one SHORT line per
  beat, separated by newline characters (\n). The user message says which line
  your part starts at AND how many beats it asks for: write that many.
  Chronological order, no bullet characters, no numbering, no blank lines: the
  session page shows one line per row so the DM can correct it line by line.
  ONE BEAT IS ONE MOMENT, so a stretch that holds three moments takes three
  beats rather than one longer line, and the moments at the END of your part
  need their beats as much as the first one does. A part squeezed into three
  lines is a part that was not told.
  A moment that BEGINS before your part starts belongs to the chunk before you,
  which can see those same lines and is describing them: do NOT write a beat for
  it, however clearly you can see it. Two chunks telling one moment in different
  words is how a single person becomes two people in the summary.
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
    owned: OwnedPart | None = None,
    cast: str = "",
    lines_per_beat: int | None = None,
) -> str:
    """User message for one chunk: index context + the transcript view.

    'out_of_world' lists resolved speaker names that narrate but are not part
    of the world (the DM); the model is reminded never to make characters of
    them.

    'owned' is the slice of the session this chunk narrates (chunking.OwnedPart):
    where it starts, where it ends, and how many lines it covers.
    Everything above it is the tail of the previous chunk, repeated so that an
    entity near the boundary is seen twice - which is the wrong thing for the
    story, because two writers who cannot see each other's work describe one
    moment twice in different words and nothing downstream can tell the two lines
    apart. Saying where the chunk's own part begins is what makes the partition
    real: the model narrates only from there, while still using everything it can
    see for the entities.

    Its SIZE is the other half. v16 partitioned the beats and kept asking for
    "1-3 lines" regardless of how much of the session a chunk owned, so a chunk
    whose part ended with a scene left the scene out instead of describing it
    twice - on the fixture session the last chunk owns fifty-odd lines and its
    girl never got a beat. The budget is now proportional (chunking.beat_budget):
    a part that covers a quarter of an hour owes more beats than one that covers
    five minutes.

    Both notes come BEFORE the lines they are about, so the rules are read before
    the transcript rather than after it.
    """
    note = ""
    if out_of_world:
        names = ", ".join(sorted({n.strip() for n in out_of_world if n.strip()}))
        note = (
            f"\n\nOut-of-world speakers (never characters): {names}. "
            "They narrate the world; do NOT create a character for them."
        )
    parts = [
        f"This is chunk {chunk_index + 1} of {total_chunks} of the session transcript."
    ]
    # Only a chunk that HAS something above it gets the boundary sentence: the
    # first one owns the session from its first line, so there is nothing to
    # exclude. That is a question about the chunk's position, which is why it is
    # decided here and not by an empty marker in the data.
    if owned is not None and chunk_index > 0:
        parts.append(
            f"Your part of this session starts at {owned.start} and covers "
            f"{owned.lines} transcript lines. The lines before it belong to the "
            "previous chunk and are here as CONTEXT: use them to get the names and "
            "who is present right, but do NOT write a session_summary beat for a "
            f"moment that begins before {owned.start}. The chunk before you can see "
            "those lines too, and it is describing them."
        )
    if owned is not None:
        low, high = beat_budget(
            owned.lines,
            lines_per_beat=lines_per_beat or LINES_PER_BEAT,
        )
        parts.append(
            f"Write the beats of your part as {low} to {high} lines. ONE BEAT IS ONE "
            "MOMENT: a stretch that holds three moments takes three beats, not one "
            "longer line. Cover the whole part including how it ends - the last "
            "moment of your part needs its beat as much as the first one does.\n"
            "A beat must carry the SPECIFIC thing: what was said, what was learned, "
            "what was seen. The sentence a reader would quote back IS the beat - "
            "'il paziente dice che li hanno seguiti', 'gli Indaco hanno aperto "
            "qualcosa da cui \u00e8 uscito qualcos'altro', 'un pennello sporco di "
            "tintura indaco'. A general sentence about the scene ('il gruppo parla "
            "con il paziente') costs a beat and tells the DM nothing; a beat that "
            "only says a conversation happened is a beat wasted."
        )
    # The '[Cast]' note goes in front of the lines, like the '[Stretches]' note and
    # for the same reason: it is a reading of what the lines BELOW it establish,
    # and the rules have to be read before the transcript rather than after it.
    parts.append(f"{cast}{chunk_view}{note}")
    return "\n\n".join(parts)

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
- EVERY BEAT CARRIES THE SPAN OF THE SESSION IT CAME FROM, in square brackets in
  front of it, and that span is how you tell a moment recorded twice from two
  moments. The session was cut into fixed-size pieces to be read, the pieces
  OVERLAP, and each beat was written by somebody who saw only their own piece.
  A scene that falls on a cut is therefore recorded from BOTH sides, in
  different words, by readings that could not see each other. When two beats
  from different spans describe what looks like the same person, the same object
  or the same situation in the same place, they are ONE moment seen twice: tell
  it ONCE, with the fuller description, and never report both as though there
  were two. Reporting both is how a single person becomes two people in a
  summary.
  The spans are a mechanical cut of the transcript, NOT the scenes of the
  session: they say nothing about how the story divides into blocks, and they are
  never mentioned in it.
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
- A 'location' LABEL IS WRITTEN IN THAT SAME LANGUAGE and names a place the
  material names ("Ospedale di Fatumastra"), in the form the table says it. The
  'where the session happens' reading below was made in another language: it
  tells you WHERE a stretch happens and says nothing about what to call it, so
  never copy its words into a label. If the material names no place for a block,
  leave the label empty - an empty label means "the same place as before", which
  is better than a name nobody at the table ever said.
- WHAT THIS RECORD IS FOR decides what goes in it. The timeline below already
  holds every moment of the session, one line each; the DISCOVERIES, the
  characters and the places become their own pages. What the story has to carry
  is what a reader needs to follow the session and what the DM needs to CORRECT:
  who did what, what was said or learned that mattered, what changed, and how it
  ended. So KEEP: the moments that turn the session (the words that started it,
  what the group learned, the decisions, the arrivals and departures, the
  ending). DROP: local colour (what was eaten or drunk, the waiters, the furniture,
  the weather), the mechanics (dice rolls, initiative, "con un tiro di
  percezione"), and anything that only happened because the table was talking out
  of character.
- HOW LONG: around 10-16 sentences for a full session, one opening and a few
  sentences for a short one. That is roughly one sentence per two or three beats -
  the beats are notes, the story is what you make of them, and two beats that
  belong to one moment share a sentence. A draft that gives each beat its own
  sentence has not summarised anything.

What you must NOT do:
- Never invent a fact, a name, an outcome or a motive that the material does not
  contain. If a stretch is thin, say less about it.
- Do not write a list of separate one-sentence beats, and do not state the same
  thing twice in two blocks.
- Never settle a disagreement in the material by picking a side. When one beat
  says a being claimed something and another line refuses the claim - "io sono
  Galgith" answered by "conosco un Galgith, non sei tu" - write what happened
  without the contested name; do not write either reading as fact. The same goes
  for a name the material spells two ways: use the one the recording uses most.
- Do not reproduce the material: no beat number, no [span], and no sentence
  copied from a beat. A beat is a note about something that happened; the story
  is what you write from it, in your own words and in connected prose.
- Do not add a title, headings, bullets or numbering.
- Do not address the reader ("in questa sessione..."; "il gruppo poi..." is
  fine, "come potete vedere" is not).
"""


#: The second, mechanical pass over a finished summary: the composer both SELECTS
#: and WRITES, and it will not drop content it has decided to keep (measured: a
#: sentence budget alone made drafts shorter and lost benchmark facts, 100% to 82%).
#: Asking a call that sees ONLY the finished text to tighten it separates the two
#: jobs - nothing new may enter, every fact must survive, and the only thing that
#: may go is the connective tissue between them.
SUMMARY_TIGHTEN_SYSTEM_PROMPT = """You are tightening a tabletop RPG session record.

You are given the record as it was written, and a target length. Rewrite it
SHORTER without losing anything that happened.

What may go:
* sentences that only carry the reader from one scene to the next;
* a fact said twice, or said once and then restated as a consequence;
* the connectives and the scene-setting that surround a fact.

What may NOT go, ever:
* who did what, and who was present;
* what was said, learned, decided or discovered;
* how a scene ended and how the session ended;
* any proper name the record uses.

Merging is the tool: two facts that happened in the same moment belong in ONE
sentence. Do not summarise at a higher altitude - a reader must still be able to
correct any statement in it.

Keep the place labels ('location') exactly as they are, and keep the record in the
same language.

Respond with a single JSON object:

{"session_summary": [{"location": "<unchanged>", "text": "<the tightened text>"}]}
"""


def _beat_lines(current: dict) -> list[str]:
    """The beats, each prefixed with the span of the session it came from.

    Prefers the rendered beats the merger produced (current['summary_beats']).
    Falls back to splitting the narrative when there is no provenance - a
    summary reloaded from the database, an older revision - and the composer
    then works as it did before v18, minus the ability to tell a moment recorded
    twice from two moments.
    """
    rendered: list[str] = []
    for beat in current.get("summary_beats") or []:
        if not isinstance(beat, dict):
            continue
        text = str(beat.get("text") or "").strip()
        if not text:
            continue
        start = str(beat.get("from") or "").strip()
        end = str(beat.get("to") or "").strip()
        rendered.append(f"[{start}-{end}] {text}" if start and end else text)
    if rendered:
        return rendered
    return [
        line.strip()
        for line in str(current.get("session_summary") or "").splitlines()
        if line.strip()
    ]


def build_summary_tighten_message(blocks: list[dict], *, target_sentences: int) -> str:
    """User message for the tightening pass: the record, and how long it should be."""
    rendered = "\n\n".join(
        f'[{"location: " + block["location"] if block.get("location") else "same place as before"}]\n'
        f'{block.get("text") or ""}'
        for block in blocks
    )
    return (
        f"The record, as written ({len(blocks)} block(s)):\n\n{rendered}\n\n"
        f"Rewrite it in about {target_sentences} sentences in total, across the same "
        "blocks and the same places. Merge what belongs to one moment; keep every "
        "fact, every name and every outcome."
    )


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
    attribution engine read, when it ran - 'scenes').

    Each beat carries the SPAN of the session it came from (v18). That span is
    the only thing in this message that the merger could not have supplied by
    comparing text, and it is what lets this call tell a moment the session
    recorded twice - the chunks overlap, so a scene on a boundary is described
    from both sides - from two moments that merely look alike.
    """
    beats = _beat_lines(current)
    parts = [f"Session language: {language or current.get('language') or 'en'}", ""]
    parts.append(
        "Beats, in the order they were distilled. These are NOTES for you to write "
        "from, not text to reproduce: the [span] in front of each is the part of "
        "the session it came from, and neither the span nor the wording of a beat "
        "belongs in the story you write."
    )
    parts += [f"  - {line}" for line in beats]

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
                "Where the session happens, as the record reads it. WARNING: the "
                "record reads in another language, so the place names below are "
                "NOT in the session's language and are NOT what to call those "
                "places in a 'location' label - use them to know WHERE a stretch "
                "happens, and label the block with the places named above. "
                "(Stretch by stretch: the place, who is there, who is elsewhere.)"
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
    narrative exactly as displayed instead of the stored one. The client sends
    it as TEXT - the place labels are block metadata the displayed prose does
    not carry - so the blocks are rebuilt from it with the stored labels
    re-attached (app/summary.py). A blank override is ignored rather than
    allowed to erase the narrative, and sending the displayed text can never
    cost the session its places again.
    """
    payload = dict(current)
    if summary_text_override and summary_text_override.strip():
        payload["session_summary"] = summary_text_override
        payload["summary_blocks"] = blocks_with_places(
            summary_text_override,
            current.get("summary_blocks") or current.get("session_summary"),
        )

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
