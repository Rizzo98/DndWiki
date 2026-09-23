"""Cross-chunk merge: dedupe entities, merge facts, resolve conflicts.

Each chunk produces an independent extraction; the merger combines them:

- entities (characters, locations) are keyed by a normalized name; aliases,
  facts and mention counts are merged across chunks
- descriptions resolve by "longest wins" (most detail, still grounded)
- conflicts surface as multiple facts (the DM reviews drafts anyway)
- per-entity confidence = fraction of chunks that mentioned the entity
- overall job confidence = mean of entity confidences

Wiki-quality guards (v2):

- entities whose name is made ONLY of generic common nouns ("the city",
  "la citta", "the guard") are dropped before they can become junk pages;
  the prompt already asks the model not to emit them, this is the net.
- 'facts' stay durable cross-session knowledge; 'session_facts' travel
  separately and land on the entity page as a session-referenced block
  (session_references), so session-specific details are always traceable to
  the session that produced them.
- build_page_drafts() drafts CHARACTER and LOCATION pages; build_event_drafts()
  drafts WORLD-SIGNIFICANT events as event pages (kind='event') and upserts
  their campaign timeline entries (pending DM approval). Events already on the
  timeline are updated, not duplicated. Session recaps stay on the session page.
- an extraction whose name (or alias) matches an existing page is NOT drafted
  again; its new facts remain visible on the persisted session summary shown
  on the session page. A fuzzy (near-match) hit still becomes a draft, plus
  a 'possible_duplicate' relation proposal pointing at the existing page.
- player characters are tagged attributes.character_type='player' (vs 'npc')
  from the party's member character names and the model's 'is_party' hint;
  the wiki always refers to players by their CHARACTER name, never the
  player's own name.

Also maps the merged result to wiki-service draft payloads (PageCreate JSON)
plus 'possible_duplicate' relation proposals for near-matches of existing
pages.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from app.attribution import is_raw_label
from app.chunking import OwnedPart

#: Status of the payloads built here. They are PROPOSALS, not pages: the
#: planner turns them into the change set the DM reviews, and only the
#: confirmed set is written (published) by wiki-service.
DRAFT_STATUS = "draft"
DEFAULT_VISIBILITY = "public"

#: Relation proposed when a fresh extraction looks like an existing page.
POSSIBLE_DUPLICATE = "possible_duplicate"

#: Relation types auto-proposed from extracted durable relationships.
#: ONLY persistent ties - membership, alliance, leadership. One-off events
#: (appears_in) and system tags (possible_duplicate) are never auto-proposed.
_DURABLE_RELATION_TYPES = ("member_of", "allied_with", "led_by", "owner")

#: Minimum SequenceMatcher ratio for two names to be considered the same
#: real-world entity by fuzz alone (token containment is checked separately).
NEAR_MATCH_RATIO = 0.84

#: The same question for two CHARACTERS of ONE session, where the names being
#: compared are spellings of one voice rather than two pages of a wiki. A
#: recording that hears "Jackie" once as "Jace" (ratio 0.80) is the case this
#: exists for; the ratio is looser than the cross-session one because a
#: mis-hearing is a one-edit distortion, and it demands the two names OPEN alike
#: so "Sceriffo" and "vicesceriffo" (also 0.80, two different officers) stay two
#: people. Calibrated on the pairs two real sessions produced - see _same_entity.
CHARACTER_NEAR_MATCH_RATIO = 0.78


# --------------------------------------------------------------------------
# generic-name detection
# --------------------------------------------------------------------------

#: Articles/prepositions that carry no identity; ignored when judging whether
#: a name is generic and when comparing two names.
_FILLER_TOKENS = {
    "the", "a", "an", "of", "at", "in", "on",
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una",
    "di", "del", "dello", "della", "dei", "delle", "dal", "dalla",
    "da", "su", "l", "d",
}

#: Common nouns that never identify a place on their own (English + Italian,
#: since tables speak either).
_GENERIC_LOCATION_WORDS = {
    "city", "town", "village", "hamlet", "capital",
    "citta", "città", "paese", "villaggio", "borgo", "frazione", "capoluogo",
    "kingdom", "realm", "empire", "duchy", "county", "region", "province",
    "regno", "impero", "ducato", "contado", "regione", "provincia", "marca",
    "inn", "tavern", "pub", "lodging",
    "osteria", "taverna", "locanda", "albergo",
    "castle", "keep", "fortress", "citadel", "stronghold",
    "castello", "rocca", "fortezza", "cittadella", "fortino",
    "tower", "torre", "temple", "shrine", "church", "cathedral", "abbey",
    "tempio", "santuario", "chiesa", "cattedrale", "abbazia", "monastero", "convento",
    "market", "marketplace", "shop", "store",
    "mercato", "negozio", "bottega",
    "house", "manor", "palace", "villa", "building",
    "casa", "palazzo", "palazzotto",
    "street", "road", "alley", "square", "bridge", "gate", "walls", "wall",
    "strada", "via", "vicolo", "piazza", "ponte", "porta", "mura", "muro",
    "river", "lake", "sea", "ocean", "mountain", "mountains", "hill", "valley",
    "fiume", "lago", "mare", "oceano", "montagna", "monti", "collina", "valle",
    "forest", "woods", "wood", "swamp", "marsh",
    "foresta", "bosco", "selva", "palude",
    "cave", "cavern", "grotto", "mine", "dungeon", "crypt", "ruins",
    "caverna", "grotta", "miniera", "cripta", "rovine", "sotterranei",
    "camp", "campsite", "outpost", "harbor", "port", "dock", "farm", "mill",
    "campo", "accampamento", "avamposto", "porto", "molo", "fattoria", "mulino",
    "district", "quarter", "quartiere", "zona",
    # institutions / venues: never identify a place on their own ("Ospedale",
    # "Strada Maestra", "the hospital") - the wiki composes them with the
    # named place that contains them ("Ospedale di Fatumastra")
    "hospital", "clinic", "school", "academy", "university", "college",
    "ospedale", "clinica", "scuola", "accademia", "universita", "università", "collegio",
    "prison", "jail", "cemetery", "graveyard", "library", "bank", "pharmacy",
    "barracks", "laboratory", "workshop", "morgue", "embassy", "courthouse",
    "carcere", "prigione", "cimitero", "biblioteca", "banca", "farmacia",
    "caserma", "laboratorio", "officina", "obitorio", "ambasciata", "tribunale",
    "theater", "theatre", "museum", "restaurant", "bakery", "station",
    "teatro", "museo", "ristorante", "panetteria", "forno", "stazione",
    "municipio", "sede", "ufficio", "uffici",
}

#: Common nouns that never identify a person/NPC on their own.
_GENERIC_CHARACTER_WORDS = {
    "man", "woman", "boy", "girl", "child", "kid", "elder", "old", "young",
    "uomo", "donna", "ragazzo", "ragazza", "bambino", "bambina", "vecchio",
    "vecchia", "giovanotto", "giovane",
    "guard", "guardsman", "soldier", "knight", "captain", "sergeant", "sentinel",
    "guardia", "scorta", "soldato", "cavaliere", "capitano", "sergente", "sentinella",
    "stranger", "beggar", "merchant", "farmer", "blacksmith", "innkeeper",
    "priest", "priestess", "mage", "wizard", "thief", "bandit", "sailor",
    "villager", "local", "noble", "lord", "lady", "bard", "hunter",
    "sconosciuto", "sconosciuta", "mendicante", "mercante", "contadino",
    "contadina", "fabbro", "oste", "prete", "sacerdote", "sacerdotessa",
    "mago", "stregone", "strega", "ladro", "bandito", "marinaio", "paesano",
    "abitante", "nobile", "signore", "dama", "barde", "cacciatore", "cacciatrice",
    "voice", "figure", "shadow", "crowd", "people", "pair", "group", "gang",
    "voce", "figura", "ombra", "folla", "gente", "gruppo", "banda",
    # --- ROLES and DESCRIPTIONS measured as drafted characters ----------------
    # Every word below headed a character entry the pipeline produced on the two
    # benchmark sessions, each of which becomes a wiki page the DM has to delete:
    # "Sceriffo", "Vice sceriffo", "Vicario", "il primario", "la nana", "la
    # scimmietta", "Creatura piumata", "Uomo urlante", "il merlo". They are what
    # the table CALLS somebody, not who they are.
    "sheriff", "deputy", "marshal", "constable", "doctor", "nurse", "chief",
    "sceriffo", "vicesceriffo", "vice", "vicario", "primario", "dottore",
    "dottoressa", "medico", "infermiere", "infermiera", "capo",
    "creature", "beast", "animal", "monkey", "bird", "raven", "crow", "gnome",
    "dwarf", "halfling", "elf", "orc", "half-orc", "half-elf",
    "creatura", "bestia", "animale", "scimmia", "scimmietta", "uccello",
    "corvo", "merlo", "gnomo", "nano", "nana", "elfo", "elfi", "orco",
    "mezzorco", "mezzelfo", "umanoide",
}

#: Out-of-world speaker names that must never become character pages: the
#: game master narrates the world but is not part of it. The worker also
#: excludes the campaign's actual DM speaker dynamically (by user id); this
#: set is the hardcoded net for the default names and common table slang.
_NARRATOR_NAMES = frozenset({
    "dungeon master", "dm", "master", "game master", "gm",
    "narratore", "narrator",
    "il dungeon master", "il master", "il narratore", "la voce narrante",
})

#: Whole generic phrases ("strada maestra" = "main street") that never
#: identify a place on their own even though one word is not generic.
_GENERIC_LOCATION_PHRASES = frozenset({
    "strada maestra", "via principale", "strada principale",
    "corso principale", "main street",
    "piazza principale", "grande piazza", "piazza del mercato",
    "mercato centrale", "market square",
    "porta principale", "porta della citta", "porta della città", "city gate",
    "mura della citta", "mura della città", "city walls",
    "grande tempio", "gran tempio", "cattedrale principale",
    "town hall", "municipio", "ufficio postale", "post office",
})

_WORD_RE = re.compile(r"[\w]+")


def _normalize(name: str) -> str:
    """Case/whitespace-insensitive entity key."""
    return " ".join(name.lower().split())

def normalize_entity_name(name: str) -> str:
    """Public case/whitespace-insensitive entity key (worker-facing)."""
    return _normalize(name)


def _tokens(name: str) -> list[str]:
    """Lowercased word tokens of a name (unicode-aware)."""
    return _WORD_RE.findall(name.lower())


def _content_tokens(name: str) -> frozenset[str]:
    """Identity-bearing tokens of a name (filler articles removed)."""
    return frozenset(t for t in _tokens(name) if t not in _FILLER_TOKENS)


def is_generic_name(name: str, kind: str) -> bool:
    """True when every word of the name is a generic/filler common noun.

    Only WHOLE-generic names are rejected ("Citta", "the city", "la citta",
    "Strada Maestra"), so legitimate proper names that merely contain such
    words survive ("Old Town", "Citta di Fatumastra").

    A DIARIZATION LABEL is rejected outright. is_generic_name("SPEAKER_00",
    "character") used to be False, so nothing stopped the model from emitting one
    and the merger from drafting a character page for it - the single most
    visible symptom of the old design (docs/attribution-model.md S13).
    """
    if is_raw_label(name):
        return True
    normalized = _normalize(name)
    if kind == "location" and normalized in _GENERIC_LOCATION_PHRASES:
        return True
    tokens = _tokens(name)
    if not tokens:
        return True
    lexicon = (
        _GENERIC_CHARACTER_WORDS if kind == "character" else _GENERIC_LOCATION_WORDS
    )
    # A PHRASE WITH NO NAME IN IT IS NOT A PAGE. "Uomo urlante", "Creatura piumata",
    # "Vice sceriffo", "la nana": the phrase is built on a generic noun and the
    # qualifier does not turn it into a name. Requiring EVERY token to be generic
    # let all four through, because the qualifier is in no lexicon - and checking
    # only one end of the phrase does not work either, because Italian puts the head
    # first ("creatura piumata") and English puts it last ("screaming man").
    #
    # What separates a name from a description is a CAPITALISED word after the
    # first: "Sir Lucius", "Hann Caleto", "Miles Falco", "Vice Sceriffo Miles Falco"
    # all carry one and keep their page; none of the phrases above does.
    if (
        kind == "character"
        and not _has_proper_token(name)
        and (tokens[0] in lexicon or tokens[-1] in lexicon)
    ):
        return True
    return all(t in _FILLER_TOKENS or t in lexicon for t in tokens)


def _has_proper_token(name: str) -> bool:
    """Whether a phrase carries a name inside it: a capitalised word after the first.

    Used to tell a NAME ("Sir Lucius", "Vice Sceriffo Miles Falco") from a phrase
    built out of what somebody was or did ("Uomo urlante", "Creatura piumata"),
    which the extraction writes in the table's own casing.
    """
    words = str(name or "").split()
    return any(
        len(word) > 1 and word[:1].isupper() and word.lower() not in _FILLER_TOKENS
        for word in words[1:]
    )


def is_narrator_name(name: str) -> bool:
    """True for out-of-world speaker names (the DM / narrator)."""
    return _normalize(name or "") in _NARRATOR_NAMES


# --------------------------------------------------------------------------
# existing-page matching (cross-session dedupe)
# --------------------------------------------------------------------------

def _names_ratio(a: str, b: str) -> float:
    """0..1 similarity between two names' content tokens."""
    ta, tb = sorted(_content_tokens(a)), sorted(_content_tokens(b))
    if not ta or not tb:
        return 0.0
    return SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()


