// PageDraftForm — the editor for the CONTENT of a wiki page draft.
//
// It renders a page's content_json the way the kind's schema sees it: the
// narrative fields as proper prose editors (with the "#" page-link
// autocomplete), name/fact lists as one row per item, and the validated
// attributes as a labelled grid. It is deliberately content-only — title,
// visibility and timeline entry belong to the change, not to the page — so a
// future page editor can mount it next to its own chrome.
//
// Two rules come from the API and are enforced here:
//   * attributes are a closed set per kind (extra keys are rejected), so the
//     editor offers only the keys the schema accepts;
//   * a location field must belong to the page's location_type, so a draft
//     that carries a foreign field is shown as a problem to fix, not silently
//     written.

"use client";

import { Alert, Field, Select, TextInput } from "./ui";
import { RlButton, RlIcon, RlTag } from "./ravenlore";
import { PageLinkEditor, type PageLinkSuggestion } from "./page-link-editor";
import {
  attributeKeysFor,
  emptyValueFor,
  invalidAttributeKeys,
  kindFields,
  locationTypeOf,
  type DraftFieldSpec,
} from "@/lib/page-fields";

export type DraftContent = Record<string, unknown>;

/** Keys that are metadata rather than page fields (never edited as fields). */
const META_KEYS = new Set([
  "attributes",
  "language",
  "image_uri",
  "confidence",
  "session_references",
  "title",
]);

interface SessionReference {
  sessionId: string;
  facts: string[];
}

function attributesOf(content: DraftContent): Record<string, unknown> {
  const value = content.attributes;
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function sessionReferencesOf(content: DraftContent): SessionReference[] {
  if (!Array.isArray(content.session_references)) return [];
  return (content.session_references as unknown[])
    .map((raw): SessionReference | null => {
      if (typeof raw !== "object" || raw === null) return null;
      const ref = raw as Record<string, unknown>;
      if (typeof ref.session_id !== "string") return null;
      const facts = Array.isArray(ref.facts)
        ? ref.facts.filter((f): f is string => typeof f === "string")
        : [];
      return { sessionId: ref.session_id, facts };
    })
    .filter((ref): ref is SessionReference => ref !== null && ref.facts.length > 0);
}

function asText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.map((item) => String(item)).join(", ");
  return String(value);
}

