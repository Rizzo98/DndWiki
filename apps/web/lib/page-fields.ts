// Everything the UI knows about the FIELDS of a wiki page's content_json:
// how to label a path, how to diff a page against the change set that rewrites
// it, and the per-kind field specs the review editor renders.
//
// The attribute rules here mirror services/wiki-service/app/page_attributes.py.
// That schema is strict — unknown keys are rejected (`extra="forbid"`) and a
// location field must belong to the page's location_type — so the editor
// offers exactly the keys it accepts and flags the ones a draft already
// carries that the API would refuse at apply time.

import type { PlanChange, WikiPageKind } from "./api";

// ------------------------------------------------------------------ labels

/** Human label of a flattened content path ("attributes.population"). */
const FIELD_LABELS: Record<string, string> = {
  title: "Title",
  summary: "Overview",
  physical_look: "Physical look",
  personality: "Personality",
  history: "History",
  facts: "Facts",
  session_facts: "Session facts",
  aliases: "Aliases",
  body: "Body",
  language: "Language",
  session_references: "Session references",
  image_uri: "Portrait",
  confidence: "Confidence",
  "attributes.character_type": "Type",
  "attributes.race": "Race",
  "attributes.class": "Class",
  "attributes.gender": "Gender",
  "attributes.height": "Height",
  "attributes.weight": "Weight",
  "attributes.age": "Age",
  "attributes.location_type": "Kind of place",
  "attributes.region": "Region",
  "attributes.latitude": "Latitude",
  "attributes.longitude": "Longitude",
  "attributes.founded": "Founded",
  "attributes.population": "Population",
  "attributes.government": "Government",
  "attributes.ruler": "Ruler",
  "attributes.demographics": "Demographics",
  "attributes.economy": "Economy",
  "attributes.defenses": "Defenses",
  "attributes.religion": "Religion",
  "attributes.districts": "Districts",
  "attributes.notable_locations": "Notable locations",
  "attributes.capital": "Capital",
  "attributes.terrain": "Terrain",
  "attributes.climate": "Climate",
  "attributes.pantheon": "Pantheon",
  "attributes.planes": "Planes",
  "attributes.owner": "Owner",
  "attributes.purpose": "Purpose",
  "attributes.entrance": "Entrance",
  "attributes.levels": "Levels",
  "attributes.hazards": "Hazards",
  "attributes.flora_fauna": "Flora & fauna",
  "attributes.event_type": "Event type",
  "attributes.in_world_date": "In-world date",
  "attributes.participants": "Participants",
  "attributes.event_status": "Status",
  "attributes.faction_type": "Faction type",
  "attributes.leader": "Leader",
  "attributes.headquarters": "Headquarters",
  "attributes.item_type": "Item type",
  "attributes.rarity": "Rarity",
  "attributes.quest_status": "Quest status",
  "attributes.giver": "Given by",
  "attributes.reward": "Reward",
};

export function fieldLabel(path: string): string {
  if (FIELD_LABELS[path]) return FIELD_LABELS[path];
  const last = path.split(".").pop() ?? path;
  return last.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

// ------------------------------------------------------------------ diffing

/** Flatten a content_json into "path -> leaf" pairs (arrays are leaves). */
export function flattenContent(
  value: unknown,
  prefix = "",
): Record<string, unknown> {
  if (value === null || value === undefined) return {};
  if (typeof value !== "object" || Array.isArray(value)) {
    return prefix ? { [prefix]: value } : {};
  }
  const out: Record<string, unknown> = {};
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    Object.assign(out, flattenContent(child, prefix ? prefix + "." + key : key));
  }
  return out;
}

/** Render a leaf value for display (nested objects as compact JSON). */
export function displayValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  const leaf = (item: unknown) =>
    typeof item === "object" && item !== null ? JSON.stringify(item) : String(item);
  if (Array.isArray(value)) return value.map(leaf).join(" · ");
  return leaf(value);
}

export interface FieldDiff {
  path: string;
  label: string;
  before: unknown;
  after: unknown;
}

