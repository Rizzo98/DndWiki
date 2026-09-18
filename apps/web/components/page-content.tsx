// Renders a wiki page's content_json. The LLM draft pipeline (content-service
// merger) produces { summary, aliases?, facts?, session_references?,
// attributes? }; DM-created pages may use any shape (e.g. { body: "..." }),
// so unknown keys fall back to a pretty-printed JSON view.
//
// Character pages get a dedicated layout (kind === "character"): instead of a
// Summary block they render the four canonical sections — Physical look
// (portrait box + static info table + description), Character type, Personality
// and Facts. Session references always link by SESSION NAME when the caller
// supplies a sessionNames map (id -> title), falling back to the id.

"use client";

import { useState, type ReactNode } from "react";
import Link from "next/link";
import { Badge, Button, Card, FileInput } from "./ui";
import { objectUrl } from "@/lib/api";
import { LinkedText, type LinkIndex } from "./linked-text";

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <h3 className="mb-2 text-xs font-bold uppercase tracking-wider text-[color:var(--rl-text-on-parchment-muted)]">{title}</h3>
      {children}
    </div>
  );
}

interface SessionReference {
  sessionId: string;
  facts: string[];
}

/** Friendly labels for the per-kind attribute fields (mirrors
 * wiki-service app/page_attributes.py). */
const ATTRIBUTE_LABELS: Record<string, string> = {
  character_type: "Character type",
  race: "Race",
  class: "Class",
  character_class: "Class",
  location_type: "Location type",
  region: "Region",
  latitude: "Latitude",
  longitude: "Longitude",
  founded: "Founded",
  population: "Population",
  government: "Government",
  ruler: "Ruler",
  demographics: "Demographics",
  economy: "Economy",
  defenses: "Defenses",
  religion: "Religion",
  districts: "Districts",
  notable_locations: "Notable locations",
  capital: "Capital",
  terrain: "Terrain",
  climate: "Climate",
  pantheon: "Pantheon",
  planes: "Planes",
  purpose: "Purpose",
  entrance: "Entrance",
  levels: "Levels",
  hazards: "Hazards",
  flora_fauna: "Flora & fauna",
  faction_type: "Faction type",
  leader: "Leader",
  headquarters: "Headquarters",
  item_type: "Item type",
  rarity: "Rarity",
  owner: "Owner",
  quest_status: "Quest status",
  giver: "Given by",
  reward: "Reward",
};

/** Display order of a location's attribute rows per location_type (mirrors
 * wiki-service applicability). A field present on the page but missing from
 * its type's list still renders, appended in entry order. */
const LOCATION_ATTRIBUTE_ORDER: Record<string, string[]> = {
  city: ["population", "government", "ruler", "demographics", "economy", "defenses", "religion", "districts", "notable_locations", "founded", "region", "latitude", "longitude"],
  town: ["population", "government", "ruler", "demographics", "economy", "defenses", "religion", "districts", "notable_locations", "founded", "region", "latitude", "longitude"],
  village: ["population", "government", "ruler", "demographics", "economy", "defenses", "religion", "notable_locations", "founded", "region", "latitude", "longitude"],
  region: ["capital", "government", "ruler", "terrain", "climate", "notable_locations", "founded", "region", "latitude", "longitude"],
  continent: ["capital", "government", "ruler", "terrain", "climate", "notable_locations", "founded", "region", "latitude", "longitude"],
  world: ["terrain", "climate", "planes", "pantheon", "notable_locations", "founded", "region", "latitude", "longitude"],
  building: ["owner", "purpose", "founded", "region", "latitude", "longitude"],
  structure: ["owner", "purpose", "founded", "region", "latitude", "longitude"],
  dungeon: ["entrance", "levels", "hazards", "founded", "region", "latitude", "longitude"],
  wilderness: ["terrain", "climate", "hazards", "flora_fauna", "notable_locations", "founded", "region", "latitude", "longitude"],
  other: ["region", "founded", "latitude", "longitude"],
};

/** Static info table fields of the character page (Physical look section). */
const CHARACTER_STATIC_FIELDS: { key: string; label: string }[] = [
  { key: "race", label: "Race" },
  { key: "class", label: "Class" },
  { key: "gender", label: "Gender" },
  { key: "height", label: "Height" },
  { key: "weight", label: "Weight" },
  { key: "age", label: "Age" },
];

