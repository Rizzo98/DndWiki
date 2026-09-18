// Session plan: the "git status" of a session, as a stack of panels.
//
// The confirmed summary is turned into a set of PROPOSED wiki changes — pages
// to create, pages to update, the timeline entries they back and the links
// between them. Nothing is in the wiki yet: every proposed page gets its own
// panel (never a row inside one shared box), where the DM reads the page
// exactly as it will be written (Inspect), fixes what is wrong field by field
// (Edit) or drops it. Confirming the set is the only thing that writes pages
// (published) and timeline entries (approved) into the wiki.

"use client";

import { useMemo, useState } from "react";
import { Alert } from "@/components/ui";
import {
  RlButton,
  RlEmptyState,
  RlIcon,
  RlIconChip,
  RlPanel,
  RlPanelHead,
  RlStat,
  RlStatRow,
  RlTag,
} from "@/components/ravenlore";
import { normalizeName, type LinkIndex } from "@/components/linked-text";
import {
  PlanChangePanel,
  draftOf,
  type ChangeDraft,
} from "@/components/session/plan-change-panel";
import {
  PlanRelationsPanel,
  endpointResolves,
  type PlanEndpoint,
} from "@/components/session/plan-relations-panel";
import { PAGE_KIND_ICON, PAGE_KIND_TINT } from "@/lib/page-kinds";
import { pruneContent } from "@/lib/page-fields";
import {
  PAGE_KINDS,
  PAGE_KIND_LABELS,
  type PageSummary,
  type PlanChange,
  type PlanChangeEdit,
  type PlanRelation,
  type PlanRelationEdit,
  type SessionPlan,
  type WikiPageKind,
} from "@/lib/api";

/** "1 new page" / "3 new pages". */
function plural(count: number, one: string, many: string): string {
  return count + " " + noun(count, one, many);
}

/** Just the noun, for a label that already shows the number. */
function noun(count: number, one: string, many: string): string {
  return count === 1 ? one : many;
}

/** "a, b and c". */
function joinList(parts: string[]): string {
  if (parts.length <= 1) return parts.join("");
  return parts.slice(0, -1).join(", ") + " and " + parts[parts.length - 1];
}

