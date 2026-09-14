// Session plan card: the "git status" of a session.
//
// The confirmed summary is turned into a set of PROPOSED wiki changes — pages
// to create, pages to update and the timeline entries they back. Nothing is in
// the wiki yet: this card is where the DM inspects each change (the same form
// as the editor, read-only, so a creation reads like the page it will become
// and an update shows the fields it rewrites with the previous value), edits or
// drops the ones they disagree with, and confirms the whole set. Only then does
// the pipeline write the pages (published) and the timeline entries (approved).

"use client";

import { useMemo, useState } from "react";
import { Alert, Badge, Button, Card, EmptyState, Field, Select, TextArea, TextInput, fmtDate } from "@/components/ui";
import { LinkedText, type LinkIndex } from "@/components/linked-text";
import {
  PAGE_KIND_LABELS,
  PAGE_KIND_TITLES,
  type PlanChange,
  type PlanChangeEdit,
  type PlanRelationEdit,
  type SessionPlan,
  type WikiPageKind,
  type WikiVisibility,
} from "@/lib/api";

// ------------------------------------------------------------- diff helpers

const FIELD_LABELS: Record<string, string> = {
  summary: "Summary",
  physical_look: "Physical look",
  personality: "Personality",
  history: "History",
  facts: "Durable facts",
  session_facts: "Session facts",
  aliases: "Aliases",
  body: "Body",
  language: "Language",
  session_references: "Session references",
  image_uri: "Portrait",
  "attributes.character_type": "Type",
  "attributes.race": "Race",
  "attributes.class": "Class",
  "attributes.gender": "Gender",
  "attributes.height": "Height",
  "attributes.weight": "Weight",
  "attributes.age": "Age",
  "attributes.location_type": "Kind of place",
  "attributes.region": "Region",
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
};