/** The fields that differ between the current page and the proposed one. */
export function diffChange(change: PlanChange): FieldDiff[] {
  const before = flattenContent(change.before?.content_json ?? {});
  const after = flattenContent(change.after.content_json ?? {});
  const paths = Array.from(new Set([...Object.keys(before), ...Object.keys(after)]));
  const diffs: FieldDiff[] = [];
  if (change.before && change.before.title !== change.after.title) {
    diffs.push({
      path: "title",
      label: "Title",
      before: change.before.title,
      after: change.after.title,
    });
  }
  for (const path of paths) {
    const a = before[path];
    const b = after[path];
    if (JSON.stringify(a ?? null) === JSON.stringify(b ?? null)) continue;
    diffs.push({ path, label: fieldLabel(path), before: a, after: b });
  }
  return diffs;
}

// -------------------------------------------------------------- field specs

export type DraftFieldType = "prose" | "list" | "text" | "select";

export interface DraftFieldSpec {
  key: string;
  label: string;
  type: DraftFieldType;
  /** Options of a "select" field; the first one is the default. */
  options?: string[];
  /** Rendered even when the draft does not carry the key yet. */
  core?: boolean;
  /** A list whose rows are names, not prose: a single-line editor each. */
  plain?: boolean;
  /** Editor height of a prose field / list row. */
  rows?: number;
  hint?: string;
}

export interface KindFields {
  /** The narrative of the page: always offered (it is what the page IS). */
  prose: DraftFieldSpec[];
  /** Name/fact lists, one row per item. */
  lists: DraftFieldSpec[];
  /** The validated attributes object of the kind. */
  attributes: DraftFieldSpec[];
}

/** Subtypes a place may declare (mirrors wiki-service LocationType). */
export const LOCATION_TYPES = [
  "city",
  "town",
  "village",
  "region",
  "continent",
  "world",
  "building",
  "structure",
  "dungeon",
  "wilderness",
  "other",
] as const;

const FACTS: DraftFieldSpec = {
  key: "facts",
  label: "Facts",
  type: "list",
  core: true,
  rows: 2,
  hint: "One durable fact per row — type # to link a page",
};
const ALIASES: DraftFieldSpec = {
  key: "aliases",
  label: "Aliases",
  type: "list",
  core: true,
  plain: true,
  hint: "Other names this page is known by — one per row",
};

const CHARACTER_FIELDS: KindFields = {
  prose: [
    { key: "physical_look", label: "Physical look", type: "prose", rows: 4, hint: "What the character looks like" },
    { key: "personality", label: "Personality", type: "prose", rows: 4, hint: "How the character behaves" },
    { key: "history", label: "History", type: "prose", rows: 4, hint: "What happened to them before this session" },
  ],
  lists: [FACTS, ALIASES],
  attributes: [
    { key: "character_type", label: "Character type", type: "select", options: ["npc", "player"], core: true },
    { key: "race", label: "Race", type: "text", core: true },
    { key: "class", label: "Class", type: "text", core: true },
    { key: "gender", label: "Gender", type: "text" },
    { key: "height", label: "Height", type: "text" },
    { key: "weight", label: "Weight", type: "text" },
    { key: "age", label: "Age", type: "text" },
  ],
};

const LOCATION_FIELDS: KindFields = {
  prose: [
    { key: "summary", label: "Overview", type: "prose", rows: 4, hint: "What this place is, in a few lines" },
    { key: "history", label: "History", type: "prose", rows: 5, hint: "Founding, wars, famous events" },
  ],
  lists: [FACTS, ALIASES],
  attributes: [
    { key: "location_type", label: "Kind of place", type: "select", options: [...LOCATION_TYPES], core: true },
    { key: "region", label: "Region", type: "text", core: true },
    { key: "founded", label: "Founded", type: "text" },
    { key: "population", label: "Population", type: "text" },
    { key: "government", label: "Government", type: "text" },
    { key: "ruler", label: "Ruler", type: "text" },
    { key: "demographics", label: "Demographics", type: "text" },
    { key: "economy", label: "Economy", type: "text" },
    { key: "defenses", label: "Defenses", type: "text" },
    { key: "religion", label: "Religion", type: "text" },
    { key: "districts", label: "Districts", type: "list", plain: true, hint: "One per row" },
    { key: "notable_locations", label: "Notable locations", type: "list", rows: 2, hint: "One per row — type # to link a page" },
    { key: "capital", label: "Capital", type: "text" },
    { key: "terrain", label: "Terrain", type: "text" },
    { key: "climate", label: "Climate", type: "text" },
    { key: "planes", label: "Planes", type: "text" },
    { key: "pantheon", label: "Pantheon", type: "text" },
    { key: "owner", label: "Owner", type: "text" },
    { key: "purpose", label: "Purpose", type: "text" },
    { key: "entrance", label: "Entrance", type: "text" },
    { key: "levels", label: "Levels", type: "text" },
    { key: "hazards", label: "Hazards", type: "text" },
    { key: "flora_fauna", label: "Flora & fauna", type: "text" },
  ],
};