export function SessionPlanCard({
  plan,
  campaignId,
  linkIndex,
  pages = [],
  sessionNames,
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
  /** The campaign's pages: the "#" autocomplete and the relation resolver. */
  pages?: PageSummary[];
  /** session id -> display name, for the session references of a preview. */
  sessionNames?: Record<string, string>;
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
  // Local, unsaved edits: the panels render the server state plus these
  // overrides, so a background poll never discards what the DM is typing.
  const [edits, setEdits] = useState<Record<string, Partial<PlanChange>>>({});
  const [droppedRelations, setDroppedRelations] = useState<Record<string, boolean>>({});
  // One panel per change: opened on demand (a session proposes a lot of pages).
  const [open, setOpen] = useState<Record<string, boolean>>({});
  // The macro-groups (Characters, Locations, Events) fold too. An entry is the
  // DM's explicit choice; without one the group follows the review: open while
  // the set awaits them, folded once it is confirmed (see `groupOpen`).
  const [groupOverrides, setGroupOverrides] = useState<Record<string, boolean>>({});
  // The change being edited and the draft that "Apply to the proposal" writes.
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<ChangeDraft | null>(null);

  const merged: PlanChange[] = useMemo(
    () =>
      (plan?.changes ?? []).map((change) =>
        edits[change.id] ? { ...change, ...edits[change.id] } : change,
      ),
    [plan?.changes, edits],
  );

  const relationDropped = (relation: PlanRelation) =>
    droppedRelations[relation.id] ?? relation.dropped;
  const activeRelations = (plan?.relations ?? []).filter((r) => !relationDropped(r));

  // Where a relation's title lands: a page of this change set, a page the
  // campaign already has, or nothing at all (the link is then skipped).
  const changeByTitle = useMemo(() => {
    const map = new Map<string, { kind: WikiPageKind; dropped: boolean }>();
    for (const change of merged) {
      map.set(normalizeName(change.title), { kind: change.kind, dropped: change.dropped });
    }
    return map;
  }, [merged]);

  const pageByTitle = useMemo(() => {
    const map = new Map<string, { kind: WikiPageKind }>();
    for (const page of pages) map.set(normalizeName(page.title), { kind: page.kind });
    return map;
  }, [pages]);

  function resolve(title: string | null, pageId?: string | null): PlanEndpoint {
    if (!title) {
      const page = pages.find((candidate) => candidate.id === pageId);
      return page
        ? { title: page.title, status: "existing", kind: page.kind }
        : { title: pageId ? pageId.slice(0, 8) : "—", status: "missing", kind: null };
    }
    const key = normalizeName(title);
    const proposed = changeByTitle.get(key);
    if (proposed) {
      return { title, status: proposed.dropped ? "dropped" : "proposed", kind: proposed.kind };
    }
    const existing = pageByTitle.get(key);
    if (existing) return { title, status: "existing", kind: existing.kind };
    return { title, status: "missing", kind: null };
  }

  // Links only land when both ends find a page: the relations panel says so
  // per row, and the confirm summary counts what will actually be written.
  const landedRelations = activeRelations.filter(
    (relation) =>
      endpointResolves(resolve(relation.from_title)) &&
      endpointResolves(resolve(relation.to_title, relation.to_page_id)),
  );

  const dirty = Object.keys(edits).length > 0 || Object.keys(droppedRelations).length > 0;
  const active = merged.filter((change) => !change.dropped);
  const counts = {
    create: active.filter((change) => change.action === "create").length,
    update: active.filter((change) => change.action === "update").length,
    events: active.filter((change) => change.kind === "event" || change.timeline).length,
    dropped: merged.length - active.length,
    relations: activeRelations.length,
  };

  // One section per page kind, in the wiki's own category order.
  const groups: { kind: WikiPageKind; items: PlanChange[] }[] = [];
  for (const kind of [...PAGE_KINDS, ...merged.map((c) => c.kind)]) {
    const items = merged.filter((change) => change.kind === kind);
    if (items.length === 0 || groups.some((group) => group.kind === kind)) continue;
    groups.push({ kind, items });
  }

  // A confirmed set is a record, not a review: its groups start folded, so the
  // session page shows what was written and opens a category when asked. While
  // the set is still the DM's to decide, the categories are open.
  const settled =
    plan?.status === "applying" || plan?.status === "applied" || Boolean(plan?.confirmed_at);
  const groupOpen = (kind: WikiPageKind) => groupOverrides[kind] ?? !settled;

  const allExpanded =
    groups.length > 0 &&
    groups.every((group) => groupOpen(group.kind)) &&
    merged.every((change) => open[change.id]);

  // The confirm summary lists only what there is, and only what lands.
  const written: string[] = [];
  if (counts.create > 0) written.push(plural(counts.create, "new page", "new pages"));
  if (counts.update > 0) written.push(plural(counts.update, "update", "updates"));
  if (counts.events > 0) written.push(plural(counts.events, "timeline entry", "timeline entries"));
  if (landedRelations.length > 0) {
    written.push(plural(landedRelations.length, "link", "links"));
  }
  const skippedLinks = counts.relations - landedRelations.length;

  /** One sentence, used both in the panel and in the confirmation dialog. */
  const confirmSummary =
    written.length === 0
      ? "Nothing will be written to the wiki."
      : joinList(written) + " will be written to the wiki and become visible to the players.";

  function patch(change: PlanChange, partial: Partial<PlanChange>) {
    setEdits((prev) => ({ ...prev, [change.id]: { ...prev[change.id], ...partial } }));
  }

  function startEditing(change: PlanChange) {
    setEditing(change.id);
    setDraft(draftOf(change));
    setOpen((prev) => ({ ...prev, [change.id]: true }));
  }

  function cancelEditing() {
    setEditing(null);
    setDraft(null);
  }

  /** Move the draft into the proposal (still unsaved until the DM confirms). */
  function applyDraft(change: PlanChange) {
    if (!draft) return;
    patch(change, {
      title: draft.title,
      after: {
        ...change.after,
        content_json: pruneContent(draft.content),
        visibility: draft.visibility,
      },
      timeline: draft.timeline,
    });
    cancelEditing();
  }

  function toggleAll() {
    const next: Record<string, boolean> = {};
    for (const change of merged) next[change.id] = !allExpanded;
    setOpen(next);
    const nextGroups: Record<string, boolean> = {};
    for (const group of groups) nextGroups[group.kind] = !allExpanded;
    setGroupOverrides(nextGroups);
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
    // two it is looking at instead of showing an empty review.
    const generating = sessionStatus === "generating_wiki";
    return (
      <RlPanel>
        <RlPanelHead
          eyebrow="Session review"
          action={generating ? <RlTag tint="item">computing…</RlTag> : undefined}
        />
        <div className="border-t border-[color:var(--rl-border-parchment)] px-4 pb-4 pt-5">
          <RlEmptyState icon="book" tint="muted" title="No proposed changes yet">
            {generating
              ? "The confirmed summary is being turned into the pages and timeline entries it implies — they appear here as soon as they are ready. Nothing is written to the wiki before you confirm them."
              : "Once you confirm the session summary, the pages and events it implies are proposed here — nothing is written to the wiki before you confirm them."}
          </RlEmptyState>
        </div>
      </RlPanel>
    );
  }

  const isApplied = plan.status === "applied";
  const isApplying = plan.status === "applying";
  const locked = !canReview || isApplied || isApplying;

  return (
    <div className="space-y-4">
      {/* What the set is, how big it is, and what the DM is looking at. */}
      <RlPanel>
        <div className="space-y-4 px-4 py-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <span className="rl-eyebrow">Session review</span>
              <h2 className="rl-title mt-1 text-[22px]">Proposed wiki changes</h2>
              <p className="rl-body mt-1 max-w-[75ch]">
                {isApplied
                  ? "These changes are in the wiki now: pages published, timeline entries approved."
                  : "Nothing here is in the wiki yet. Open a page to read it as it will be written, edit what is wrong, drop what you do not want, then confirm the set."}
                {plan.applied_at ? " Applied " + new Date(plan.applied_at).toLocaleString() + "." : ""}
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {isApplied ? (
                <RlTag tint="neutral">applied</RlTag>
              ) : isApplying ? (
                <RlTag tint="item">writing…</RlTag>
              ) : (
                <RlTag tint="npc">awaiting your confirmation</RlTag>
              )}
              {merged.length > 0 ? (
                <RlButton variant="outline" size="sm" onClick={toggleAll}>
                  {allExpanded ? "Collapse all" : "Expand all"}
                </RlButton>
              ) : null}
            </div>
          </div>

          <RlStatRow>
            <RlStat
              icon="plus"
              tint="neutral"
              value={counts.create}
              label={noun(counts.create, "new page", "new pages")}
            />
            <RlStat
              icon="spark"
              tint="item"
              value={counts.update}
              label={noun(counts.update, "page updated", "pages updated")}
            />
            <RlStat
              icon="clock"
              tint="ink"
              value={counts.events}
              label={noun(counts.events, "timeline entry", "timeline entries")}
            />
            <RlStat
              icon="arrow"
              tint="place"
              value={counts.relations}
              label={noun(counts.relations, "link between pages", "links between pages")}
            />
          </RlStatRow>

          {plan.error || error ? <Alert tone="error">{plan.error ?? error}</Alert> : null}
          {counts.dropped > 0 && !isApplied ? (
            <p className="rl-card-meta">
              {plural(counts.dropped, "change is", "changes are")} dropped and will not be
              written.
            </p>
          ) : null}
        </div>
      </RlPanel>

      {merged.length === 0 ? (
        <RlPanel>
          <div className="px-4 pb-4 pt-5">
            <RlEmptyState icon="spark" tint="neutral" title="Nothing to write">
              This session adds nothing the wiki does not already have.
            </RlEmptyState>
          </div>
        </RlPanel>
      ) : null}

      {/* One panel per proposed page, grouped by wiki category. */}
      {groups.map((group) => {
        const droppedCount = group.items.filter((change) => change.dropped).length;
        const isOpen = groupOpen(group.kind);
        return (
          <section key={group.kind} className="space-y-3">
            {/* The category head IS the fold: it says what the category holds
                and opens or closes its panels (a heading that contains its own
                toggle, so the outline and the control stay one thing). */}
            <h3>
              <button
                type="button"
                onClick={() =>
                  setGroupOverrides((prev) => ({ ...prev, [group.kind]: !isOpen }))
                }
                aria-expanded={isOpen}
                className="flex w-full flex-wrap items-center gap-2.5 rounded-[var(--rl-radius-sm)] px-2 py-2 text-left transition hover:bg-[color:var(--rl-bg-parchment-sunk)]"
              >
                <RlIcon
                  name="chevron"
                  size={14}
                  className={
                    "shrink-0 text-[color:var(--rl-text-on-parchment-muted)] transition-transform " +
                    (isOpen ? "rotate-90" : "")
                  }
                />
                <RlIconChip
                  name={PAGE_KIND_ICON[group.kind] ?? "book"}
                  tint={PAGE_KIND_TINT[group.kind] ?? "muted"}
                  size={30}
                  iconSize={16}
                />
                <span className="rl-title text-lg">
                  {PAGE_KIND_LABELS[group.kind] ?? group.kind}
                </span>
                <span className="rl-card-meta">
                  {plural(group.items.length - droppedCount, "page", "pages")} to write
                  {droppedCount > 0 ? " · " + droppedCount + " dropped" : ""}
                </span>
                <span className="rl-card-meta ml-auto">{isOpen ? "Hide" : "Show"}</span>
              </button>
            </h3>
            {!isOpen
              ? null
              : group.items.map((change) => (
              <PlanChangePanel
                key={change.id}
                change={change}
                campaignId={campaignId}
                linkIndex={linkIndex}
                pages={pages}
                sessionNames={sessionNames}
                canReview={canReview}
                applied={isApplied || isApplying}
                open={open[change.id] ?? false}
                editing={editing === change.id}
                draft={editing === change.id ? draft : null}
                onToggle={() =>
                  setOpen((prev) => ({ ...prev, [change.id]: !(prev[change.id] ?? false) }))
                }
                onStartEdit={() => startEditing(change)}
                onCancelEdit={cancelEditing}
                onDraftChange={setDraft}
                onApplyDraft={() => applyDraft(change)}
                onDrop={() => patch(change, { dropped: !change.dropped })}
              />
              ))}
          </section>
        );
      })}

      {(plan.relations ?? []).length > 0 ? (
        <PlanRelationsPanel
          relations={plan.relations}
          isDropped={relationDropped}
          resolve={resolve}
          canReview={canReview}
          applied={isApplied || isApplying}
          onToggleDropped={(relation, dropped) =>
            setDroppedRelations((prev) => ({ ...prev, [relation.id]: dropped }))
          }
        />
      ) : null}

      {plan.skipped.length > 0 ? (
        <RlPanel>
          <RlPanelHead
            eyebrow="Already in the wiki"
            meta={plural(plan.skipped.length, "entity", "entities") + " — not proposed again"}
          />
          <p className="rl-body border-t border-[color:var(--rl-border-parchment)] px-4 py-3">
            The campaign documents these already, so the session only adds to their pages
            through the session references above. Edit them directly if needed.
          </p>
          <ul>
            {plan.skipped.map((skipped) => (
              <li
                key={skipped.title}
                className="flex flex-wrap items-center gap-2 border-t border-[color:var(--rl-border-parchment)] px-4 py-2.5"
              >
                <RlIconChip
                  name={PAGE_KIND_ICON[skipped.kind as WikiPageKind] ?? "book"}
                  tint={PAGE_KIND_TINT[skipped.kind as WikiPageKind] ?? "muted"}
                  size={26}
                  iconSize={14}
                />
                <span className="text-sm font-semibold text-[color:var(--rl-text-on-parchment-primary)]">
                  {skipped.title}
                </span>
                <span className="rl-card-meta">{skipped.reason}</span>
                {skipped.matched_title && skipped.matched_title !== skipped.title ? (
                  <span className="rl-card-meta">matched “{skipped.matched_title}”</span>
                ) : null}
              </li>
            ))}
          </ul>
        </RlPanel>
      ) : null}

      {canReview && !isApplied && !isApplying ? (
        <RlPanel>
          <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-4">
            <div className="min-w-0">
              <span className="rl-eyebrow">Confirm</span>
              <p className="rl-body mt-1 max-w-[70ch]">
                {written.length === 0
                  ? "There is nothing left to write: every proposed change is dropped."
                  : confirmSummary}
              </p>
              {skippedLinks > 0 ? (
                <p className="rl-card-meta mt-1">
                  {plural(skippedLinks, "link", "links")} will be skipped: no page to link to.
                </p>
              ) : null}
              {dirty ? (
                <p className="rl-card-meta mt-1">
                  Unsaved edits are saved automatically when you confirm.
                </p>
              ) : null}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {dirty ? (
                <RlButton
                  variant="outline"
                  disabled={busy !== null}
                  onClick={() => {
                    const payload = reviewPayload();
                    onSave(payload.changes, payload.relations);
                  }}
                >
                  {busy === "save" ? "Saving…" : "Save review"}
                </RlButton>
              ) : null}
              <RlButton
                variant="primary"
                disabled={busy !== null}
                onClick={() => {
                  if (!window.confirm("Confirm these changes?\n\n" + confirmSummary)) {
                    return;
                  }
                  const payload = reviewPayload();
                  onConfirm(payload.changes, payload.relations);
                }}
              >
                {busy === "confirm" ? "Writing to the wiki…" : "Confirm changes & update the wiki"}
              </RlButton>
            </div>
          </div>
        </RlPanel>
      ) : null}
    </div>
  );
}