/** Human text for attribute values ("npc" -> "NPC", "in_progress" ->
 * "In progress", ["Porto", "Alto"] -> "Porto, Alto"). */
function formatAttributeValue(key: string, value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") return String(value);
  if (Array.isArray(value)) {
    const parts = value
      .filter((v): v is string => typeof v === "string" && v.trim() !== "")
      .map((v) => v.trim());
    return parts.length ? parts.join(", ") : null;
  }
  if (typeof value !== "string") return null;
  const text = value.replace(/_/g, " ");
  if (key === "character_type") {
    return value === "player" ? "Player character" : "NPC";
  }
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Order the location's attribute rows per its location_type (fields the
 * type does not know still render, appended in entry order). The type itself
 * is always the first row: the page badge only names the kind ("Location"),
 * this names the subtype ("City", "Region", ...). */
function orderedLocationAttributes(
  attributesRaw: Record<string, unknown>,
): [string, string][] {
  const type =
    typeof attributesRaw.location_type === "string" ? attributesRaw.location_type : "other";
  const order = LOCATION_ATTRIBUTE_ORDER[type] ?? LOCATION_ATTRIBUTE_ORDER.other;
  const byKey: Record<string, [string, string]> = {};
  const entries: [string, string][] = [];
  const typeLabel = formatAttributeValue("location_type", type);
  if (typeLabel !== null) entries.push(["location_type", typeLabel]);
  for (const [key, value] of Object.entries(attributesRaw)) {
    if (key === "location_type") continue;
    const formatted = formatAttributeValue(key, value);
    if (formatted !== null) {
      byKey[key] = [key, formatted];
      entries.push([key, formatted]);
    }
  }
  const rank = (key: string) => {
    // The subtype is not part of the per-type order: rank it above every field.
    if (key === "location_type") return -1;
    const i = order.indexOf(key);
    return i === -1 ? order.length : i;
  };
  entries.sort((a, b) => rank(a[0]) - rank(b[0]));
  return entries;
}

/** Portrait box: the uploaded image (presigned URL, proxied) + DM upload.
 * Compact by design so it never stretches the Physical look section. */