const FACTION_FIELDS: KindFields = {
  prose: [{ key: "summary", label: "Overview", type: "prose", rows: 4, hint: "What this faction is" }],
  lists: [FACTS, ALIASES],
  attributes: [
    { key: "faction_type", label: "Faction type", type: "select", options: ["guild", "order", "government", "military", "criminal", "religious", "other"], core: true },
    { key: "leader", label: "Leader", type: "text" },
    { key: "headquarters", label: "Headquarters", type: "text" },
  ],
};

const ITEM_FIELDS: KindFields = {
  prose: [{ key: "summary", label: "Overview", type: "prose", rows: 4, hint: "What this object is" }],
  lists: [FACTS, ALIASES],
  attributes: [
    { key: "item_type", label: "Item type", type: "select", options: ["weapon", "armor", "potion", "artifact", "wondrous", "trinket", "other"], core: true },
    { key: "rarity", label: "Rarity", type: "select", options: ["common", "uncommon", "rare", "very_rare", "legendary", "artifact"] },
    { key: "owner", label: "Owner", type: "text" },
  ],
};

const QUEST_FIELDS: KindFields = {
  prose: [{ key: "summary", label: "Overview", type: "prose", rows: 4, hint: "What the party was asked to do" }],
  lists: [FACTS, ALIASES],
  attributes: [
    { key: "quest_status", label: "Quest status", type: "select", options: ["open", "in_progress", "completed", "failed"], core: true },
    { key: "giver", label: "Given by", type: "text" },
    { key: "reward", label: "Reward", type: "text" },
  ],
};

const EVENT_FIELDS: KindFields = {
  prose: [
    { key: "summary", label: "What happened", type: "prose", rows: 5, hint: "The event as the timeline tells it" },
    { key: "history", label: "History", type: "prose", rows: 4 },
  ],
  lists: [FACTS, ALIASES],
  attributes: [
    { key: "event_type", label: "Event type", type: "select", options: ["battle", "negotiation", "discovery", "quest", "catastrophe", "political", "other"], core: true },
    { key: "in_world_date", label: "In-world date", type: "text", core: true },
    { key: "event_status", label: "Status", type: "select", options: ["ongoing", "resolved", "unknown"] },
    { key: "participants", label: "Participants", type: "list", rows: 2, hint: "One per row — type # to link a page" },
  ],
};

/** Kinds a DM may create by hand, and anything the pipeline invents later. */
const GENERIC_FIELDS: KindFields = {
  prose: [
    { key: "body", label: "Body", type: "prose", rows: 6 },
    { key: "summary", label: "Overview", type: "prose", rows: 4 },
    { key: "history", label: "History", type: "prose", rows: 4 },
  ],
  lists: [FACTS, ALIASES],
  attributes: [],
};

const KIND_FIELDS: Record<WikiPageKind, KindFields> = {
  character: CHARACTER_FIELDS,
  location: LOCATION_FIELDS,
  faction: FACTION_FIELDS,
  item: ITEM_FIELDS,
  quest: QUEST_FIELDS,
  event: EVENT_FIELDS,
};

export function kindFields(kind: string): KindFields {
  return KIND_FIELDS[kind as WikiPageKind] ?? GENERIC_FIELDS;
}

/** The value a freshly added field starts from. */
export function emptyValueFor(spec: DraftFieldSpec): unknown {
  if (spec.type === "list") return [];
  if (spec.type === "select") return spec.options?.[0] ?? "";
  return "";
}

// -------------------------------------------------------- attribute schemas

const COMMON_LOCATION_FIELDS = ["location_type", "region", "latitude", "longitude", "founded"];
const SETTLEMENT_LOCATION_FIELDS = ["population", "government", "ruler", "demographics", "economy", "defenses", "religion", "districts", "notable_locations"];
const REGION_LOCATION_FIELDS = ["government", "ruler", "capital", "terrain", "climate", "notable_locations"];
const WORLD_LOCATION_FIELDS = ["terrain", "climate", "planes", "pantheon", "notable_locations"];
const BUILDING_LOCATION_FIELDS = ["owner", "purpose"];
const DUNGEON_LOCATION_FIELDS = ["entrance", "levels", "hazards"];
const WILDERNESS_LOCATION_FIELDS = ["terrain", "climate", "hazards", "flora_fauna", "notable_locations"];

