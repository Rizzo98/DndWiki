// PlanChangePanel — ONE proposed wiki page, as its own panel.
//
// A change set is a list of pages to create or update, so the review is a list
// of panels rather than rows inside one box: each panel carries the page's
// sigil, title and what kind of change it is in its head, and its own body
// where the DM either reads the page exactly as it will be written (Inspect)
// or fixes it field by field (Edit). Updates additionally show the fields the
// page says today next to the ones the change set would write.

"use client";

import { useEffect, useState } from "react";
import { Alert, Field, Select, TextInput, TextArea, VisibilityBadge } from "@/components/ui";
import { PageContent } from "@/components/page-content";
import { PageDraftForm, type DraftContent } from "@/components/page-draft-form";
import { RlButton, RlButtonLink, RlIcon, RlIconChip, RlPanel, RlTag } from "@/components/ravenlore";
import type { LinkIndex } from "@/components/linked-text";
import type { PageLinkSuggestion } from "@/components/page-link-editor";
import { PAGE_KIND_ICON, PAGE_KIND_TINT } from "@/lib/page-kinds";
import { PAGE_KIND_TITLES, type PlanChange, type WikiVisibility } from "@/lib/api";
import { diffChange, displayValue, type FieldDiff } from "@/lib/page-fields";

/** The editable shape of one change: what "Apply to the proposal" writes. */
export interface ChangeDraft {
  title: string;
  content: DraftContent;
  visibility: WikiVisibility;
  timeline: { summary: string; in_world_date: string | null } | null;
}

/** The draft a change starts editing from (the proposal as it stands). */
export function draftOf(change: PlanChange): ChangeDraft {
  return {
    title: change.title,
    content: change.after.content_json ?? {},
    visibility: change.after.visibility,
    timeline: change.timeline,
  };
}