/** Human label of a flattened content path ("attributes.population"). */
export function fieldLabel(path: string): string {
  if (FIELD_LABELS[path]) return FIELD_LABELS[path];
  const last = path.split(".").pop() ?? path;
  return last.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

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

/** Set (or delete) a nested value by path, mutating the copy in place. */
function setPath(target: Record<string, unknown>, path: string, value: unknown): void {
  const parts = path.split(".");
  let cursor: Record<string, unknown> = target;
  for (const part of parts.slice(0, -1)) {
    const next = cursor[part];
    if (typeof next !== "object" || next === null || Array.isArray(next)) {
      cursor[part] = {};
    }
    cursor = cursor[part] as Record<string, unknown>;
  }
  const last = parts[parts.length - 1];
  if (value === "" || value === null || value === undefined) delete cursor[last];
  else cursor[last] = value;
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

/** Render a leaf value for display. */
export function displayValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.map((v) => String(v)).join(" · ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** Leaf value -> the text an editor shows (lists one per line). */
function editableValue(value: unknown): string {
  if (Array.isArray(value)) return value.map((v) => String(v)).join("\n");
  return displayValue(value);
}

/** Editor text -> the value to store (lists become arrays again). */
function parseValue(text: string, previous: unknown): unknown {
  if (Array.isArray(previous)) {
    return text
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
  }
  return text.trim();
}

// ------------------------------------------------------------- form pieces

/** No local field drafts (inspect mode always shows the stored values). */
const NO_DRAFTS: Record<string, string> = {};

/** Read-only look of an input: same box, muted, nothing to type into. */
const READONLY_CONTROL =
  "w-full rounded-lg border border-slate-800 bg-slate-900/50 px-3 py-2 text-sm text-slate-300 outline-none";

function Chevron({ open }: { open: boolean }) {
  return (
    <span
      aria-hidden
      className={"inline-block text-xs text-slate-500 transition-transform " + (open ? "rotate-90" : "")}
    >
      ▶
    </span>
  );
}

function ActionChip({ action }: { action: "create" | "update" }) {
  return action === "create" ? (
    <Badge tone="green">+ new</Badge>
  ) : (
    <Badge tone="amber">~ update</Badge>
  );
}

/** The proposed value of a change: what the page will say. */
function ChangeForm({
  change,
  readOnly,
  drafts,
  onDraft,
  rawDraft,
  onRawDraft,
  rawError,
  onTitle,
  onTimeline,
  onVisibility,
}: {
  change: PlanChange;
  /** Inspect mode: every control is the editor's, disabled (nothing to type). */
  readOnly: boolean;
  drafts: Record<string, string>;
  onDraft: (path: string, value: string) => void;
  rawDraft: string | null;
  onRawDraft: (value: string) => void;
  rawError: string | null;
  onTitle: (title: string) => void;
  onTimeline: (timeline: { summary: string; in_world_date: string | null }) => void;
  onVisibility: (visibility: WikiVisibility) => void;
}) {
  const fields = flattenContent(change.after.content_json ?? {});
  const before = flattenContent(change.before?.content_json ?? {});
  // Fields the update drops: they exist in the page today and not in the
  // proposal (the merge never removes facts silently, so this is rare - and
  // worth showing).
  const removed = change.before
    ? Object.keys(before).filter((path) => !(path in fields))
    : [];
  const titleChanged = Boolean(change.before && change.before.title !== change.after.title);

  return (
    <div className="space-y-3">
      <Field label="Title">
        {readOnly ? (
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold text-slate-100">{change.title}</span>
            {titleChanged ? (
              <span className="text-xs text-slate-500">
                was <span className="text-red-300 line-through">{change.before?.title}</span>
              </span>
            ) : null}
          </div>
        ) : (
          <TextInput value={change.title} onChange={(e) => onTitle(e.target.value)} />
        )}
      </Field>

      {rawDraft !== null ? (
        <Field
          label="content_json"
          hint={
            readOnly
              ? "Raw JSON of the proposed page."
              : "Raw JSON — the escape hatch for anything the fields do not cover."
          }
        >
          <TextArea
            rows={12}
            className={readOnly ? READONLY_CONTROL + " font-mono text-xs" : "font-mono text-xs"}
            value={rawDraft}
            readOnly={readOnly}
            onChange={(e) => onRawDraft(e.target.value)}
          />
        </Field>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {Object.entries(fields).map(([path, value]) => {
            const was = before[path];
            const changed =
              change.before !== null &&
              JSON.stringify(was ?? null) !== JSON.stringify(value ?? null);
            const text = drafts[path] ?? editableValue(value);
            const multiline = Array.isArray(value) || editableValue(value).length > 60;
            return (
              <Field key={path} label={fieldLabel(path)}>
                {multiline ? (
                  <TextArea
                    rows={3}
                    className={readOnly ? READONLY_CONTROL : undefined}
                    value={text}
                    readOnly={readOnly}
                    onChange={(e) => onDraft(path, e.target.value)}
                  />
                ) : (
                  <TextInput
                    className={readOnly ? READONLY_CONTROL : undefined}
                    value={text}
                    readOnly={readOnly}
                    onChange={(e) => onDraft(path, e.target.value)}
                  />
                )}
                {readOnly && changed ? (
                  <span className="mt-1 block text-xs text-slate-500">
                    {was === undefined ? (
                      <span className="text-green-300">new field</span>
                    ) : (
                      <>
                        was <span className="text-red-300 line-through">{displayValue(was)}</span>
                      </>
                    )}
                  </span>
                ) : null}
              </Field>
            );
          })}
        </div>
      )}

      {removed.length ? (
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2">
          <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Dropped from the page
          </div>
          <ul className="mt-1 space-y-0.5">
            {removed.map((path) => (
              <li key={path} className="text-xs text-slate-400">
                <span className="font-semibold">{fieldLabel(path)}</span>:{" "}
                <span className="text-red-300 line-through">{displayValue(before[path])}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {rawError ? <Alert tone="error">{rawError}</Alert> : null}

      {change.timeline ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Timeline entry">
            <TextArea
              rows={2}
              className={readOnly ? READONLY_CONTROL : undefined}
              value={change.timeline.summary}
              readOnly={readOnly}
              onChange={(e) =>
                onTimeline({
                  summary: e.target.value,
                  in_world_date: change.timeline?.in_world_date ?? null,
                })
              }
            />
          </Field>
          <Field label="In-world date">
            <TextInput
              className={readOnly ? READONLY_CONTROL : undefined}
              value={change.timeline.in_world_date ?? ""}
              readOnly={readOnly}
              onChange={(e) =>
                onTimeline({
                  summary: change.timeline?.summary ?? "",
                  in_world_date: e.target.value || null,
                })
              }
            />
          </Field>
        </div>
      ) : null}

      <Field label="Visibility">
        {readOnly ? (
          <div className="text-sm text-slate-300">{change.after.visibility.replace(/_/g, " ")}</div>
        ) : (
          <Select
            className="w-48"
            value={change.after.visibility}
            onChange={(e) => onVisibility(e.target.value as WikiVisibility)}
          >
            <option value="public">public</option>
            <option value="dm_only">dm only</option>
            <option value="hidden">hidden</option>
          </Select>
        )}
      </Field>

      {readOnly ? (
        <p className="text-xs text-slate-500">
          This is the page the change set will write. Switch to Edit to change it, or Drop to
          leave it out.
        </p>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------- component

export function SessionPlanCard({
  plan,
  campaignId,
  linkIndex,
  sessionStatus,
  canReview,
  busy,
  error,
  onSave,
  onConfirm,
}: {
  plan: SessionPlan | null;
  campaignId: string;
  linkIndex?: LinkIndex | null;
  /** Session status: 'generating_wiki' is the moment the set is computed. */
  sessionStatus?: string;
  /** DM (or dev): may edit and confirm the proposed changes. */
  canReview: boolean;
  /** Which action is in flight. */
  busy: "save" | "confirm" | null;
  /** Error reported by the last action (or by the failed apply). */
  error?: string | null;
  onSave: (changes: PlanChangeEdit[], relations: PlanRelationEdit[]) => void;
  /** Save (when needed) and confirm in one go: the parent orders the calls. */
  onConfirm: (changes: PlanChangeEdit[], relations: PlanRelationEdit[]) => void;
}) {
  // Local, unsaved edits: the card renders the server state plus these
  // overrides, so a background poll never discards what the DM is typing.
  const [edits, setEdits] = useState<Record<string, Partial<PlanChange>>>({});
  const [droppedRelations, setDroppedRelations] = useState<Record<string, boolean>>({});
  // One panel per change: read-only (Inspect) or editable (Edit).
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [editing, setEditing] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  // Raw-JSON escape hatch, per change (a draft typed for one change must not
  // show up in the panel of another).
  const [rawDraft, setRawDraft] = useState<Record<string, string>>({});
  const [rawError, setRawError] = useState<string | null>(null);
  // Page-type sections start expanded; the DM collapses what they are done with.
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const merged: PlanChange[] = useMemo(
    () =>
      (plan?.changes ?? []).map((change) =>
        edits[change.id] ? { ...change, ...edits[change.id] } : change,
      ),
    [plan?.changes, edits],
  );

  const dirty = Object.keys(edits).length > 0 || Object.keys(droppedRelations).length > 0;
  const active = merged.filter((c) => !c.dropped);
  const counts = {
    create: active.filter((c) => c.action === "create").length,
    update: active.filter((c) => c.action === "update").length,
    events: active.filter((c) => c.kind === "event" || c.timeline).length,
    dropped: merged.length - active.length,
    relations: (plan?.relations ?? []).filter((r) => !(droppedRelations[r.id] ?? r.dropped))
      .length,
  };

  function patch(change: PlanChange, partial: Partial<PlanChange>) {
    setEdits((prev) => ({ ...prev, [change.id]: { ...prev[change.id], ...partial } }));
  }

  /** Open the editable panel of a change (seeds the field drafts). */
  function startEditing(change: PlanChange) {
    const fields = flattenContent(change.after.content_json ?? {});
    const seeded: Record<string, string> = {};
    for (const [path, value] of Object.entries(fields)) seeded[path] = editableValue(value);
    setDrafts(seeded);
    setEditing(change.id);
    setOpen((prev) => ({ ...prev, [change.id]: true }));
    clearRaw(change.id);
    setRawError(null);
  }

  /** Close the panel of a change (leaving edit mode). */
  function closePanel(change: PlanChange) {
    setOpen((prev) => ({ ...prev, [change.id]: false }));
    if (editing === change.id) setEditing(null);
    clearRaw(change.id);
    setRawError(null);
  }

  /** Drop the raw-JSON draft of one change (back to the field editors). */
  function clearRaw(id: string) {
    setRawDraft((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }

  function saveEdit(change: PlanChange) {
    const raw = rawDraft[change.id];
    if (raw !== undefined && raw !== null) {
      try {
        const parsed = JSON.parse(raw) as Record<string, unknown>;
        patch(change, { after: { ...change.after, content_json: parsed } });
      } catch (err) {
        setRawError(err instanceof Error ? err.message : "invalid JSON");
        return;
      }
      setEditing(null);
      clearRaw(change.id);
      setRawError(null);
      return;
    }
    const previousFields = flattenContent(change.after.content_json ?? {});
    const content: Record<string, unknown> = {};
    for (const [path, value] of Object.entries(previousFields)) {
      setPath(content, path, parseValue(drafts[path] ?? editableValue(value), value));
    }
    patch(change, { after: { ...change.after, content_json: content } });
    setEditing(null);
  }

  /** The review payload: only the fields the DM actually touched. */
  function reviewPayload(): { changes: PlanChangeEdit[]; relations: PlanRelationEdit[] } {
    return {
      changes: Object.entries(edits).map(([id, partial]) => ({
        id,
        ...(partial.title !== undefined ? { title: partial.title } : {}),
        ...(partial.dropped !== undefined ? { dropped: partial.dropped } : {}),
        ...(partial.timeline !== undefined ? { timeline: partial.timeline } : {}),
        ...(partial.after !== undefined
          ? {
              after: {
                content_json: partial.after.content_json,
                visibility: partial.after.visibility,
              },
            }
          : {}),
      })),
      relations: Object.entries(droppedRelations).map(([id, dropped]) => ({ id, dropped })),
    };
  }

  if (!plan) {
    // The set is computed while the session runs 'generating_wiki' and is
    // ready when it parks on 'wiki_plan_ready': the panel says which of the
    // two it is looking at instead of showing an empty review card.
    const generating = sessionStatus === "generating_wiki";
    return (
      <Card>
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-lg font-semibold">Proposed wiki changes</h2>
          {generating ? <Badge tone="amber">computing…</Badge> : null}
        </div>
        <div className="mt-4">
          <EmptyState>
            {generating
              ? "The confirmed summary is being turned into the pages and timeline entries it implies — they appear here as soon as they are ready. Nothing is written to the wiki before you confirm them."
              : "Once you confirm the session summary, the pages and events it implies are proposed here — nothing is written to the wiki before you confirm them."}
          </EmptyState>
        </div>
      </Card>
    );
  }

  const isApplied = plan.status === "applied";
  const isApplying = plan.status === "applying";
  const groups: { kind: WikiPageKind; items: PlanChange[] }[] = [];
  for (const change of merged) {
    const group = groups.find((g) => g.kind === change.kind);
    if (group) group.items.push(change);
    else groups.push({ kind: change.kind, items: [change] });
  }

  return (
    <Card>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-lg font-semibold">Proposed wiki changes</h2>
          {isApplied ? (
            <Badge tone="green">applied</Badge>
          ) : isApplying ? (
            <Badge tone="amber">applying…</Badge>
          ) : (
            <Badge tone="amber">awaiting your confirmation</Badge>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <Badge tone="green">+{counts.create} new</Badge>
          {counts.update ? <Badge tone="amber">~{counts.update} update</Badge> : null}
          {counts.events ? <Badge tone="slate">{counts.events} timeline</Badge> : null}
          {counts.relations ? <Badge tone="slate">{counts.relations} links</Badge> : null}
          {counts.dropped ? <Badge tone="red">{counts.dropped} dropped</Badge> : null}
        </div>
      </div>

      <p className="mb-3 text-xs text-slate-500">
        {isApplied
          ? "These changes are in the wiki now (pages published, timeline entries approved)."
          : "Nothing here is in the wiki yet: inspect each change, edit what is wrong, drop what you do not want, then confirm."}
        {plan.applied_at ? " Applied " + fmtDate(plan.applied_at) + "." : ""}
      </p>

      {plan.error || error ? <Alert tone="error">{plan.error ?? error}</Alert> : null}

      {merged.length === 0 ? (
        <EmptyState>
          No changes: this session adds nothing the wiki does not already have.
        </EmptyState>
      ) : null}

      {groups.map((group) => {
        const isCollapsed = collapsed[group.kind] ?? false;
        const label = PAGE_KIND_LABELS[group.kind] ?? group.kind;
        const groupDropped = group.items.filter((c) => c.dropped).length;
        return (
          <div key={group.kind} className="mt-4">
            <button
              type="button"
              onClick={() => setCollapsed((prev) => ({ ...prev, [group.kind]: !isCollapsed }))}
              aria-expanded={!isCollapsed}
              className="mb-2 flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left transition hover:bg-slate-800/60"
            >
              <Chevron open={!isCollapsed} />
              <h3 className="text-xs font-bold uppercase tracking-wider text-slate-400">{label}</h3>
              <Badge tone="slate">
                {group.items.length - groupDropped}
                {groupDropped ? ` · ${groupDropped} dropped` : ""}
              </Badge>
              <span className="ml-auto text-xs text-slate-500">{isCollapsed ? "Show" : "Hide"}</span>
            </button>

            {isCollapsed ? null : (
              <ul className="space-y-2">
                {group.items.map((change) => {
                  const diffs = change.before ? diffChange(change) : [];
                  const expanded = open[change.id] ?? false;
                  const isEditing = editing === change.id;
                  return (
                    <li
                      key={change.id}
                      className={
                        "rounded-lg border px-3 py-2.5 " +
                        (change.dropped
                          ? "border-slate-800 bg-slate-900/60 opacity-60"
                          : "border-slate-800 bg-slate-800/30")
                      }
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex min-w-0 flex-wrap items-center gap-2">
                          <ActionChip action={change.action} />
                          <span
                            className={
                              "text-sm font-semibold " +
                              (change.dropped ? "text-slate-500 line-through" : "text-slate-100")
                            }
                          >
                            <LinkedText text={change.title} campaignId={campaignId} index={linkIndex} />
                          </span>
                          {change.before ? (
                            <span className="text-xs text-slate-500">
                              {diffs.length} field{diffs.length === 1 ? "" : "s"} changed
                            </span>
                          ) : null}
                          {change.timeline ? <Badge tone="blue">timeline</Badge> : null}
                          {change.after.confidence !== null &&
                          change.after.confidence !== undefined ? (
                            <span className="text-xs text-slate-500">
                              confidence {Math.round(change.after.confidence * 100)}%
                            </span>
                          ) : null}
                        </div>
                        <div className="flex flex-wrap items-center gap-1">
                          <Button
                            variant="ghost"
                            onClick={() => (expanded ? closePanel(change) : setOpen((prev) => ({ ...prev, [change.id]: true })))}
                          >
                            {expanded ? "Hide" : "Inspect"}
                          </Button>
                          {canReview && !isApplied && !isApplying ? (
                            <>
                              <Button
                                variant="ghost"
                                onClick={() => (isEditing ? setEditing(null) : startEditing(change))}
                              >
                                {isEditing ? "Done editing" : "Edit"}
                              </Button>
                              <Button
                                variant="ghost"
                                onClick={() => patch(change, { dropped: !change.dropped })}
                                title={change.dropped ? "Keep this change" : "Drop this change"}
                              >
                                {change.dropped ? "Restore" : "Drop"}
                              </Button>
                            </>
                          ) : null}
                        </div>
                      </div>

                      {expanded ? (
                        <div className="mt-3 space-y-3 border-t border-slate-800 pt-3">
                          {/* Inspect and Edit render the SAME form; Inspect is
                              simply read-only (and, for updates, annotates every
                              field with the value the page holds today). */}
                          <ChangeForm
                            change={change}
                            readOnly={!isEditing}
                            drafts={isEditing ? drafts : NO_DRAFTS}
                            onDraft={(path, value) =>
                              setDrafts((prev) => ({ ...prev, [path]: value }))
                            }
                            rawDraft={rawDraft[change.id] ?? null}
                            onRawDraft={(value) =>
                              setRawDraft((prev) => ({ ...prev, [change.id]: value }))
                            }
                            rawError={rawError}
                            onTitle={(title) => patch(change, { title })}
                            onTimeline={(timeline) => patch(change, { timeline })}
                            onVisibility={(visibility) =>
                              patch(change, { after: { ...change.after, visibility } })
                            }
                          />

                          <div className="flex flex-wrap gap-2">
                            {isEditing ? (
                              <Button onClick={() => saveEdit(change)}>Apply to the proposal</Button>
                            ) : null}
                            <Button
                              variant="ghost"
                              onClick={() => {
                                setRawError(null);
                                if (rawDraft[change.id] !== undefined) {
                                  clearRaw(change.id);
                                  return;
                                }
                                setRawDraft((prev) => ({
                                  ...prev,
                                  [change.id]: JSON.stringify(
                                    change.after.content_json ?? {},
                                    null,
                                    2,
                                  ),
                                }));
                              }}
                            >
                              {rawDraft[change.id] !== undefined
                                ? "Back to fields"
                                : isEditing
                                  ? "Edit raw JSON"
                                  : "View raw JSON"}
                            </Button>
                          </div>
                        </div>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        );
      })}

      {plan.relations.length ? (
        <div className="mt-4">
          <h3 className="mb-2 text-xs font-bold uppercase tracking-wider text-slate-500">
            Links between pages
          </h3>
          <ul className="space-y-1">
            {plan.relations.map((relation) => {
              const dropped = droppedRelations[relation.id] ?? relation.dropped;
              return (
                <li
                  key={relation.id}
                  className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-300"
                >
                  <span className={dropped ? "line-through opacity-60" : ""}>
                    {relation.from_title} → {relation.to_title ?? relation.to_page_id}{" "}
                    <span className="text-slate-500">
                      ({relation.relation_type.replace(/_/g, " ")})
                    </span>
                  </span>
                  {canReview && !isApplied && !isApplying ? (
                    <Button
                      variant="ghost"
                      onClick={() =>
                        setDroppedRelations((prev) => ({ ...prev, [relation.id]: !dropped }))
                      }
                    >
                      {dropped ? "Restore" : "Drop"}
                    </Button>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      {plan.skipped.length ? (
        <div className="mt-4 rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2">
          <h3 className="mb-1 text-xs font-bold uppercase tracking-wider text-slate-500">
            Already in the wiki — not proposed again
          </h3>
          <p className="text-xs text-slate-400">
            {plan.skipped.map((s) => s.title).join(", ")}. Their new facts stay on the session
            summary above; edit those pages directly if needed.
          </p>
        </div>
      ) : null}

      {canReview && !isApplied ? (
        <div className="mt-4 flex flex-wrap items-center justify-end gap-2 border-t border-slate-800 pt-4">
          {dirty ? (
            <Button
              variant="secondary"
              disabled={busy !== null}
              onClick={() => {
                const payload = reviewPayload();
                onSave(payload.changes, payload.relations);
              }}
            >
              {busy === "save" ? "Saving…" : "Save review"}
            </Button>
          ) : null}
          <Button
            variant="danger"
            disabled={busy !== null || isApplying}
            title="Write the proposed pages and timeline entries into the wiki"
            onClick={() => {
              if (
                !window.confirm(
                  "Confirm these changes?\n\n" +
                    counts.create +
                    " new page(s), " +
                    counts.update +
                    " update(s) and " +
                    counts.events +
                    " timeline entr(ies) will be written to the wiki and become visible to the players.",
                )
              ) {
                return;
              }
              const payload = reviewPayload();
              onConfirm(payload.changes, payload.relations);
            }}
          >
            {busy === "confirm" || isApplying
              ? "Writing to the wiki…"
              : "Confirm changes & update the wiki"}
          </Button>
        </div>
      ) : null}
      {canReview && dirty && !isApplied ? (
        <p className="mt-2 text-right text-xs text-amber-300">
          Unsaved edits are saved automatically when you confirm.
        </p>
      ) : null}
    </Card>
  );
}