function CharacterPortraitBox({
  imageUrl,
  onUploadImage,
  uploadingImage,
}: {
  imageUrl?: string | null;
  onUploadImage?: (file: File) => void;
  uploadingImage?: boolean;
}) {
  const [file, setFile] = useState<File | null>(null);
  const src = imageUrl ? objectUrl(imageUrl) : null;
  return (
    <div className="rounded-lg border border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-bg-parchment-sunk)] p-2">
      <div className="flex h-40 items-center justify-center overflow-hidden rounded-md bg-[color:var(--rl-bg-card)]">
        {src ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={src} alt="Character portrait" className="h-full w-full object-cover" />
        ) : (
          <span className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">No portrait yet</span>
        )}
      </div>
      {onUploadImage ? (
        <div className="mt-2 flex items-center gap-2">
          <FileInput
            accept="image/*"
            className="min-w-0 flex-1 text-xs"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
          <Button
            type="button"
            variant="secondary"
            className="shrink-0 px-3 py-1.5 text-xs"
            disabled={!file || uploadingImage}
            onClick={() => {
              if (file) onUploadImage(file);
              setFile(null);
            }}
          >
            {uploadingImage ? "Uploading…" : "Upload"}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

export function PageContent({
  content,
  campaignId,
  kind,
  sessionNames,
  imageUrl,
  onUploadImage,
  uploadingImage = false,
  hidePortrait = false,
  linkIndex,
  currentPageId,
}: {
  content: Record<string, unknown>;
  campaignId?: string;
  kind?: string;
  /** session id -> display name; used to label session references. */
  sessionNames?: Record<string, string>;
  /** Presigned portrait URL (from the page detail API). */
  imageUrl?: string | null;
  /** When provided (DM), the portrait box gains an upload control. */
  onUploadImage?: (file: File) => void;
  uploadingImage?: boolean;
  /** Omit the portrait box entirely (used by version snapshots whose image
   *  is not the current one — no presigned URL exists for it). */
  hidePortrait?: boolean;
  /** Campaign page index: entity names in the text link to their pages. */
  linkIndex?: LinkIndex | null;
  /** The page being viewed (its own names are not self-linked). */
  currentPageId?: string | null;
}) {
  const body = typeof content.body === "string" ? content.body : null;
  const summary = typeof content.summary === "string" ? content.summary : null;
  // Cross-type narrative section (locations: founding/wars/famous events).
  const history =
    typeof content.history === "string" && content.history.trim() ? content.history : null;
  const aliases = Array.isArray(content.aliases) ? content.aliases.filter((a): a is string => typeof a === "string") : [];
  const facts = Array.isArray(content.facts) ? content.facts.filter((f): f is string => typeof f === "string") : [];

  const attributesRaw =
    typeof content.attributes === "object" && content.attributes !== null
      ? (content.attributes as Record<string, unknown>)
      : {};

  // Structured category attributes (wiki-service validates them server-side):
  // rendered as a definition list, unknown-but-valid keys included. Location
  // rows are ordered by location_type (city -> population/government/...).
  const attributes: [string, string][] =
    kind === "location"
      ? orderedLocationAttributes(attributesRaw)
      : Object.entries(attributesRaw)
          .map(([key, value]) => [key, formatAttributeValue(key, value)] as [string, string | null])
          .filter((entry): entry is [string, string] => entry[1] !== null);

  // Session-scoped facts recorded by the draft pipeline: grouped per source
  // session and linked back to the session that produced them.
  const sessionReferences: SessionReference[] = Array.isArray(content.session_references)
    ? (content.session_references as unknown[])
        .map((ref): SessionReference | null => {
          if (typeof ref !== "object" || ref === null) return null;
          const r = ref as Record<string, unknown>;
          if (typeof r.session_id !== "string") return null;
          const groupFacts = Array.isArray(r.facts)
            ? r.facts.filter((f): f is string => typeof f === "string")
            : [];
          return { sessionId: r.session_id, facts: groupFacts };
        })
        .filter((r): r is SessionReference => r !== null && r.facts.length > 0)
    : [];

  const knownKeys = new Set([
    "summary",
    "body",
    "aliases",
    "facts",
    "session_references",
    "attributes",
    "language",
    "confidence",
    "physical_look",
    "personality",
    "history",
    "image_uri",
  ]);
  const extraKeys = Object.keys(content).filter((k) => !knownKeys.has(k));

  // Renders one free-text value with entity names linked to their pages.
  const linkify = (value: string) => (
    <LinkedText text={value} campaignId={campaignId} index={linkIndex} currentPageId={currentPageId} />
  );

  const sessionHref = (sessionId: string) =>
    campaignId ? "/campaigns/" + campaignId + "/sessions/" + sessionId : null;
  // Session references must read as the SESSION NAME, not the id.
  const sessionLabel = (sessionId: string) =>
    sessionNames?.[sessionId] || "session " + sessionId.slice(0, 8);

  // ------------------------------------------------------------- characters
  if (kind === "character") {
    // Legacy drafts (pre-v4) carried the description under 'summary' — fall
    // back to it so old character pages keep showing their content.
    const physicalLook =
      typeof content.physical_look === "string" && content.physical_look.trim()
        ? content.physical_look
        : typeof content.summary === "string" && content.summary.trim()
          ? content.summary
          : null;
    const personality =
      typeof content.personality === "string" && content.personality.trim()
        ? content.personality
        : null;
    const characterType = attributesRaw.character_type === "player" ? "player" : "npc";
    const staticRows = CHARACTER_STATIC_FIELDS.map(({ key, label }) => {
      const value = formatAttributeValue(key, attributesRaw[key]);
      return value ? { key, label, value } : null;
    }).filter((row): row is { key: string; label: string; value: string } => row !== null);

    const hasCharacterContent =
      Boolean(physicalLook) ||
      Boolean(personality) ||
      Boolean(history) ||
      facts.length > 0 ||
      aliases.length > 0 ||
      sessionReferences.length > 0 ||
      staticRows.length > 0 ||
      Boolean(imageUrl) ||
      extraKeys.length > 0;

    if (!hasCharacterContent) {
      return <p className="text-sm text-[color:var(--rl-text-on-parchment-muted)]">This page has no content yet.</p>;
    }

    // The sections stack tightly (Character type sits directly under
    // Physical look); the portrait floats at the top right of the stack.
    const sections = (
      <div className="space-y-6">
        <Section title="Physical look">
          {staticRows.length > 0 ? (
            <dl className="grid grid-cols-[max-content_1fr] items-baseline gap-x-6 gap-y-2">
              {staticRows.map((row) => (
                <div key={row.key} className="contents">
                  <dt className="text-sm text-[color:var(--rl-text-on-parchment-muted)]">{row.label}</dt>
                  <dd className="text-sm text-[color:var(--rl-text-on-parchment-primary)]">{linkify(row.value)}</dd>
                </div>
              ))}
            </dl>
          ) : null}
          {physicalLook ? (
            <p className="whitespace-pre-wrap text-[color:var(--rl-text-on-parchment-primary)]">{linkify(physicalLook)}</p>
          ) : (
            <p className="text-sm text-[color:var(--rl-text-on-parchment-muted)]">No physical description yet.</p>
          )}
        </Section>

        <Section title="Character type">
          <Badge tone={characterType === "player" ? "emerald" : "slate"}>
            {characterType === "player" ? "Player character" : "NPC"}
          </Badge>
        </Section>

        <Section title="Personality">
          {personality ? (
            <p className="whitespace-pre-wrap text-[color:var(--rl-text-on-parchment-primary)]">{linkify(personality)}</p>
          ) : (
            <p className="text-sm text-[color:var(--rl-text-on-parchment-muted)]">No personality described yet.</p>
          )}
        </Section>

        {history ? (
          <Section title="History">
            <p className="whitespace-pre-wrap text-[color:var(--rl-text-on-parchment-primary)]">{linkify(history)}</p>
          </Section>
        ) : null}

        <Section title="Facts">
          {facts.length > 0 ? (
            <ul className="list-inside list-disc space-y-1 text-[color:var(--rl-text-on-parchment-primary)]">
              {facts.map((f) => (
                <li key={f}>{linkify(f)}</li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-[color:var(--rl-text-on-parchment-muted)]">No facts recorded yet.</p>
          )}
        </Section>

        {aliases.length > 0 ? (
          <Section title="Aliases">
            <div className="flex flex-wrap gap-2">
              {aliases.map((a) => (
                <span key={a} className="rounded-full bg-[color:var(--rl-bg-parchment-sunk)] px-3 py-1 text-sm text-[color:var(--rl-text-on-parchment-primary)]">
                  {a}
                </span>
              ))}
            </div>
          </Section>
        ) : null}

        {sessionReferences.length > 0 ? (
          <Section title="Session references">
            <p className="mb-2 text-xs text-[color:var(--rl-text-on-parchment-muted)]">
              Session-specific details tied to the sessions that produced them.
            </p>
            <ul className="space-y-3">
              {sessionReferences.map((ref) => {
                const href = sessionHref(ref.sessionId);
                const label = sessionLabel(ref.sessionId);
                return (
                  <li key={ref.sessionId} className="text-sm">
                    {href ? (
                      <Link href={href} className="font-mono text-xs rl-text-item hover:underline">
                        {label}
                      </Link>
                    ) : (
                      <span className="font-mono text-xs text-[color:var(--rl-text-on-parchment-muted)]">{label}</span>
                    )}
                    <ul className="mt-1 list-inside list-disc space-y-1 text-[color:var(--rl-text-on-parchment-primary)]">
                      {ref.facts.map((f) => (
                        <li key={f}>{linkify(f)}</li>
                      ))}
                    </ul>
                  </li>
                );
              })}
            </ul>
          </Section>
        ) : null}

        {extraKeys.length > 0 ? (
          <Section title="Raw content">
            <Card className="overflow-x-auto p-3">
              <pre className="whitespace-pre-wrap font-mono text-xs text-[color:var(--rl-text-on-parchment-muted)]">
                {JSON.stringify(content, null, 2)}
              </pre>
            </Card>
          </Section>
        ) : null}
      </div>
    );

    if (hidePortrait) {
      return sections;
    }

    // Portrait first in DOM (renders above the sections on mobile); on lg+ it
    // moves to column 2, row 1 so it sits beside the Physical look section
    // without dictating the height of the section stack.
    return (
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_12rem] lg:items-start">
        <div className="lg:col-start-2 lg:row-start-1">
          <CharacterPortraitBox
            imageUrl={imageUrl}
            onUploadImage={onUploadImage}
            uploadingImage={uploadingImage}
          />
        </div>
        <div className="lg:col-start-1 lg:row-start-1">{sections}</div>
      </div>
    );
  }

  // --------------------------------------------------- events
  if (kind === "event") {
    const participants = Array.isArray(attributesRaw.participants)
      ? attributesRaw.participants.filter((p): p is string => typeof p === "string")
      : [];
    const eventType = formatAttributeValue("event_type", attributesRaw.event_type);
    const eventStatus = formatAttributeValue("event_status", attributesRaw.event_status);
    const inWorldDate = formatAttributeValue("in_world_date", attributesRaw.in_world_date);

    const hasEventContent =
      Boolean(summary) ||
      Boolean(history) ||
      facts.length > 0 ||
      aliases.length > 0 ||
      sessionReferences.length > 0 ||
      participants.length > 0 ||
      Boolean(eventType) ||
      Boolean(eventStatus) ||
      Boolean(inWorldDate) ||
      extraKeys.length > 0;

    if (!hasEventContent) {
      return <p className="text-sm text-[color:var(--rl-text-on-parchment-muted)]">This event page has no content yet.</p>;
    }

    return (
      <div className="space-y-6">
        {eventType || inWorldDate || eventStatus ? (
          <div className="flex flex-wrap items-center gap-2">
            {eventType ? <Badge tone="amber">{eventType}</Badge> : null}
            {inWorldDate ? (
              <span className="text-xs font-semibold uppercase tracking-wide rl-text-accent">{inWorldDate}</span>
            ) : null}
            {eventStatus ? <Badge tone="slate">{eventStatus}</Badge> : null}
          </div>
        ) : null}

        {summary ? (
          <Section title="Overview">
            <p className="whitespace-pre-wrap text-[color:var(--rl-text-on-parchment-primary)]">{linkify(summary)}</p>
          </Section>
        ) : null}

        {participants.length > 0 ? (
          <Section title="Participants">
            <div className="flex flex-wrap gap-2">
              {participants.map((p) => (
                <span key={p} className="rounded-full bg-[color:var(--rl-bg-parchment-sunk)] px-3 py-1 text-sm text-[color:var(--rl-text-on-parchment-primary)]">
                  {linkify(p)}
                </span>
              ))}
            </div>
          </Section>
        ) : null}

        {history ? (
          <Section title="History">
            <p className="whitespace-pre-wrap text-[color:var(--rl-text-on-parchment-primary)]">{linkify(history)}</p>
          </Section>
        ) : null}

        {facts.length > 0 ? (
          <Section title="Facts">
            <ul className="list-inside list-disc space-y-1 text-[color:var(--rl-text-on-parchment-primary)]">
              {facts.map((f) => (
                <li key={f}>{linkify(f)}</li>
              ))}
            </ul>
          </Section>
        ) : null}

        {aliases.length > 0 ? (
          <Section title="Aliases">
            <div className="flex flex-wrap gap-2">
              {aliases.map((a) => (
                <span key={a} className="rounded-full bg-[color:var(--rl-bg-parchment-sunk)] px-3 py-1 text-sm text-[color:var(--rl-text-on-parchment-primary)]">
                  {a}
                </span>
              ))}
            </div>
          </Section>
        ) : null}

        {sessionReferences.length > 0 ? (
          <Section title="Session references">
            <p className="mb-2 text-xs text-[color:var(--rl-text-on-parchment-muted)]">
              Session-specific details tied to the sessions that produced them.
            </p>
            <ul className="space-y-3">
              {sessionReferences.map((ref) => {
                const href = sessionHref(ref.sessionId);
                const label = sessionLabel(ref.sessionId);
                return (
                  <li key={ref.sessionId} className="text-sm">
                    {href ? (
                      <Link href={href} className="font-mono text-xs rl-text-item hover:underline">
                        {label}
                      </Link>
                    ) : (
                      <span className="font-mono text-xs text-[color:var(--rl-text-on-parchment-muted)]">{label}</span>
                    )}
                    <ul className="mt-1 list-inside list-disc space-y-1 text-[color:var(--rl-text-on-parchment-primary)]">
                      {ref.facts.map((f) => (
                        <li key={f}>{linkify(f)}</li>
                      ))}
                    </ul>
                  </li>
                );
              })}
            </ul>
          </Section>
        ) : null}

        {extraKeys.length > 0 ? (
          <Section title="Raw content">
            <Card className="overflow-x-auto p-3">
              <pre className="whitespace-pre-wrap font-mono text-xs text-[color:var(--rl-text-on-parchment-muted)]">
                {JSON.stringify(content, null, 2)}
              </pre>
            </Card>
          </Section>
        ) : null}
      </div>
    );
  }

  // --------------------------------------------------- other page kinds
  const hasAnything =
    Boolean(body) ||
    Boolean(summary) ||
    Boolean(history) ||
    aliases.length > 0 ||
    facts.length > 0 ||
    attributes.length > 0 ||
    sessionReferences.length > 0 ||
    extraKeys.length > 0;

  return (
    <div className="space-y-6">
      {body ? (
        <p className="whitespace-pre-wrap text-[color:var(--rl-text-on-parchment-primary)]">{linkify(body)}</p>
      ) : null}
      {summary ? (
        <Section title="Overview">
          <p className="whitespace-pre-wrap text-[color:var(--rl-text-on-parchment-primary)]">{linkify(summary)}</p>
        </Section>
      ) : null}

      {history ? (
        <Section title="History">
          <p className="whitespace-pre-wrap text-[color:var(--rl-text-on-parchment-primary)]">{linkify(history)}</p>
        </Section>
      ) : null}

      {attributes.length > 0 ? (
        <Section title={kind === "location" ? "Location details" : "Attributes"}>
          <dl className="grid grid-cols-[max-content_1fr] items-baseline gap-x-6 gap-y-2">
            {attributes.map(([key, value]) => (
              <div key={key} className="contents">
                <dt className="text-sm text-[color:var(--rl-text-on-parchment-muted)]">{ATTRIBUTE_LABELS[key] ?? key}</dt>
                <dd className="text-sm text-[color:var(--rl-text-on-parchment-primary)]">{linkify(value)}</dd>
              </div>
            ))}
          </dl>
        </Section>
      ) : null}

      {aliases.length > 0 ? (
        <Section title="Aliases">
          <div className="flex flex-wrap gap-2">
            {aliases.map((a) => (
              <span key={a} className="rounded-full bg-[color:var(--rl-bg-parchment-sunk)] px-3 py-1 text-sm text-[color:var(--rl-text-on-parchment-primary)]">
                {a}
              </span>
            ))}
          </div>
        </Section>
      ) : null}

      {facts.length > 0 ? (
        <Section title="Facts">
          <ul className="list-inside list-disc space-y-1 text-[color:var(--rl-text-on-parchment-primary)]">
            {facts.map((f) => (
              <li key={f}>{linkify(f)}</li>
            ))}
          </ul>
        </Section>
      ) : null}

      {sessionReferences.length > 0 ? (
        <Section title="Session references">
          <p className="mb-2 text-xs text-[color:var(--rl-text-on-parchment-muted)]">
            Session-specific details tied to the sessions that produced them.
          </p>
          <ul className="space-y-3">
            {sessionReferences.map((ref) => {
              const href = sessionHref(ref.sessionId);
              const label = sessionLabel(ref.sessionId);
              return (
                <li key={ref.sessionId} className="text-sm">
                  {href ? (
                    <Link href={href} className="font-mono text-xs rl-text-item hover:underline">
                      {label}
                    </Link>
                  ) : (
                    <span className="font-mono text-xs text-[color:var(--rl-text-on-parchment-muted)]">{label}</span>
                  )}
                  <ul className="mt-1 list-inside list-disc space-y-1 text-[color:var(--rl-text-on-parchment-primary)]">
                    {ref.facts.map((f) => (
                      <li key={f}>{linkify(f)}</li>
                    ))}
                  </ul>
                </li>
              );
            })}
          </ul>
        </Section>
      ) : null}

      {extraKeys.length > 0 ? (
        <Section title="Raw content">
          <Card className="overflow-x-auto p-3">
            <pre className="whitespace-pre-wrap font-mono text-xs text-[color:var(--rl-text-on-parchment-muted)]">
              {JSON.stringify(content, null, 2)}
            </pre>
          </Card>
        </Section>
      ) : null}

      {!hasAnything ? (
        <p className="text-sm text-[color:var(--rl-text-on-parchment-muted)]">This page has no content yet.</p>
      ) : null}
    </div>
  );
}