export function PlanChangePanel({
  change,
  campaignId,
  linkIndex,
  pages,
  sessionNames,
  canReview,
  applied,
  open,
  editing,
  draft,
  onToggle,
  onStartEdit,
  onCancelEdit,
  onDraftChange,
  onApplyDraft,
  onDrop,
}: {
  change: PlanChange;
  campaignId: string;
  linkIndex?: LinkIndex | null;
  pages: PageLinkSuggestion[];
  sessionNames?: Record<string, string>;
  /** DM (or dev) with a plan that is not written yet. */
  canReview: boolean;
  applied: boolean;
  open: boolean;
  editing: boolean;
  draft: ChangeDraft | null;
  onToggle: () => void;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  onDraftChange: (draft: ChangeDraft) => void;
  onApplyDraft: () => void;
  onDrop: () => void;
}) {
  // Raw JSON is the escape hatch for anything the field editors do not cover.
  const [rawOpen, setRawOpen] = useState(false);
  const [raw, setRaw] = useState("");
  const [rawError, setRawError] = useState<string | null>(null);

  // Leaving edit mode (apply, cancel, drop) closes the escape hatch: it must
  // never reopen on the next edit with the previous draft's text in it.
  useEffect(() => {
    if (!editing) {
      setRawOpen(false);
      setRawError(null);
    }
  }, [editing]);

  const kind = change.kind;
  const tint = PAGE_KIND_TINT[kind] ?? "muted";
  const diffs: FieldDiff[] = change.before ? diffChange(change) : [];
  const confidence = change.after.confidence;
  const facts = Array.isArray(change.after.content_json?.facts)
    ? (change.after.content_json.facts as unknown[]).length
    : 0;
  const meta = [
    facts > 0 ? facts + (facts === 1 ? " fact" : " facts") : null,
    change.before
      ? diffs.length === 0
        ? "no field changes"
        : diffs.length + (diffs.length === 1 ? " field changes" : " fields change")
      : null,
    confidence !== null && confidence !== undefined
      ? Math.round(confidence * 100) + "% confidence"
      : null,
  ]
    .filter(Boolean)
    .join(" · ");

  function openRaw() {
    setRaw(JSON.stringify(draft?.content ?? change.after.content_json ?? {}, null, 2));
    setRawError(null);
    setRawOpen(true);
  }

  function applyRaw() {
    if (!draft) return;
    try {
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      onDraftChange({ ...draft, content: parsed });
      setRawOpen(false);
      setRawError(null);
    } catch (err) {
      setRawError(err instanceof Error ? err.message : "invalid JSON");
    }
  }

  return (
    <RlPanel className={change.dropped ? "border-dashed" : ""}>
      <header className="flex flex-wrap items-start gap-3 px-4 py-3.5">
        <RlIconChip name={PAGE_KIND_ICON[kind] ?? "book"} tint={tint} size={38} iconSize={19} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="rl-eyebrow">{PAGE_KIND_TITLES[kind] ?? kind}</span>
            {change.action === "create" ? (
              <RlTag tint="neutral">+ new page</RlTag>
            ) : (
              <RlTag tint="item">~ update</RlTag>
            )}
            {change.timeline ? <RlTag tint="ink">timeline</RlTag> : null}
            {change.dropped ? <RlTag tint="villain">dropped</RlTag> : null}
          </div>
          <h3
            className={
              "rl-title mt-0.5 text-lg " +
              (change.dropped ? "text-[color:var(--rl-text-on-parchment-muted)] line-through" : "")
            }
          >
            {change.title}
          </h3>
          {meta ? <p className="rl-card-meta mt-0.5">{meta}</p> : null}
        </div>
        <div className="flex flex-wrap items-center gap-1">
          {applied && change.page_id ? (
            <RlButtonLink
              href={"/campaigns/" + campaignId + "/pages/" + change.page_id}
              variant="ghost"
              size="sm"
            >
              Open page
            </RlButtonLink>
          ) : null}
          <RlButton variant="ghost" size="sm" onClick={onToggle} aria-expanded={open}>
            <RlIcon name="chevron" size={14} className={open ? "rotate-90" : ""} />
            {open ? "Hide" : "Inspect"}
          </RlButton>
          {canReview && !applied ? (
            <>
              <RlButton
                variant={editing ? "outline" : "ghost"}
                size="sm"
                onClick={editing ? onCancelEdit : onStartEdit}
              >
                {editing ? "Discard edits" : "Edit"}
              </RlButton>
              <RlButton variant="ghost" size="sm" onClick={onDrop}>
                {change.dropped ? "Restore" : "Drop"}
              </RlButton>
            </>
          ) : null}
        </div>
      </header>

      {open ? (
        <div className="space-y-5 border-t border-[color:var(--rl-border-parchment)] px-4 py-4">
          {change.dropped ? (
            <Alert tone="info">
              Dropped — this page is left out of the change set. Restore it to write it again.
            </Alert>
          ) : null}

          {editing && draft ? (
            <div className="space-y-5">
              <Field label="Title">
                <TextInput
                  value={draft.title}
                  onChange={(e) => onDraftChange({ ...draft, title: e.target.value })}
                />
              </Field>

              {rawOpen ? (
                <Field
                  label="content_json"
                  hint="The raw page content — the escape hatch for anything the fields do not cover."
                >
                  <TextArea
                    rows={14}
                    className="font-mono text-xs"
                    value={raw}
                    onChange={(e) => setRaw(e.target.value)}
                  />
                </Field>
              ) : (
                <PageDraftForm
                  kind={kind}
                  content={draft.content}
                  onChange={(content) => onDraftChange({ ...draft, content })}
                  pages={pages}
                  excludePageId={change.page_id}
                  sessionNames={sessionNames}
                />
              )}

              {draft.timeline ? (
                <TimelineEntryEditor
                  value={draft.timeline}
                  onChange={(timeline) => onDraftChange({ ...draft, timeline })}
                />
              ) : null}

              <Field label="Visibility" hint="Who can read this page once it is written.">
                <Select
                  className="w-48"
                  value={draft.visibility}
                  onChange={(e) =>
                    onDraftChange({ ...draft, visibility: e.target.value as WikiVisibility })
                  }
                >
                  <option value="public">public</option>
                  <option value="dm_only">dm only</option>
                  <option value="hidden">hidden</option>
                </Select>
              </Field>

              {rawError ? <Alert tone="error">{rawError}</Alert> : null}

              <div className="flex flex-wrap items-center gap-2 border-t border-[color:var(--rl-border-parchment)] pt-3">
                <RlButton variant="primary" onClick={rawOpen ? applyRaw : onApplyDraft}>
                  Apply to the proposal
                </RlButton>
                <RlButton variant="outline" size="sm" onClick={rawOpen ? () => setRawOpen(false) : openRaw}>
                  {rawOpen ? "Back to fields" : "Edit raw JSON"}
                </RlButton>
                <RlButton variant="ghost" size="sm" onClick={onCancelEdit}>
                  Discard edits
                </RlButton>
              </div>
            </div>
          ) : (
            <div className="space-y-5">
              <div className="space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="rl-eyebrow">
                    {change.before ? "The page as it will read" : "The new page"}
                  </span>
                  <VisibilityBadge visibility={change.after.visibility} />
                </div>
                <PageContent
                  content={change.after.content_json ?? {}}
                  campaignId={campaignId}
                  kind={kind}
                  sessionNames={sessionNames}
                  linkIndex={linkIndex}
                  currentPageId={change.page_id}
                  hidePortrait
                />
              </div>

              {change.timeline ? (
                <TimelineEntryBlock timeline={change.timeline} />
              ) : null}

              {diffs.length > 0 ? <ChangeDiff diffs={diffs} /> : null}

              {canReview && !applied ? (
                <p className="rl-card-meta">
                  Edit to change what this page will say, or Drop to leave it out of the change
                  set.
                </p>
              ) : null}
            </div>
          )}
        </div>
      ) : null}
    </RlPanel>
  );
}

