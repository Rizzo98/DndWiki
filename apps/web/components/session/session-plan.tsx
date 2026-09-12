// Session plan card: the "git status" of a session.
//
// The confirmed summary is turned into a set of PROPOSED wiki changes — pages
// to create, pages to update and the timeline entries they back. Nothing is in
// the wiki yet: this card is where the DM inspects each change (creations as a
// preview, updates as a field-by-field diff against what the wiki already
// documents), edits or drops the ones they disagree with, and confirms the
// whole set. Only then does the pipeline write the pages (published) and the
// timeline entries (approved).

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

// ------------------------------------------------------------- component

function ActionChip({ action }: { action: "create" | "update" }) {
  return action === "create" ? (
    <Badge tone="green">+ new</Badge>
  ) : (
    <Badge tone="amber">~ update</Badge>
  );
}

export function SessionPlanCard({
  plan,
  campaignId,
  linkIndex,
  canReview,
  busy,
  error,
  onSave,
  onConfirm,
}: {
  plan: SessionPlan | null;
  campaignId: string;
  linkIndex?: LinkIndex | null;
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
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [editing, setEditing] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [rawDraft, setRawDraft] = useState<string | null>(null);
  const [rawError, setRawError] = useState<string | null>(null);

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

  function startEditing(change: PlanChange) {
    const fields = flattenContent(change.after.content_json ?? {});
    const seeded: Record<string, string> = {};
    for (const [path, value] of Object.entries(fields)) seeded[path] = editableValue(value);
    setDrafts(seeded);
    setEditing(change.id);
    setRawDraft(null);
    setRawError(null);
  }

  function saveEdit(change: PlanChange) {
    if (rawDraft !== null) {
      try {
        const parsed = JSON.parse(rawDraft) as Record<string, unknown>;
        patch(change, { after: { ...change.after, content_json: parsed } });
      } catch (err) {
        setRawError(err instanceof Error ? err.message : "invalid JSON");
        return;
      }
      setEditing(null);
      setRawDraft(null);
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
    return (
      <Card>
        <h2 className="text-lg font-semibold">Proposed wiki changes</h2>
        <div className="mt-4">
          <EmptyState>
            Once you confirm the session summary, the pages and events it implies are proposed
            here — nothing is written to the wiki before you confirm them.
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

      {groups.map((group) => (
        <div key={group.kind} className="mt-4">
          <h3 className="mb-2 text-xs font-bold uppercase tracking-wider text-slate-500">
            {PAGE_KIND_LABELS[group.kind] ?? group.kind}
          </h3>
          <ul className="space-y-2">
            {group.items.map((change) => {
              const diffs = change.before ? diffChange(change) : [];
              const expanded = open[change.id] ?? false;
              const isEditing = editing === change.id;
              const fields = flattenContent(change.after.content_json ?? {});
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
                      <Badge tone="slate">{PAGE_KIND_TITLES[change.kind] ?? change.kind}</Badge>
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
                        onClick={() => setOpen((prev) => ({ ...prev, [change.id]: !expanded }))}
                      >
                        {expanded ? "Hide" : "Inspect"}
                      </Button>
                      {canReview && !isApplied && !isApplying ? (
                        <>
                          <Button
                            variant="ghost"
                            onClick={() => (isEditing ? setEditing(null) : startEditing(change))}
                          >
                            {isEditing ? "Cancel" : "Edit"}
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
                    <div className="mt-3 space-y-2 border-t border-slate-800 pt-3">
                      {isEditing ? (
                        <div className="space-y-3">
                          <Field label="Title">
                            <TextInput
                              value={change.title}
                              onChange={(e) => patch(change, { title: e.target.value })}
                            />
                          </Field>
                          {rawDraft !== null ? (
                            <Field
                              label="content_json"
                              hint="Raw JSON — the escape hatch for anything the fields do not cover."
                            >
                              <TextArea
                                rows={12}
                                className="font-mono text-xs"
                                value={rawDraft}
                                onChange={(e) => setRawDraft(e.target.value)}
                              />
                            </Field>
                          ) : (
                            <div className="grid gap-3 sm:grid-cols-2">
                              {Object.entries(fields).map(([path, value]) => (
                                <Field key={path} label={fieldLabel(path)}>
                                  {Array.isArray(value) ||
                                  editableValue(value).length > 60 ? (
                                    <TextArea
                                      rows={3}
                                      value={drafts[path] ?? editableValue(value)}
                                      onChange={(e) =>
                                        setDrafts((prev) => ({
                                          ...prev,
                                          [path]: e.target.value,
                                        }))
                                      }
                                    />
                                  ) : (
                                    <TextInput
                                      value={drafts[path] ?? editableValue(value)}
                                      onChange={(e) =>
                                        setDrafts((prev) => ({
                                          ...prev,
                                          [path]: e.target.value,
                                        }))
                                      }
                                    />
                                  )}
                                </Field>
                              ))}
                            </div>
                          )}
                          {rawError ? <Alert tone="error">{rawError}</Alert> : null}
                          {change.timeline ? (
                            <div className="grid gap-3 sm:grid-cols-2">
                              <Field label="Timeline entry">
                                <TextArea
                                  rows={2}
                                  value={change.timeline.summary}
                                  onChange={(e) =>
                                    patch(change, {
                                      timeline: {
                                        summary: e.target.value,
                                        in_world_date: change.timeline?.in_world_date ?? null,
                                      },
                                    })
                                  }
                                />
                              </Field>
                              <Field label="In-world date">
                                <TextInput
                                  value={change.timeline.in_world_date ?? ""}
                                  onChange={(e) =>
                                    patch(change, {
                                      timeline: {
                                        summary: change.timeline?.summary ?? "",
                                        in_world_date: e.target.value || null,
                                      },
                                    })
                                  }
                                />
                              </Field>
                            </div>
                          ) : null}
                          <Field label="Visibility">
                            <Select
                              className="w-48"
                              value={change.after.visibility}
                              onChange={(e) =>
                                patch(change, {
                                  after: {
                                    ...change.after,
                                    visibility: e.target.value as WikiVisibility,
                                  },
                                })
                              }
                            >
                              <option value="public">public</option>
                              <option value="dm_only">dm only</option>
                              <option value="hidden">hidden</option>
                            </Select>
                          </Field>
                          <div className="flex flex-wrap gap-2">
                            <Button onClick={() => saveEdit(change)}>
                              Apply to the proposal
                            </Button>
                            <Button
                              variant="ghost"
                              onClick={() => {
                                if (rawDraft === null) {
                                  setRawDraft(
                                    JSON.stringify(change.after.content_json ?? {}, null, 2),
                                  );
                                  setRawError(null);
                                } else {
                                  setRawDraft(null);
                                }
                              }}
                            >
                              {rawDraft === null ? "Edit raw JSON" : "Back to fields"}
                            </Button>
                          </div>
                        </div>
                      ) : change.before ? (
                        <ul className="space-y-1.5">
                          {diffs.length === 0 ? (
                            <li className="text-xs text-slate-500">
                              No field changes — only the timeline entry below.
                            </li>
                          ) : null}
                          {diffs.map((diff) => (
                            <li key={diff.path} className="text-xs">
                              <span className="font-semibold text-slate-400">{diff.label}</span>
                              <div className="mt-0.5 grid gap-1 sm:grid-cols-2">
                                <span className="rounded bg-red-500/10 px-2 py-1 text-red-300 line-through">
                                  {displayValue(diff.before) || "—"}
                                </span>
                                <span className="rounded bg-green-500/10 px-2 py-1 text-green-200">
                                  {displayValue(diff.after) || "—"}
                                </span>
                              </div>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <dl className="space-y-1.5">
                          {Object.entries(fields).map(([path, value]) => (
                            <div key={path} className="text-xs">
                              <dt className="font-semibold text-slate-400">{fieldLabel(path)}</dt>
                              <dd className="text-slate-200">
                                <LinkedText
                                  text={displayValue(value)}
                                  campaignId={campaignId}
                                  index={linkIndex}
                                />
                              </dd>
                            </div>
                          ))}
                        </dl>
                      )}

                      {change.timeline && !isEditing ? (
                        <div className="rounded-lg bg-slate-900/60 px-2 py-1.5 text-xs text-slate-300">
                          <span className="font-semibold text-slate-400">Timeline</span>{" "}
                          {change.timeline.in_world_date ? (
                            <span className="text-slate-500">
                              {change.timeline.in_world_date} ·{" "}
                            </span>
                          ) : null}
                          {change.timeline.summary}
                        </div>
                      ) : null}
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </div>
      ))}

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