/** "very_rare" -> "Very rare", "npc" -> "NPC". */
function humanize(value: string): string {
  if (value === "npc") return "NPC";
  if (value === "player") return "Player character";
  const text = value.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function PageDraftForm({
  kind,
  content,
  onChange,
  pages,
  excludePageId,
  sessionNames,
}: {
  kind: string;
  content: DraftContent;
  onChange: (next: DraftContent) => void;
  /** Campaign pages offered by the "#" autocomplete. */
  pages: PageLinkSuggestion[];
  /** The page being edited: never suggested (no self-links). */
  excludePageId?: string | null;
  /** session id -> display name, for the session facts of the page. */
  sessionNames?: Record<string, string>;
}) {
  const fields = kindFields(kind);
  const attributes = attributesOf(content);
  const invalid = invalidAttributeKeys(kind, content);
  const allowed = attributeKeysFor(kind, content);

  // The narrative is what the page IS: always offered, key or no key.
  // Attributes are offered when they carry a value, when they are an enum (a
  // select always has something to say) or when the kind marks them essential;
  // the rest stay behind "add a field" so a location page does not open with
  // twenty empty boxes.
  const attributeSpecs = fields.attributes.filter(
    (spec) => spec.key in attributes || spec.core || spec.type === "select",
  );
  // "Add a field" only offers what the schema accepts for THIS page (a dungeon
  // has no population, a guild no rarity), so a field can never be added into
  // a change set the wiki-service would reject.
  const addable = fields.attributes.filter(
    (spec) =>
      !(spec.key in attributes) &&
      !spec.core &&
      spec.type !== "select" &&
      (allowed === null || allowed.has(spec.key)),
  );
  const covered = new Set(
    [...fields.prose, ...fields.lists, ...fields.attributes].map((spec) => spec.key),
  );
  const extraKeys = Object.keys(content).filter(
    (key) => !covered.has(key) && !META_KEYS.has(key),
  );
  const sessionReferences = sessionReferencesOf(content);

  function set(key: string, value: unknown) {
    onChange({ ...content, [key]: value });
  }

  function withoutKey(key: string) {
    const next = { ...content };
    delete next[key];
    onChange(next);
  }

  function setAttribute(key: string, value: unknown) {
    onChange({ ...content, attributes: { ...attributes, [key]: value } });
  }

  function removeAttribute(key: string) {
    const next = { ...attributes };
    delete next[key];
    onChange({ ...content, attributes: next });
  }

  /** A list is either a top-level key (facts, aliases) or an attribute. */
  function listField(spec: DraftFieldSpec, inAttributes: boolean) {
    return (
      <ListField
        key={spec.key}
        spec={spec}
        value={inAttributes ? attributes[spec.key] : content[spec.key]}
        pages={pages}
        excludePageId={excludePageId}
        onChange={(next) =>
          inAttributes ? setAttribute(spec.key, next) : set(spec.key, next)
        }
      />
    );
  }

  return (
    <div className="space-y-5">
      {fields.prose.map((spec) => (
        <Field key={spec.key} label={spec.label} hint={spec.hint}>
          <PageLinkEditor
            rows={spec.rows ?? 4}
            value={asText(content[spec.key])}
            onChange={(value) => set(spec.key, value)}
            pages={pages}
            excludePageId={excludePageId}
          />
        </Field>
      ))}

      {fields.lists.map((spec) => listField(spec, false))}

      <section className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="rl-eyebrow">Details</span>
          {addable.length > 0 ? (
            <label className="flex items-center gap-2">
              <span className="rl-card-meta">Add a field</span>
              <Select
                className="w-52"
                value=""
                onChange={(e) => {
                  const spec = addable.find((item) => item.key === e.target.value);
                  if (spec) setAttribute(spec.key, emptyValueFor(spec));
                }}
              >
                <option value="">Choose…</option>
                {addable.map((spec) => (
                  <option key={spec.key} value={spec.key}>
                    {spec.label}
                  </option>
                ))}
              </Select>
            </label>
          ) : null}
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          {attributeSpecs.map((spec) =>
            spec.type === "list" ? (
              <div key={spec.key} className="sm:col-span-2">
                {listField(spec, true)}
              </div>
            ) : (
              <AttributeField
                key={spec.key}
                spec={spec}
                value={attributes[spec.key]}
                invalid={invalid.includes(spec.key)}
                onChange={(value) => setAttribute(spec.key, value)}
              />
            ),
          )}
        </div>

        {invalid.length > 0 ? (
          <Alert tone="error">
            <span className="font-semibold">
              {invalid.map((key) => key.replace(/_/g, " ")).join(", ")}
            </span>{" "}
            {invalid.length === 1 ? "is not a field" : "are not fields"} the wiki accepts for a{" "}
            {kind}
            {kind === "location" ? " of this kind (" + locationTypeOf(content) + ")" : ""} — the
            change set fails when it is written. Remove{" "}
            {invalid.length === 1 ? "it" : "them"} here, or fix the field above it names.
            <span className="mt-2 flex flex-wrap gap-2">
              {invalid.map((key) => (
                <RlButton
                  key={key}
                  variant="outline"
                  size="sm"
                  onClick={() => removeAttribute(key)}
                >
                  Remove {key.replace(/_/g, " ")}
                </RlButton>
              ))}
            </span>
          </Alert>
        ) : null}
      </section>

      {extraKeys.length > 0 ? (
        <section className="space-y-2">
          <span className="rl-eyebrow">Other fields</span>
          <div className="space-y-1.5 rounded-[var(--rl-radius-sm)] border border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-bg-card)] p-3">
            {extraKeys.map((key) => (
              <div key={key} className="flex items-start justify-between gap-3 text-xs">
                <span className="min-w-0">
                  <span className="font-semibold text-[color:var(--rl-text-on-parchment-primary)]">
                    {key.replace(/_/g, " ")}
                  </span>
                  <span className="ml-2 text-[color:var(--rl-text-on-parchment-muted)]">
                    {asText(content[key])}
                  </span>
                </span>
                <RlButton variant="ghost" size="sm" onClick={() => withoutKey(key)}>
                  Remove
                </RlButton>
              </div>
            ))}
          </div>
        </section>
      ) : null}

      {sessionReferences.length > 0 ? (
        <section className="space-y-2">
          <span className="rl-eyebrow">Session facts</span>
          <div className="space-y-3 rounded-[var(--rl-radius-sm)] border border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-bg-card)] p-3">
            <p className="rl-card-meta">
              Recorded with the session they come from, and kept with the page. Edit the
              session summary to change them.
            </p>
            {sessionReferences.map((ref) => (
              <div key={ref.sessionId}>
                <RlTag tint="ink">
                  {sessionNames?.[ref.sessionId] ?? ref.sessionId.slice(0, 8)}
                </RlTag>
                <ul className="mt-1.5 list-inside list-disc space-y-1 text-xs text-[color:var(--rl-text-on-parchment-primary)]">
                  {ref.facts.map((fact) => (
                    <li key={fact}>{fact}</li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------- pieces

/** One labelled attribute of the kind's schema. */
function AttributeField({
  spec,
  value,
  invalid,
  onChange,
}: {
  spec: DraftFieldSpec;
  value: unknown;
  invalid: boolean;
  onChange: (value: unknown) => void;
}) {
  const className = invalid ? "border-[color:var(--rl-cat-villain)]" : undefined;
  if (spec.type === "select") {
    const options = spec.options ?? [];
    const current = typeof value === "string" ? value : "";
    return (
      <Field label={spec.label} hint={spec.hint}>
        <Select
          className={className}
          value={current}
          onChange={(e) => onChange(e.target.value)}
        >
          {current === "" ? <option value="">—</option> : null}
          {options.map((option) => (
            <option key={option} value={option}>
              {humanize(option)}
            </option>
          ))}
          {!options.includes(current) && current !== "" ? (
            <option value={current}>{current}</option>
          ) : null}
        </Select>
      </Field>
    );
  }
  return (
    <Field label={spec.label} hint={spec.hint}>
      <TextInput
        className={className}
        value={asText(value)}
        onChange={(e) => onChange(e.target.value)}
      />
    </Field>
  );
}

/**
 * A list of short values: one editor per row, so a single bad fact can be
 * fixed or deleted without touching its neighbours.
 */
function ListField({
  spec,
  value,
  onChange,
  pages,
  excludePageId,
}: {
  spec: DraftFieldSpec;
  value: unknown;
  onChange: (next: string[]) => void;
  pages: PageLinkSuggestion[];
  excludePageId?: string | null;
}) {
  const items = Array.isArray(value) ? value.map((item) => asText(item)) : [];

  function replace(index: number, next: string) {
    onChange(items.map((item, i) => (i === index ? next : item)));
  }

  function remove(index: number) {
    onChange(items.filter((_, i) => i !== index));
  }

  return (
    <Field label={spec.label} hint={spec.hint}>
      <div className="space-y-1.5 rounded-[var(--rl-radius-sm)] border border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-bg-card)] p-2">
        {items.map((item, index) => (
          <div key={index} className="flex items-start gap-1.5">
            <div className="min-w-0 flex-1">
              {spec.plain ? (
                <TextInput
                  value={item}
                  onChange={(e) => replace(index, e.target.value)}
                />
              ) : (
                <PageLinkEditor
                  rows={spec.rows ?? 2}
                  value={item}
                  onChange={(next) => replace(index, next)}
                  pages={pages}
                  excludePageId={excludePageId}
                />
              )}
            </div>
            <RlButton
              variant="ghost"
              size="sm"
              title="Remove this row"
              aria-label={"Remove row " + (index + 1)}
              onClick={() => remove(index)}
            >
              ✕
            </RlButton>
          </div>
        ))}
        <RlButton variant="ghost" size="sm" onClick={() => onChange([...items, ""])}>
          <RlIcon name="plus" size={14} />
          {items.length === 0 ? "Add the first one" : "Add a row"}
        </RlButton>
      </div>
    </Field>
  );
}