// ------------------------------------------------------------------- pieces

/** The timeline entry an event page backs — written with it, approved later. */
function TimelineEntryBlock({
  timeline,
}: {
  timeline: { summary: string; in_world_date: string | null };
}) {
  return (
    <div className="space-y-2">
      <span className="rl-eyebrow">Timeline entry</span>
      <div className="space-y-1 rounded-[var(--rl-radius-sm)] border border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-bg-parchment-sunk)] px-3 py-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <RlIcon name="clock" size={14} className="rl-text-ink" />
          <span className="text-xs font-semibold text-[color:var(--rl-text-on-parchment-primary)]">
            {timeline.in_world_date || "no in-world date"}
          </span>
          <RlTag tint="neutral">approved when written</RlTag>
        </div>
        <p className="text-sm text-[color:var(--rl-text-on-parchment-primary)]">
          {timeline.summary}
        </p>
      </div>
    </div>
  );
}

function TimelineEntryEditor({
  value,
  onChange,
}: {
  value: { summary: string; in_world_date: string | null };
  onChange: (next: { summary: string; in_world_date: string | null }) => void;
}) {
  return (
    <section className="space-y-3">
      <span className="rl-eyebrow">Timeline entry</span>
      <Field label="What the timeline says">
        <TextArea
          rows={2}
          value={value.summary}
          onChange={(e) => onChange({ ...value, summary: e.target.value })}
        />
      </Field>
      <Field label="In-world date" hint="Mirrored on the campaign timeline.">
        <TextInput
          className="w-64"
          value={value.in_world_date ?? ""}
          placeholder="17 Ches 1492 DR"
          onChange={(e) => onChange({ ...value, in_world_date: e.target.value || null })}
        />
      </Field>
    </section>
  );
}

/**
 * A diff row's value. Session references are a list of objects (one per
 * session, each with its facts): rendered raw they would fill the row with
 * JSON, so they get a one-line reading instead.
 */
function diffValue(path: string, value: unknown): string {
  if (path === "session_references" && Array.isArray(value)) {
    const facts = value.reduce<number>(
      (total, ref) =>
        total +
        (typeof ref === "object" && ref !== null && Array.isArray((ref as { facts?: unknown }).facts)
          ? ((ref as { facts: unknown[] }).facts.length)
          : 0),
      0,
    );
    const sessions = value.length;
    return (
      facts +
      (facts === 1 ? " fact" : " facts") +
      " from " +
      sessions +
      (sessions === 1 ? " session" : " sessions")
    );
  }
  return displayValue(value);
}

/** What the page says today, next to what the change set would write. */
function ChangeDiff({ diffs }: { diffs: FieldDiff[] }) {
  return (
    <details className="group">
      <summary className="flex cursor-pointer flex-wrap items-center gap-2 text-xs marker:hidden">
        <RlIcon
          name="chevron"
          size={14}
          className="text-[color:var(--rl-text-on-parchment-muted)] transition-transform group-open:rotate-90"
        />
        <span className="font-semibold text-[color:var(--rl-text-on-parchment-primary)]">
          {diffs.length} field{diffs.length === 1 ? "" : "s"} change
        </span>
        <span className="text-[color:var(--rl-text-on-parchment-muted)]">
          what the page says today, and what it would say
        </span>
      </summary>
      <ul className="mt-3 divide-y divide-[color:var(--rl-border-parchment)] rounded-[var(--rl-radius-sm)] border border-[color:var(--rl-border-parchment)]">
        {diffs.map((diff) => (
          <li key={diff.path} className="grid gap-1 px-3 py-2 sm:grid-cols-[9rem_minmax(0,1fr)_minmax(0,1fr)]">
            <span className="rl-eyebrow">{diff.label}</span>
            <span className="break-words text-xs text-[color:var(--rl-text-on-parchment-muted)] line-through">
              {diffValue(diff.path, diff.before) || "—"}
            </span>
            <span className="break-words text-xs font-semibold text-[color:var(--rl-text-on-parchment-primary)]">
              {diffValue(diff.path, diff.after) || "removed"}
            </span>
          </li>
        ))}
      </ul>
    </details>
  );
}