/** location_type -> the extra fields the API accepts for it. */
const LOCATION_TYPE_FIELDS: Record<string, string[]> = {
  city: SETTLEMENT_LOCATION_FIELDS,
  town: SETTLEMENT_LOCATION_FIELDS,
  village: SETTLEMENT_LOCATION_FIELDS,
  region: REGION_LOCATION_FIELDS,
  continent: REGION_LOCATION_FIELDS,
  world: WORLD_LOCATION_FIELDS,
  building: BUILDING_LOCATION_FIELDS,
  structure: BUILDING_LOCATION_FIELDS,
  dungeon: DUNGEON_LOCATION_FIELDS,
  wilderness: WILDERNESS_LOCATION_FIELDS,
  other: [],
};

const KIND_ATTRIBUTE_KEYS: Record<WikiPageKind, string[]> = {
  character: ["character_type", "race", "class", "gender", "height", "weight", "age"],
  location: [], // type-scoped, see attributeKeysFor
  faction: ["faction_type", "leader", "headquarters"],
  item: ["item_type", "rarity", "owner"],
  quest: ["quest_status", "giver", "reward"],
  event: ["event_type", "in_world_date", "participants", "event_status"],
};

function attributesOf(content: Record<string, unknown>): Record<string, unknown> {
  const value = content.attributes;
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

/** The location_type a location page claims ("other" when it names none). */
export function locationTypeOf(content: Record<string, unknown>): string {
  const value = attributesOf(content).location_type;
  return typeof value === "string" && value ? value : "other";
}

/**
 * The attribute keys the API accepts for this draft, or null when the kind has
 * no schema (a future kind: anything goes).
 */
export function attributeKeysFor(
  kind: string,
  content: Record<string, unknown>,
): Set<string> | null {
  if (kind === "location") {
    const type = locationTypeOf(content);
    return new Set([...COMMON_LOCATION_FIELDS, ...(LOCATION_TYPE_FIELDS[type] ?? [])]);
  }
  const keys = KIND_ATTRIBUTE_KEYS[kind as WikiPageKind];
  return keys ? new Set(keys) : null;
}

/**
 * Attribute keys of a draft the API would refuse (unknown key, or a location
 * field that belongs to another location_type). Confirming a change set that
 * carries one fails when the wiki-service validates it, so the review warns
 * while there is still time to fix it.
 */
export function invalidAttributeKeys(
  kind: string,
  content: Record<string, unknown>,
): string[] {
  const allowed = attributeKeysFor(kind, content);
  if (!allowed) return [];
  return Object.entries(attributesOf(content))
    .filter(([key, value]) => !allowed.has(key) && !isEmpty(value))
    .map(([key]) => key);
}

function isEmpty(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (typeof value === "string") return value.trim() === "";
  if (Array.isArray(value)) return value.length === 0;
  return false;
}

/**
 * Drop the fields the DM emptied, so "clear this box" means "remove this
 * field" instead of writing an empty string. Latitude/longitude are coerced
 * back to numbers (the schema types them as float) and dropped when they are
 * not a number at all.
 */
export function pruneContent(content: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(content)) {
    if (key === "attributes") {
      const attributes = pruneAttributes(attributesOf(content));
      if (Object.keys(attributes).length > 0) out.attributes = attributes;
      continue;
    }
    if (isEmpty(value)) continue;
    if (Array.isArray(value) && value.every((item) => typeof item === "string")) {
      const rows = (value as string[]).map((item) => item.trim()).filter(Boolean);
      if (rows.length === 0) continue;
      out[key] = rows;
      continue;
    }
    out[key] = value;
  }
  return out;
}

function pruneAttributes(attributes: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(attributes)) {
    if (isEmpty(value)) continue;
    if (key === "latitude" || key === "longitude") {
      const parsed = typeof value === "number" ? value : Number(String(value).replace(",", "."));
      if (!Number.isFinite(parsed)) continue;
      out[key] = parsed;
      continue;
    }
    out[key] = value;
  }
  return out;
}