def _is_near_match(a: str, b: str) -> bool:
    """Fuzzy same-entity test: high ratio OR clean token containment."""
    ta, tb = _content_tokens(a), _content_tokens(b)
    if not ta or not tb:
        return False
    if ta <= tb or tb <= ta:
        return True
    return _names_ratio(a, b) >= NEAR_MATCH_RATIO


def known_names_for_page(page: dict[str, Any]) -> list[str]:
    """All names an existing wiki page answers to (title + stored aliases)."""
    names = [page.get("title") or ""]
    aliases = page.get("aliases")
    if aliases is None:  # raw content_json payload instead of the flat view
        content = page.get("content_json") or {}
        raw = content.get("aliases")
        aliases = raw if isinstance(raw, list) else []
    names.extend(a for a in aliases if isinstance(a, str))
    return [n for n in names if n]


def match_existing_page(
    title: str,
    aliases: list[str],
    existing_pages: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Match a freshly extracted entity against the campaign's pages.

    Returns (exact_page, near_page); at most one of the two is set. An exact
    match wins (no new draft at all); otherwise the best near match (if any)
    lets the caller propose a 'possible_duplicate' relation.
    """
    if not existing_pages:
        return None, None

    candidates = [title, *(a for a in aliases if a)]

    def _matches(names: list[str], predicate) -> bool:
        return any(predicate(c, n) for c in candidates for n in names)

    for page in existing_pages:
        if (page.get("status") or "") == "archived":
            continue
        names = known_names_for_page(page)
        if _matches(names, lambda c, n: _content_tokens(c) == _content_tokens(n)):
            return page, None

    near: tuple[float, dict[str, Any]] | None = None
    for page in existing_pages:
        if (page.get("status") or "") == "archived":
            continue
        names = known_names_for_page(page)
        if _matches(names, _is_near_match):
            best = max(
                (_names_ratio(c, n) for c in candidates for n in names),
                default=0.0,
            )
            if near is None or best > near[0]:
                near = (best, page)
    return None, (near[1] if near else None)


# --------------------------------------------------------------------------
# cross-chunk merge
# --------------------------------------------------------------------------

def _uniq(items: list[str]) -> list[str]:
    """Order-preserving dedupe of non-empty strings."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = item.strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def _strings(value: Any) -> list[str]:
    """Coerce an untrusted JSON value into a clean list of strings."""
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _as_relationships(value: Any) -> dict[str, list[str]]:
    """Coerce an untrusted 'relationships' object into {type: [names]}."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[str]] = {}
    for rel_type, names in value.items():
        if not isinstance(rel_type, str) or not rel_type.strip():
            continue
        out[rel_type.strip()] = [
            n.strip()
            for n in (names if isinstance(names, list) else [])
            if isinstance(n, str) and n.strip()
        ]
    return out


def _longest(candidates: list[str]) -> str:
    """Most detailed (longest) non-empty candidate; '' if none."""
    non_empty = [c.strip() for c in candidates if c and c.strip()]
    return max(non_empty, key=len, default="")


def _entity_confidence(appearances: int, total_chunks: int) -> float:
    """0..1: how consistently the entity appeared across chunks."""
    if total_chunks <= 0:
        return 1.0
    return round(appearances / total_chunks, 3)


def _new_bucket(name: str) -> dict[str, Any]:
    """The empty accumulator for one entity: every field any chunk may set."""
    return {
        "name": name,
        "aliases": [],
        "description": "",
        # v4 character page sections (appearance / temperament)
        "physical_look": "",
        "personality": "",
        # v5 static info fields (race/class/gender/height/weight/age)
        # and durable relationships: type -> [proper names]
        "race": "",
        "class": "",
        "gender": "",
        "height": "",
        "weight": "",
        "age": "",
        "relationships": {},
        "facts": [],
        "session_facts": [],
        # v3 category hints (characters: is_party; locations:
        # place_type/part_of) - other kinds simply never set them
        "is_party": False,
        "place_type": "",
        "part_of": "",
        # v8 location page structure: narrative 'history' section +
        # type-specific detail fields (characters never set them)
        "history": "",
        "founded": "",
        "population": "",
        "government": "",
        "ruler": "",
        "demographics": "",
        "economy": "",
        "defenses": "",
        "religion": "",
        "terrain": "",
        "climate": "",
        "capital": "",
        "pantheon": "",
        "planes": "",
        "owner": "",
        "purpose": "",
        "entrance": "",
        "levels": "",
        "hazards": "",
        "flora_fauna": "",
        "districts": [],
        "notable_locations": [],
        "mentions": 0,
        "appearances": 0,
    }


def _absorb(bucket: dict[str, Any], item: dict[str, Any]) -> None:
    """Fold one extraction item (or one whole bucket) into an accumulator.

    One set of rules for both callers: the per-chunk accumulation and the
    cross-chunk unification below, so an entity that is folded twice cannot end
    up merged by different rules than the ones that built it.
    """
    bucket["appearances"] += 1
    if item.get("is_party"):
        bucket["is_party"] = True
    bucket["mentions"] += int(item.get("mentions") or 0)
    bucket["aliases"].extend(a for a in item.get("aliases", []) if isinstance(a, str))
    if _normalize(item.get("description", "")) != _normalize(bucket["description"]):
        # longest description wins; keep the first non-empty otherwise
        candidates = [bucket["description"], item.get("description", "")]
        chosen = _longest(candidates)
        bucket["description"] = chosen or bucket["description"]
    for hint in ("physical_look", "personality", "place_type", "part_of"):
        value = (item.get(hint) or "").strip()
        if value:
            bucket[hint] = _longest([bucket.get(hint, ""), value]) or bucket[hint]
    # v5 static info: longest non-empty value wins (chunks agree in
    # practice; a conflict surfaces for DM review like facts do)
    for attr in ("race", "class", "gender", "height", "weight", "age"):
        value = (item.get(attr) or "").strip()
        if value:
            bucket[attr] = _longest([bucket.get(attr, ""), value]) or bucket[attr]
    # v8 location detail fields (scalars: longest wins; lists: union,
    # deduped below). Characters never set them, so the buckets stay 0/[].
    for attr in (
        "history", "founded", "population", "government", "ruler",
        "demographics", "economy", "defenses", "religion", "terrain",
        "climate", "capital", "pantheon", "planes", "owner", "purpose",
        "entrance", "levels", "hazards", "flora_fauna",
    ):
        value = (item.get(attr) or "").strip()
        if value:
            bucket[attr] = _longest([bucket.get(attr, ""), value]) or bucket[attr]
    for list_attr in ("districts", "notable_locations"):
        bucket[list_attr].extend(
            v for v in item.get(list_attr, []) if isinstance(v, str)
        )
    # v5 durable relationships: union across chunks per type
    for rel_type, names in _as_relationships(item.get("relationships")).items():
        bucket["relationships"].setdefault(rel_type, []).extend(names)
    bucket["facts"].extend(f for f in item.get("facts", []) if isinstance(f, str))
    bucket["session_facts"].extend(
        f for f in item.get("session_facts", []) if isinstance(f, str)
    )


def _times_written(corpus: str, name: str) -> int:
    """Whole-word occurrences of a name in the recording's own text."""
    if not corpus or not name:
        return 0
    return len(re.findall(rf"(?<!\w){re.escape(name)}(?!\w)", corpus, re.IGNORECASE))


def _standing(bucket: dict[str, Any], corpus: str = "") -> tuple[int, int, int, int]:
    """How much the session backs a name: (in the text, mentions, chunks, length).

    Used to pick the name that SURVIVES a fold. The first component is the one
    that decides it: how often the recording itself writes that spelling. The
    model's own 'mentions' count is self-reported and noisy - on a real session it
    had "Jace" (one line of the transcript) outweigh "Jackie" (seven), and the
    fold then kept the mis-hearing and dropped the name the table actually used.
    """
    return (
        _times_written(corpus, str(bucket.get("name") or "")),
        int(bucket.get("mentions") or 0),
        int(bucket.get("appearances") or 0),
        len(str(bucket.get("name") or "")),
    )


def _is_token_prefix(shorter: str, longer: str) -> bool:
    """Whether 'shorter' is the leading part of 'longer', token by token.

    This is how a short name and a full name meet: "Hann" / "Hann Caleto",
    "Shiran" / "Shiran Konno". A SUFFIX or a middle token is a different thing,
    and the difference is measured, not stylistic - of the pairs a real session
    produced, arbitrary containment folded these:

        "Sceriffo" -> "Vice sceriffo"          (two different officers)
        "Hyman"    -> "Letho Hyman Feulner"    (an NPC into a player character)

    Both are wrong, and both are excluded by requiring the SHORT name to come
    first.
    """
    short, long = _tokens(shorter), _tokens(longer)
    return bool(short) and len(short) < len(long) and long[: len(short)] == short


def _shares_opening(a: str, b: str, width: int = 2) -> bool:
    """Whether two names open with the same characters (case-insensitive)."""
    left, right = _normalize(a), _normalize(b)
    return len(left) >= width and len(right) >= width and left[:width] == right[:width]


def _same_entity(a: dict[str, Any], b: dict[str, Any], *, kind: str) -> bool:
    """Whether two merged entities are one being under two names.

    Signals, strongest first:

    * one name is already an ALIAS of the other - the extraction said so ("Sir
      Lucius" / aliases ["Sir Rushus"]) - AND the two names have a token in
      common;
    * (characters only) one name is the TOKEN PREFIX of the other, which is how
      a short name and a full one meet ("Hann" / "Hann Caleto");
    * the names are near-identical strings - the cross-session page ratio, or
      (characters only) a looser one that additionally requires the two names to
      OPEN alike, which is what a mis-heard name looks like ("Jackie" / "Jace",
      both starting "ja", versus "Sceriffo" / "vicesceriffo", which do not).

    THE TOKEN REQUIREMENT ON THE ALIAS RULE IS MEASURED, NOT TIDINESS. The alias
    lists are written by the model, and a model that mis-hears a scene writes
    whatever it inferred: one real run produced a character named "Hann" carrying
    aliases ["Sir Lucius", "Sir Ruscio", "Galgith", "il nano dottore"], and the
    fold - which trusts the alias - merged the researcher into Hann. Every beat
    about Sir Lucius was then rewritten as Hann, and the draft said "Hann entra
    nell'ospedale e trova Hann". Requiring the two names to share a token keeps
    the case the rule exists for (Sir Rushus / Sir Lucius share "sir") and refuses
    the ones where the model attached an unrelated person's name.

    Token containment is NOT applied to locations: "Fatumastra" is contained in
    "Ospedale di Fatumastra" and the two are a city and a building inside it -
    that relation is 'part_of', not identity.
    """
    if _normalize(a.get("name") or "") == _normalize(b.get("name") or ""):
        return True
    name_a, name_b = str(a.get("name") or ""), str(b.get("name") or "")
    tokens_a, tokens_b = _content_tokens(name_a), _content_tokens(name_b)
    if not tokens_a or not tokens_b:
        return False
    aliases_a = {_normalize(x) for x in a.get("aliases") or []}
    aliases_b = {_normalize(x) for x in b.get("aliases") or []}
    if (
        _normalize(name_b) in aliases_a or _normalize(name_a) in aliases_b
    ) and tokens_a & tokens_b:
        return True
    # An alias the two names have nothing in common with is NOT a refusal: the
    # names may still be one being by spelling alone ("Jackie" / "Jace"), which the
    # rules below decide.
    if tokens_a == tokens_b:
        return True
    if kind != "character":
        return _names_ratio(name_a, name_b) >= NEAR_MATCH_RATIO
    if _is_token_prefix(name_a, name_b) or _is_token_prefix(name_b, name_a):
        return True
    return (
        _names_ratio(name_a, name_b) >= CHARACTER_NEAR_MATCH_RATIO
        and _shares_opening(name_a, name_b)
    )


def _unify_entities(
    buckets: list[dict[str, Any]], *, kind: str, corpus: str = ""
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Fold the same being under several names into ONE entity.

    The per-chunk merge groups on the EXACT normalized name, and the chunks
    cannot see each other, so a transcription that hears one NPC's name three
    ways produced three characters - and the summary called him by two of them
    in two consecutive paragraphs:

        "Hann entra nell'ospedale e incontra Sir Rushus..."   (chunk 4)
        "Hann Caleto entra nell'ospedale e trova Sir Lucius..."  (chunk 3)

    Both lines are faithful to the recording: the transcriber really wrote
    "Sir Rushus" once and "Sir Lucius" nine times. Faithfulness to a mis-hearing
    is not the goal - ONE name for one being is, and this is the first place in
    the pipeline that can decide it.

    Returns (buckets, renames), where 'renames' maps every dropped spelling onto
    the name that survived, so the beats and the timeline can be rewritten to
    match (callers: merge_extractions, _rename_text).
    """
    kept: list[dict[str, Any]] = []
    renames: dict[str, str] = {}
    for bucket in sorted(buckets, key=lambda b: _standing(b, corpus), reverse=True):
        target = next((k for k in kept if _same_entity(k, bucket, kind=kind)), None)
        if target is None:
            kept.append(bucket)
            continue
        dropped = str(bucket.get("name") or "")
        _absorb(target, bucket)
        if dropped and _normalize(dropped) != _normalize(target["name"]):
            renames[dropped] = str(target["name"])
        for alias in bucket.get("aliases") or []:
            if _normalize(str(alias)) != _normalize(target["name"]):
                renames.setdefault(str(alias), str(target["name"]))
    return kept, renames


def _rename_text(text: str, renames: dict[str, str]) -> str:
    """Rewrite every dropped spelling of a folded name to the one kept.

    The beats and the timeline were written by chunks that could not know the
    other spelling existed, so they are rewritten here rather than left to the
    composer to reconcile. Longest names are shielded first: replacing "Hann"
    inside "Hann Caleto" would otherwise produce "Hann Caleto Caleto".
    """
    if not text or not renames:
        return text
    result = text
    shields: dict[str, str] = {}
    for index, canonical in enumerate(
        sorted(set(renames.values()), key=len, reverse=True)
    ):
        pattern = re.compile(rf"(?<!\w){re.escape(canonical)}(?!\w)", re.IGNORECASE)
        if pattern.search(result):
            token = f"\x00{index}\x00"
            result = pattern.sub(token, result)
            shields[token] = canonical
    for dropped, canonical in sorted(renames.items(), key=lambda kv: -len(kv[0])):
        result = re.sub(
            rf"(?<!\w){re.escape(dropped)}(?!\w)", canonical, result, flags=re.IGNORECASE
        )
    for token, canonical in shields.items():
        result = result.replace(token, canonical)
    return result


def _merge_entities(
    items: list[dict[str, Any]],
    total_chunks: int,
    *,
    kind: str = "character",
    corpus: str = "",
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Merge per-chunk entities of one kind (characters or locations)."""
    by_key: dict[str, dict[str, Any]] = {}

    for item in items:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        bucket = by_key.setdefault(_normalize(name), _new_bucket(name))
        _absorb(bucket, item)

    buckets, renames = _unify_entities(list(by_key.values()), kind=kind, corpus=corpus)

    merged = []
    for bucket in buckets:
        bucket["aliases"] = _uniq(bucket["aliases"])
        bucket["facts"] = _uniq(bucket["facts"])
        bucket["session_facts"] = _uniq(bucket["session_facts"])
        bucket["districts"] = _uniq(bucket["districts"])
        bucket["notable_locations"] = _uniq(bucket["notable_locations"])
        bucket["relationships"] = {
            rel_type: _uniq(names)
            for rel_type, names in bucket.get("relationships", {}).items()
            if names
        }
        bucket["confidence"] = _entity_confidence(bucket["appearances"], total_chunks)
        # drop the internal counter from the public shape
        bucket.pop("appearances")
        merged.append(bucket)
    # most-mentioned first, then name
    merged.sort(key=lambda e: (-e["mentions"], _normalize(e["name"])))
    return merged, renames


def _majority_language(extractions: list[dict[str, Any]]) -> str:
    """Most frequent per-chunk 'language' code ('' when none was reported)."""
    counts: dict[str, int] = {}
    for extraction in extractions:
        code = (extraction.get("language") or "").strip().lower()
        if code:
            counts[code] = counts.get(code, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _parse_time(time: str) -> int:
    """'HH:MM:SS' or 'MM:SS' -> seconds (invalid -> max int so it sorts last)."""
    parts = time.strip().split(":")
    try:
        nums = [int(p) for p in parts]
        if len(nums) == 3:
            return nums[0] * 3600 + nums[1] * 60 + nums[2]
        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
        if len(nums) == 1:
            return nums[0]
    except ValueError:
        pass
    return 2**31 - 1


#: Safety cap on the merged session summary: overlapping chunks can repeat a
#: beat and a runaway model can emit dozens of lines per chunk. Since v17 the
#: prompt asks a chunk for a number of beats proportional to its part, so a real
#: session lands in the twenties or thirties - still far below this.
MAX_SUMMARY_LINES = 60


def summary_beats(
    summaries: list[str], owned: list[OwnedPart] | None = None
) -> list[dict[str, str]]:
    """The merged beats, each carrying the span of the session it came from.

    v11: the summary is the REVIEWABLE layer of the pipeline, so it is one beat
    per line. Overlapping chunks repeat beats, hence the de-duplication by
    normalized text; the surviving line keeps the first spelling seen.

    v18: a beat also carries the span its chunk NARRATED (chunking.OwnedPart).
    That is the half the merger cannot supply on its own. It de-duplicates on
    identical text, so two calls describing one scene in different words both
    survive, and the composer - which is the only pass that sees every beat at
    once - had no way to tell those two lines from two real moments. The spans
    never overlap across chunks, so "different spans" means "written by readings
    that could not see each other", which is exactly the condition under which a
    scene on a chunk boundary gets recorded twice.
    """
    parts = list(owned or [])
    beats: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, summary in enumerate(summaries):
        span = parts[index] if index < len(parts) else None
        for raw in (summary or "").splitlines():
            text = raw.strip().lstrip("-*•").strip()
            if not text:
                continue
            key = _normalize(text)
            if key in seen:
                continue
            seen.add(key)
            beats.append(
                {
                    "text": text,
                    "from": span.start if span else "",
                    "to": span.end if span else "",
                }
            )
    return beats[:MAX_SUMMARY_LINES]


def merge_summary_lines(summaries: list[str]) -> str:
    """The whole-session summary as one beat per line, in chunk order."""
    return "\n".join(beat["text"] for beat in summary_beats(summaries))


def merge_extractions(
    extractions: list[dict[str, Any]],
    *,
    owned: list[OwnedPart] | None = None,
    corpus: str = "",
) -> dict[str, Any]:
    """Combine per-chunk extractions into one structured result.

    'owned' carries one OwnedPart per chunk, in chunk order, so every merged
    beat can be tagged with the span of the session it came from (summary_beats).

    'corpus' is the session's own text (the lines the chunks were given). When two
    chunks name one being two ways, the spelling the recording uses MORE OFTEN is
    the one that survives the unification: the alternative is trusting the
    per-chunk 'mentions' counts, which are the model's own estimate and were
    measured keeping a one-off mis-hearing over the name used seven times.
    """
    total_chunks = max(1, len(extractions))

    characters: list[dict[str, Any]] = []
    locations: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    timelines: list[dict[str, Any]] = []
    summaries: list[str] = []

    for extraction in extractions:
        characters.extend(extraction.get("characters", []))
        locations.extend(extraction.get("locations", []))
        events.extend(extraction.get("events", []))
        timelines.extend(extraction.get("timeline_entries", []))
        # Appended even when empty: this list is INDEX-ALIGNED with 'owned', so
        # dropping a chunk that reported no beats would shift every later chunk's
        # span onto the wrong beats.
        summaries.append((extraction.get("session_summary") or "").strip())

    merged_characters, character_renames = _merge_entities(
        characters, total_chunks, kind="character", corpus=corpus
    )
    merged_locations, location_renames = _merge_entities(
        locations, total_chunks, kind="location", corpus=corpus
    )
    # Every spelling the unification folded away, so the beats and the timeline
    # say one name for one being (see _unify_entities).
    renames = {**character_renames, **location_renames}

    # Cross-session quality nets: (1) chunk-referencing filler text is
    # blanked/dropped, (2) the narrator (Dungeon Master / DM / Narratore) is
    # never a character, (3) whole-generic names never become pages, and
    # (4) generically-named locations are renamed with their named anchor
    # ("Ospedale" -> "Ospedale di Fatumastra") or dropped without one.
    for character in merged_characters:
        _blank_filler(character, ("description", "physical_look", "personality"))
        character["facts"] = [f for f in character.get("facts", []) if not is_filler_text(f)]
        character["session_facts"] = [
            f for f in character.get("session_facts", []) if not is_filler_text(f)
        ]
    for location in merged_locations:
        _blank_filler(location, ("description",))
        location["facts"] = [f for f in location.get("facts", []) if not is_filler_text(f)]
        location["session_facts"] = [
            f for f in location.get("session_facts", []) if not is_filler_text(f)
        ]
    merged_characters = [
        c for c in merged_characters
        if not is_generic_name(c["name"], "character")
        and not is_narrator_name(c["name"])
    ]
    language = _majority_language(extractions)
    merged_locations = _finalize_locations(merged_locations, language)

    # events: keyed by normalized title (world-significant, page-backed)
    event_by_key: dict[str, dict[str, Any]] = {}
    for event in events:
        title = (event.get("title") or "").strip()
        if not title:
            continue
        key = _normalize(title)
        bucket = event_by_key.setdefault(
            key,
            {
                "title": title,
                "description": "",
                "participants": [],
                "in_world_date": "",
                "event_type": "",
                "appearances": 0,
            },
        )
        bucket["appearances"] += 1
        candidates = [bucket["description"], event.get("description", "")]
        bucket["description"] = _longest(candidates) or bucket["description"]
        bucket["participants"].extend(p for p in event.get("participants", []) if p.strip())
        for attr in ("in_world_date", "event_type"):
            value = (event.get(attr) or "").strip()
            if value:
                bucket[attr] = _longest([bucket.get(attr, ""), value]) or bucket[attr]
    merged_events = []
    for key, bucket in event_by_key.items():
        bucket["participants"] = _uniq(bucket["participants"])
        bucket["confidence"] = _entity_confidence(bucket["appearances"], total_chunks)
        bucket.pop("appearances")
        merged_events.append(bucket)
    merged_events.sort(key=lambda e: _normalize(e["title"]))

    # timeline: dedupe by (time, summary) and sort chronologically
    seen_timeline: set[tuple[str, str]] = set()
    merged_timeline: list[dict[str, Any]] = []
    for entry in timelines:
        time = (entry.get("time") or "").strip()
        summary = (entry.get("summary") or "").strip()
        if not time or not summary:
            continue
        if (time, _normalize(summary)) in seen_timeline:
            continue
        seen_timeline.add((time, _normalize(summary)))
        merged_timeline.append(
            {
                "time": time,
                "summary": summary,
                "characters": _uniq(entry.get("characters", [])),
            }
        )
    merged_timeline.sort(key=lambda e: _parse_time(e["time"]))

    entity_pages = merged_characters + merged_locations + merged_events
    if entity_pages:
        overall = round(
            sum(e["confidence"] for e in entity_pages) / len(entity_pages), 3
        )
    else:
        overall = 0.5

    if renames:
        for entry in merged_timeline:
            entry["summary"] = _rename_text(entry["summary"], renames)
        for event in merged_events:
            event["description"] = _rename_text(event["description"], renames)
            event["participants"] = _uniq(
                [_rename_text(str(p), renames) for p in event["participants"]]
            )
    beats = summary_beats(summaries, owned)
    if renames:
        for beat in beats:
            beat["text"] = _rename_text(beat["text"], renames)
    return {
        "language": language,
        "session_summary": "\n".join(beat["text"] for beat in beats),
        # The same beats with their spans: what the compose call reads so it can
        # tell a moment recorded twice from two moments. Not persisted - it is
        # scaffolding for one call, not part of the session record.
        "summary_beats": beats,
        "characters": merged_characters,
        "locations": merged_locations,
        "events": merged_events,
        "timeline_entries": merged_timeline,
        "confidence": overall,
    }


# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# draft building
# --------------------------------------------------------------------------

#: Free-text place kinds (as spoken) -> wiki location attribute enum values.
#: Unmapped values are simply omitted from the attributes - never invented.
_LOCATION_TYPE_MAP = {
    "city": "city", "citta": "city", "citt\u00e0": "city",
    "town": "town", "borgata": "town",
    "village": "village", "paese": "village", "villaggio": "village",
    "hamlet": "village", "borgo": "village",
    "region": "region", "regione": "region", "province": "region",
    "provincia": "region", "duchy": "region", "ducato": "region",
    "kingdom": "region", "regno": "region",
    "building": "building", "inn": "building", "tavern": "building",
    "osteria": "building", "taverna": "building", "locanda": "building",
    "castle": "building", "castello": "building", "tower": "building",
    "torre": "building", "temple": "building", "tempio": "building",
    "church": "building", "chiesa": "building", "keep": "building",
    "dungeon": "dungeon", "crypt": "dungeon", "cripta": "dungeon",
    "mine": "dungeon", "miniera": "dungeon",
    "wilderness": "wilderness", "forest": "wilderness", "foresta": "wilderness",
    "bosco": "wilderness", "swamp": "wilderness", "palude": "wilderness",
    "mountains": "wilderness", "montagna": "wilderness",
    "continent": "continent", "continente": "continent",
    "world": "world", "mondo": "world", "piano": "world",
    "structure": "structure", "struttura": "structure", "edificio": "structure",
    "metropolis": "city", "metropoli": "city",
}


def map_location_type(raw: str) -> str | None:
    """Free-text place kind -> one of the wiki location_type enum values.

    Case-insensitive on the exact enum words plus a small EN/IT alias table;
    anything else maps to None (the attribute is omitted - the LLM's wording
    survives in the page facts anyway).
    """
    value = (raw or "").strip().lower()
    if not value:
        return None
    if value in _LOCATION_TYPE_MAP:
        return _LOCATION_TYPE_MAP[value]
    folded = (
        value.replace("\u00e0", "a").replace("\u00e8", "e").replace("\u00e9", "e")
        .replace("\u00ec", "i").replace("\u00f2", "o").replace("\u00f9", "u")
    )
    return _LOCATION_TYPE_MAP.get(folded)

#: Display order of the type-specific location attribute fields (used by
#: _location_attributes so drafts carry a stable, readable key order).
_LOCATION_ATTRIBUTE_FIELD_ORDER = (
    "founded",
    "population", "government", "ruler", "demographics", "economy",
    "defenses", "religion", "districts", "notable_locations",
    "capital", "terrain", "climate",
    "pantheon", "planes",
    "owner", "purpose",
    "entrance", "levels", "hazards",
    "flora_fauna",
)

#: location attribute key -> the location types it is valid for. Mirrors the
#: wiki-service schema (app/page_attributes.py): the merger must never emit a
#: field the wiki-service would reject for the mapped location_type.
_LOCATION_ATTRIBUTE_ALLOWED: dict[str, set[str]] = {
    "city": {"founded", "population", "government", "ruler", "demographics",
             "economy", "defenses", "religion", "districts", "notable_locations"},
    "town": {"founded", "population", "government", "ruler", "demographics",
             "economy", "defenses", "religion", "districts", "notable_locations"},
    "village": {"founded", "population", "government", "ruler", "demographics",
                "economy", "defenses", "religion", "notable_locations"},
    "region": {"founded", "government", "ruler", "capital", "terrain",
               "climate", "notable_locations"},
    "continent": {"founded", "government", "ruler", "capital", "terrain",
                  "climate", "notable_locations"},
    "world": {"founded", "terrain", "climate", "planes", "pantheon",
              "notable_locations"},
    "building": {"founded", "owner", "purpose"},
    "structure": {"founded", "owner", "purpose"},
    "dungeon": {"founded", "entrance", "levels", "hazards"},
    "wilderness": {"founded", "terrain", "climate", "hazards", "flora_fauna",
                   "notable_locations"},
    "other": {"founded"},
}


def _location_attributes(location: dict[str, Any]) -> dict[str, Any]:
    """Structured attributes for a location draft.

    Only fields valid for the mapped location_type are emitted (mirrors
    wiki-service app/page_attributes.py), so a draft can never be rejected by
    the wiki-service schema. The common fields (region from 'part_of',
    founded) are emitted whenever present; unknown place kinds only keep the
    region hint.
    """
    attributes: dict[str, Any] = {}
    location_type = map_location_type(location.get("place_type") or "")
    region = (location.get("part_of") or "").strip()
    if location_type is not None:
        attributes["location_type"] = location_type
    if region:
        attributes["region"] = region
    if location_type is None:
        return attributes
    allowed = _LOCATION_ATTRIBUTE_ALLOWED.get(location_type, set())
    for field in _LOCATION_ATTRIBUTE_FIELD_ORDER:
        if field not in allowed:
            continue
        value = location.get(field)
        if isinstance(value, list):
            cleaned = [v.strip() for v in value if isinstance(v, str) and v.strip()]
            if cleaned:
                attributes[field] = cleaned
        elif isinstance(value, str) and value.strip():
            attributes[field] = value.strip()
    return attributes


# --------------------------------------------------------------------------
# cross-session wiki-quality guards
# --------------------------------------------------------------------------

#: Phrases that reference the extraction context instead of the world. The
#: wiki is cross-session: any text mentioning the chunk / fragment / session
#: / episode / transcription is junk and gets blanked or dropped.
_CHUNK_REFERENCE_RE = re.compile(
    r"\bin questo frammento\b"
    r"|\bnel frammento\b"
    r"|\bdel frammento\b"
    r"|\bin questa sessione\b"
    r"|\bnella sessione\b"
    r"|\bin questo episodio\b"
    r"|\bin questo chunk\b"
    r"|\bdella trascrizione\b"
    r"|\bin this fragment\b"
    r"|\bin the fragment\b"
    r"|\bof the fragment\b"
    r"|\bin this session\b"
    r"|\bin the session\b"
    r"|\bin this episode\b"
    r"|\bin this chunk\b"
    r"|\bin the chunk\b"
    r"|\bof the transcript\b",
    re.IGNORECASE,
)

#: Whole-text filler ("Membro del gruppo", "non interviene") — exact match,
#: so longer legitimate statements ("Non interviene mai nelle discussioni.")
#: survive; only the bare padding is treated as junk.
_FILLER_PHRASES = frozenset({
    "membro del gruppo", "membro del party", "member of the group",
    "member of the party",
    "non interviene", "non appare", "non compare", "non si sa molto",
    "non si sa nulla", "does not intervene", "does not appear",
    "doesn't appear", "not much is known", "nothing is known",
})


def is_filler_text(text: str) -> bool:
    """True when a text field is chunk-referencing or pure filler."""
    value = (text or "").strip()
    if not value:
        return True
    if _CHUNK_REFERENCE_RE.search(value):
        return True
    return _normalize(value).rstrip(".,;:!?") in _FILLER_PHRASES


def _blank_filler(entity: dict[str, Any], fields: tuple[str, ...]) -> None:
    """Blank text fields that reference the chunk or are pure filler."""
    for field in fields:
        value = (entity.get(field) or "").strip()
        if value and is_filler_text(value):
            entity[field] = ""


_LOCATION_QUALIFIER = {"it": "di", "en": "of"}


def _qualify_location_name(name: str, anchor: str, language: str) -> str:
    """'Ospedale' + 'Fatumastra' (+it) -> 'Ospedale di Fatumastra'."""
    code = ((language or "").strip().lower().split("-")[0]) or "en"
    prep = _LOCATION_QUALIFIER.get(code, "of")
    return f"{name} {prep} {anchor}"


def _finalize_locations(
    locations: list[dict[str, Any]], language: str
) -> list[dict[str, Any]]:
    """Enforce unique proper names for locations (the wiki naming rule).

    A generically-named place ("Ospedale", "Strada Maestra") is only kept
    when the extraction names the larger place containing it ('part_of'),
    and is then renamed to a unique composite: "Ospedale di Fatumastra".
    Without an anchor the name stays ambiguous and the location is dropped.
    """
    out: list[dict[str, Any]] = []
    for loc in locations:
        name = (loc.get("name") or "").strip()
        if not name:
            continue
        if not is_generic_name(name, "location"):
            out.append(loc)
            continue
        anchor = (loc.get("part_of") or "").strip()
        if anchor and not is_generic_name(anchor, "location"):
            loc["name"] = _qualify_location_name(name, anchor, language)
            # a generic alias ("l'ospedale") must not make the composite page
            # collide with an old generic-named page during cross-session dedupe
            loc["aliases"] = [
                a for a in loc.get("aliases", []) if not is_generic_name(a, "location")
            ]
            out.append(loc)
    return out


def exclude_character_names(
    merged: dict[str, Any], names: list[str]
) -> dict[str, Any]:
    """Drop characters whose name matches any of the given out-of-world names.

    Used by the worker for the campaign's actual DM speaker (resolved by user
    id), complementing the hardcoded 'is_narrator_name' net in
    merge_extractions(). Returns a new dict when anything was dropped.
    """
    blocked = {_normalize(n) for n in (names or []) if n and n.strip()}
    if not blocked:
        return merged
    kept = [
        c
        for c in merged.get("characters", [])
        if _normalize(c.get("name") or "") not in blocked
    ]
    if len(kept) == len(merged.get("characters", [])):
        return merged
    out = dict(merged)
    out["characters"] = kept
    return out


def rename_characters(
    merged: dict[str, Any], mapping: dict[str, str]
) -> dict[str, Any]:
    """Rewrite names in the story: a player's name stands for their character.

    The recording is full of the people at the table - "Giulia, tocca a te" - and a
    draft that carries the name into the narrative has named a person as an actor
    in the fiction. The roster says Giulia plays Dalia, so the ACTION belongs to
    Dalia and the prose has to say so. Returns a new dict when anything changed.

    Only the story is rewritten, not the entity fields: a character page built from
    a name the table never used for that character is a different, later problem,
    and quietly rewriting the extraction is how a wrong identity gets frozen in.
    """
    if not mapping:
        return merged
    renames = {player: character for player, character in mapping.items() if player and character}
    if not renames:
        return merged
    changed = False
    beats = []
    for beat in merged.get("summary_beats") or []:
        text = str(beat.get("text") or "")
        renamed = _rename_text(text, renames)
        if renamed != text:
            changed = True
        beats.append({**beat, "text": renamed})
    if not changed:
        return merged
    out = dict(merged)
    out["summary_beats"] = beats
    out["session_summary"] = "\n".join(beat["text"] for beat in beats)
    return out


def _content_json(
    *,
    summary: str | None = None,
    language: str | None = None,
    aliases: list[str] | None = None,
    facts: list[str] | None = None,
    session_references: list[dict[str, Any]] | None = None,
    attributes: dict[str, Any] | None = None,
    physical_look: str | None = None,
    personality: str | None = None,
    history: str | None = None,
) -> dict[str, Any]:
    """content_json for an entity draft (documented in docs/data-model.md).

    Characters drop the generic Summary block in favour of the Physical look
    and Personality sections; other kinds still use 'summary'. 'history' is a
    cross-type narrative section (locations: founding/wars/famous events).
    """
    body: dict[str, Any] = {}
    if summary:
        body["summary"] = summary
    if language:
        body["language"] = language
    if aliases:
        body["aliases"] = aliases
    if facts:
        body["facts"] = facts
    if session_references:
        body["session_references"] = session_references
    if attributes:
        body["attributes"] = attributes
    if physical_look:
        body["physical_look"] = physical_look
    if personality:
        body["personality"] = personality
    if history:
        body["history"] = history
    return body


def build_page_drafts(
    merged: dict[str, Any],
    campaign_id: str,
    session_id: str,
    existing_pages: list[dict[str, Any]] | None = None,
    party_characters: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Map a merged result to wiki PageCreate payloads + relation proposals.

    Only character and location pages are drafted here; events go through
    build_event_drafts() (event pages + timeline entries). Session recaps
    live on the dedicated session page.

    Returns (drafts, relations, duplicates):

    - drafts: PageCreate-shaped payloads. Characters always carry
      content_json.attributes.character_type ('player' when they match the
      party's character names or the model's is_party hint, 'npc' otherwise).
    - relations: {'from_title', 'to_page_id', 'relation_type'} proposals for
      fresh drafts that look like existing pages ('possible_duplicate'),
      resolved after the drafts are created.
    - duplicates: entities skipped because the campaign already documents
      them ({'title', 'kind', 'matched_page_id', 'matched_title'}). Their new
      facts stay visible on the persisted session summary (session page).

    'existing_pages' is the flat page listing from wiki-service
    (id/title/kind/status/aliases); without it no dedupe happens.
    'party_characters' lists the party's CHARACTER names (campaign members);
    matching entities are tagged as player characters.
    """
    drafts: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    language = (merged.get("language") or "").strip()
    party = {
        normalize_entity_name(name)
        for name in (party_characters or [])
        if isinstance(name, str) and name.strip()
    }

    def _is_party_character(entity: dict[str, Any]) -> bool:
        """Model hint OR name/alias matches a party member's character."""
        if entity.get("is_party"):
            return True
        candidates = [entity.get("name") or "", *(entity.get("aliases") or [])]
        return any(normalize_entity_name(c) in party for c in candidates if c)

    def _draft_payload(
        kind: str, entity: dict[str, Any], content: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "campaign_id": campaign_id,
            "kind": kind,
            "title": entity["name"],
            "content_json": content,
            "status": DRAFT_STATUS,
            "visibility": DEFAULT_VISIBILITY,
            "confidence": entity.get("confidence"),
            "source_session_id": session_id,
        }

    def _match_or_record(
        entity: dict[str, Any], kind: str, aliases: list[str]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """(skip-record, near-page) for one entity; None/None = draft it."""
        exact, near = match_existing_page(entity["name"], aliases, existing_pages)
        if exact is not None:
            return (
                {
                    "title": entity["name"],
                    "kind": kind,
                    "matched_page_id": str(exact.get("id") or ""),
                    "matched_title": exact.get("title") or "",
                },
                None,
            )
        if near is not None:
            relations.append(
                {
                    "from_title": entity["name"],
                    "to_page_id": str(near.get("id") or ""),
                    "relation_type": POSSIBLE_DUPLICATE,
                }
            )
        return None, near

    for character in merged.get("characters", []):
        aliases = [a for a in character.get("aliases", []) if isinstance(a, str)]
        duplicate, _near = _match_or_record(character, "character", aliases)
        if duplicate is not None:
            duplicates.append(duplicate)
            continue

        facts = [f for f in character.get("facts", []) if isinstance(f, str)]
        session_facts = [
            f for f in character.get("session_facts", []) if isinstance(f, str)
        ]
        session_references = (
            [{"session_id": session_id, "facts": session_facts}]
            if session_facts
            else None
        )
        # players are documented by their CHARACTER, never their player name;
        # every character page says which side of the table it belongs to.
        # Characters have no Summary section: the description feeds the
        # session summary only, while appearance/temperament land in the
        # Physical look / Personality sections of the page.
        physical_look = (character.get("physical_look") or "").strip()
        personality = (character.get("personality") or "").strip()
        attributes: dict[str, Any] = {
            "character_type": "player" if _is_party_character(character) else "npc"
        }
        # v5 auto-filled static info (race/class/gender/height/weight/age) —
        # only non-empty values reach the page's attribute table.
        for attr in ("race", "class", "gender", "height", "weight", "age"):
            value = (character.get(attr) or "").strip()
            if value:
                attributes[attr] = value
        content = _content_json(
            summary=None,
            language=language or None,
            aliases=aliases or None,
            facts=facts or None,
            session_references=session_references,
            physical_look=physical_look or None,
            personality=personality or None,
            attributes=attributes,
        )
        drafts.append(_draft_payload("character", character, content))

        # Durable relationships -> wiki relation proposals. PERSISTENT ties
        # only (the prompt enforces it, the whitelist guarantees it);
        # self-relations are dropped here.
        for rel_type, names in _as_relationships(character.get("relationships")).items():
            if rel_type not in _DURABLE_RELATION_TYPES:
                continue
            for target in names:
                if _normalize(target) == _normalize(character["name"]):
                    continue
                relations.append(
                    {
                        "from_title": character["name"],
                        "to_title": target,
                        "relation_type": rel_type,
                    }
                )

    for location in merged.get("locations", []):
        aliases = [a for a in location.get("aliases", []) if isinstance(a, str)]
        duplicate, _near = _match_or_record(location, "location", aliases)
        if duplicate is not None:
            duplicates.append(duplicate)
            continue

        facts = [f for f in location.get("facts", []) if isinstance(f, str)]
        session_facts = [
            f for f in location.get("session_facts", []) if isinstance(f, str)
        ]

        # structured attributes scoped to the mapped location_type (city ->
        # population/government/..., region -> terrain/climate/capital, ...);
        # unknown place kinds are omitted, never invented
        attributes = _location_attributes(location)

        session_references = (
            [{"session_id": session_id, "facts": session_facts}]
            if session_facts
            else None
        )
        content = _content_json(
            summary=location.get("description", ""),
            language=language or None,
            aliases=aliases or None,
            facts=facts or None,
            session_references=session_references,
            attributes=attributes or None,
            # cross-type narrative section: the place's durable past
            history=(location.get("history") or "").strip() or None,
        )
        drafts.append(_draft_payload("location", location, content))

    return drafts, relations, duplicates


def build_event_drafts(
    merged: dict[str, Any],
    campaign_id: str,
    session_id: str,
    existing_pages: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Map merged world events to event-page drafts + timeline upserts.

    Every merged event becomes its own wiki page (kind='event') and a
    campaign timeline entry (pending DM approval). Events the campaign
    already documents — an existing kind='event' page with the same title or
    a near-match — are NOT re-drafted: the new information is merged into the
    existing page and its timeline entry is refreshed.

    Returns (drafts, updates, timeline_events, duplicates):

    - drafts: PageCreate payloads (kind='event') for events
      the wiki does not document yet.
    - updates: {'page_id', 'title', 'content_json', 'change_note'} payloads
      for existing event pages: the fresh description/participants/date are
      merged in (longest description wins, lists union, missing attributes
      filled) so nothing is lost and nothing is duplicated.
    - timeline_events: {'title', 'page_id' (None when the page is one of the
      new drafts — the worker resolves it by title after creation),
      'in_world_date', 'summary', 'source_session_id'} upsert payloads for
      EVERY event (new and updated).
    - duplicates: events whose title matches a NON-event page of the campaign
      (title collides with a character/location/... page); recorded, not
      drafted, and surfaced in content.generated for the DM.
    """
    drafts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    timeline_events: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    language = (merged.get("language") or "").strip()

    event_pages = [
        page
        for page in (existing_pages or [])
        if (page.get("kind") or "") == "event"
    ]
    other_pages = [
        page
        for page in (existing_pages or [])
        if (page.get("kind") or "") != "event"
    ]

    def _merge_attributes(
        existing: dict[str, Any], event: dict[str, Any]
    ) -> dict[str, Any]:
        """Union participants, fill missing event_type/in_world_date."""
        attrs = dict(existing)
        existing_participants = attrs.get("participants")
        participants = _uniq(
            [
                *(existing_participants if isinstance(existing_participants, list) else []),
                *event.get("participants", []),
            ]
        )
        if participants:
            attrs["participants"] = participants
        for attr in ("event_type", "in_world_date"):
            value = (event.get(attr) or "").strip()
            if value and not (attrs.get(attr) or "").strip():
                attrs[attr] = value
        return attrs

    def _content(
        event: dict[str, Any], *, existing: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """content_json for an event page (or merged into the existing one)."""
        description = (event.get("description") or "").strip()
        base = dict(existing or {})
        if description:
            current = base.get("summary")
            if isinstance(current, str) and current.strip():
                base["summary"] = _longest([current, description])
            else:
                base["summary"] = description
        if language and not base.get("language"):
            base["language"] = language
        # session-scoped details stay traceable to their session
        session_references = base.get("session_references")
        if not isinstance(session_references, list):
            session_references = []
        if description:
            keys = {
                (r.get("session_id") or "")
                for r in session_references
                if isinstance(r, dict)
            }
            if session_id not in keys:
                session_references.append({"session_id": session_id, "facts": [description]})
        if session_references:
            base["session_references"] = session_references
        existing_attrs = base.get("attributes")
        attrs = _merge_attributes(
            existing_attrs if isinstance(existing_attrs, dict) else {},
            event,
        )
        if attrs:
            base["attributes"] = attrs
        return base

    for event in merged.get("events", []):
        title = (event.get("title") or "").strip()
        if not title:
            continue
        # Events dedupe against event pages only: a title collision with a
        # character/location page is ambiguous and never silently overwritten.
        exact, near = match_existing_page(title, [], event_pages)
        target = exact or near
        if target is None:
            collision = match_existing_page(title, [], other_pages)[0]
            if collision is not None:
                duplicates.append(
                    {
                        "title": title,
                        "kind": "event",
                        "matched_page_id": str(collision.get("id") or ""),
                        "matched_title": collision.get("title") or "",
                    }
                )
                continue
            content = _content(event)
            drafts.append(
                {
                    "campaign_id": campaign_id,
                    "kind": "event",
                    "title": title,
                    "content_json": content,
                    "status": DRAFT_STATUS,
                    "visibility": DEFAULT_VISIBILITY,
                    "confidence": event.get("confidence"),
                    "source_session_id": session_id,
                }
            )
            timeline_events.append(
                {
                    "title": title,
                    "page_id": None,  # resolved from the draft after creation
                    "in_world_date": (event.get("in_world_date") or "").strip() or None,
                    "summary": (event.get("description") or title).strip(),
                    "source_session_id": session_id,
                }
            )
            continue

        page_id = str(target.get("id") or "")
        merged_content = _content(event, existing=target.get("content_json"))
        updates.append(
            {
                "page_id": page_id,
                "title": title,
                "content_json": merged_content,
                "change_note": f"Auto-updated from session {session_id}",
            }
        )
        timeline_events.append(
            {
                "title": title,
                "page_id": page_id,
                "in_world_date": (event.get("in_world_date") or "").strip() or None,
                # longest-wins summary so the timeline row never regresses
                "summary": (
                    merged_content.get("summary")
                    if isinstance(merged_content.get("summary"), str)
                    and merged_content["summary"].strip()
                    else title
                ),
                "source_session_id": session_id,
            }
        )

    return drafts, updates, timeline_events, duplicates