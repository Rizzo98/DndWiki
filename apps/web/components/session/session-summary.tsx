// Session summary card: the LLM-written narrative of the session - prose in
// scene blocks, each carrying the place that part of the story happens in -
// plus the key events and the timeline beats, persisted by content-service.
//
// The summary is the REVIEW LAYER of the pipeline, and it is ONE STORY rather
// than a list of independent sentences: the DM
//
//   1. highlights a portion of that text with the mouse (any passage, mid
//      sentence included) and describes what must change ("it wasn't Character
//      A, it was Character B"),
//   2. adds the request to the list of changes - each one carries the passages
//      it is about, and stays editable and removable there,
//   3. regenerates - the LLM rewrites the narrative applying every queued
//      change, and propagates it to the entities, the events and the timeline,
//
// and only the CONFIRMED summary is turned into wiki content: first as the
// PROPOSED change set reviewed in the card below (pages, updates, timeline
// entries), which the DM confirms before anything is written.
//
// The place label above a paragraph is the reading the old "Where this session
// happens" panel used to show: where that part of the story happens. Timeline
// entries and events are click-to-seek into the recording player, mirroring the
// transcript viewer. Entity names that resolve to campaign wiki pages render as
// clickable links.

"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { Badge, Button, Card, EmptyState, TextArea, fmtPercent } from "@/components/ui";
import { RlIcon, RlTag } from "@/components/ravenlore";
import { LinkedText, normalizeName, type LinkIndex } from "@/components/linked-text";
import type { SessionSummary, SummaryBlock, SummaryEdit } from "@/lib/api";

/** A change queued for the next rewrite: the API payload + a stable list key. */
type PendingEdit = SummaryEdit & { key: string };

/** A portion of the narrative the DM highlighted, with where it sits. */
type Portion = {
  key: string;
  /** The highlighted text, as the model will receive it. */
  text: string;
  /** Where the highlight falls, per block, in text offsets (for the marks). */
  spans: { block: number; start: number; end: number }[];
};

/** A passage highlighted and waiting for the "+" button. */
type PendingHighlight = Omit<Portion, "key"> & {
  /** Where the mouse was let go, relative to the narrative container. */
  x: number;
  y: number;
};

/** The rewrite endpoint accepts at most 20 corrections per request. */
const MAX_CHANGES = 20;

/** How many passages are quoted before the rest collapse into "+N more". */
const MAX_QUOTED_TARGETS = 3;

/** Shorter selections are a stray click or a word the DM did not mean. */
const MIN_SELECTION_CHARS = 3;

let pendingEditSeq = 0;

/** Stable identity for a queued change (removing one must not re-target
 *  another, and the editor stays on the row the DM opened). */
function nextKey(prefix: string): string {
  pendingEditSeq += 1;
  return prefix + "-" + pendingEditSeq;
}

// Inline actions for a single queued change: the shared <Button> is sized for
// the main actions, not for a row of a list.
const ROW_ACTION =
  "rounded-md px-2 py-1 text-xs font-medium text-[color:var(--rl-text-on-parchment-muted)] transition hover:bg-[color:var(--rl-bg-parchment-sunk)] hover:text-[color:var(--rl-text-on-parchment-primary)] disabled:cursor-not-allowed disabled:opacity-50";
/** Save: the one affirmative action inside a row. */
const ROW_ACTION_PRIMARY =
  "rl-btn rl-btn--primary px-2.5 py-1 text-xs font-semibold transition disabled:cursor-not-allowed disabled:opacity-50";
const ROW_ACTION_DANGER =
  "rounded-md px-2 py-1 text-xs font-medium text-[color:var(--rl-text-on-parchment-muted)] transition hover:bg-[color:color-mix(in_srgb,var(--rl-cat-villain)_14%,transparent)] hover:text-[color:var(--rl-cat-villain)] disabled:cursor-not-allowed disabled:opacity-50";

/**
 * The narrative as the scene blocks the page renders.
 *
 * The blocks come from the stored summary; a summary written before they
 * existed (or one whose composition failed, leaving the raw beats) has only
 * text, and its paragraphs are the blocks - the DM still reads a story, just
 * without place labels (app/summary.py does the same on the server).
 */
export function summaryBlocks(summary: SessionSummary | null): SummaryBlock[] {
  const stored = (summary?.summary_blocks ?? []).filter((block) => block?.text?.trim());
  if (stored.length) return stored;
  const text = String(summary?.summary ?? "");
  if (!text.trim()) return [];
  const parts = text.includes("\n\n") ? text.split(/\n{2,}/) : text.split("\n");
  return parts
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => ({ location: "", text: part }));
}

/** The narrative as one text: what the rewrite is asked to keep in sync. */
function blocksToText(blocks: SummaryBlock[]): string {
  return blocks.map((block) => block.text).join("\n\n");
}

/** The passages a change is about, quoted and collapsed when numerous. */
function TargetPills({ targets }: { targets: string[] }) {
  if (!targets.length) return <span className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">the whole summary</span>;
  const shown = targets.slice(0, MAX_QUOTED_TARGETS);
  const hidden = targets.length - shown.length;
  return (
    <span className="flex flex-wrap items-center gap-1">
      <span className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">
        {targets.length === 1 ? "passage:" : targets.length + " passages:"}
      </span>
      {shown.map((passage, i) => (
        <span
          key={i + "-" + passage.slice(0, 16)}
          title={passage}
          className="max-w-[32ch] truncate rounded bg-[color:var(--rl-bg-parchment-sunk)] px-1.5 py-0.5 text-xs italic text-[color:var(--rl-text-on-parchment-muted)]"
        >
          “{passage}”
        </span>
      ))}
      {hidden > 0 ? <span className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">+{hidden} more</span> : null}
    </span>
  );
}

function parseTimeToSeconds(time: string): number | null {
  const parts = time.split(":").map((p) => Number(p));
  if (parts.some((n) => Number.isNaN(n))) return null;
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return null;
}

/** A chip whose label links to its wiki page when one exists in the index. */
function LinkedChip({
  name,
  campaignId,
  index,
  className,
}: {
  name: string;
  campaignId?: string;
  index?: LinkIndex | null;
  className: string;
}) {
  const target = campaignId && index ? index.byName.get(normalizeName(name)) : undefined;
  if (!target) return <span className={className}>{name}</span>;
  return (
    <Link href={"/campaigns/" + campaignId + "/pages/" + target.id} className={className + " hover:underline"}>
      {name}
    </Link>
  );
}

/** Is 'other' this node, or inside it? (compareDocumentPosition bitmask.) */
function contains(node: Node, other: Node | null): boolean {
  if (!other) return false;
  if (other === node) return true;
  return (node.compareDocumentPosition(other) & Node.DOCUMENT_POSITION_CONTAINED_BY) !== 0;
}

/** The text offset of a point INSIDE a node (the point must be inside it). */
function offsetOf(node: Node, container: Node, offset: number): number {
  const walk = document.createRange();
  walk.selectNodeContents(node);
  try {
    walk.setEnd(container, offset);
  } catch {
    return walk.toString().length;
  }
  return walk.toString().length;
}

/**
 * Where a DOM selection falls inside one block's text, in character offsets.
 *
 * The selection is CLAMPED to the block, so a highlight that runs across two
 * paragraphs is recorded on both (the marks stay aligned with the text even
 * though the DOM is split into links). Node.compareDocumentPosition is used
 * rather than Range.compareBoundaryPoints: the flags are unambiguous ("is this
 * point before/after/inside that node"), while the four compareBoundaryPoints
 * modes are famously easy to get backwards - which is exactly how the first
 * version of this silently captured nothing.
 */
function offsetsWithin(node: HTMLElement, range: Range): { start: number; end: number } | null {
  const startsHere = contains(node, range.startContainer);
  const endsHere = contains(node, range.endContainer);
  const startsBefore =
    !startsHere &&
    (node.compareDocumentPosition(range.startContainer) & Node.DOCUMENT_POSITION_PRECEDING) !== 0;
  const endsAfter =
    !endsHere &&
    (node.compareDocumentPosition(range.endContainer) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
  // The selection is entirely after this block, or entirely before it.
  if (!startsHere && !startsBefore) return null;
  if (!endsHere && !endsAfter) return null;
  const length = node.textContent?.length ?? 0;
  const start = startsHere ? offsetOf(node, range.startContainer, range.startOffset) : 0;
  const end = endsHere ? offsetOf(node, range.endContainer, range.endOffset) : length;
  return end > start ? { start, end } : null;
}

/** The block text split into plain and highlighted segments. */
function segments(
  text: string,
  spans: { start: number; end: number }[]
): { text: string; marked: boolean }[] {
  const ordered = [...spans].sort((a, b) => a.start - b.start);
  const out: { text: string; marked: boolean }[] = [];
  let cursor = 0;
  for (const span of ordered) {
    const start = Math.max(span.start, cursor);
    const end = Math.min(span.end, text.length);
    if (end <= start) continue;
    if (start > cursor) out.push({ text: text.slice(cursor, start), marked: false });
    out.push({ text: text.slice(start, end), marked: true });
    cursor = end;
  }
  if (cursor < text.length) out.push({ text: text.slice(cursor), marked: false });
  return out.length ? out : [{ text, marked: false }];
}

export function SessionSummaryCard({
  summary,
  onSeek,
  campaignId,
  linkIndex,
  currentPromptVersion,
  sessionStatus,
  canReview,
  onRegenerateSummary,
  onConfirmSummary,
  reviewBusy,
}: {
  summary: SessionSummary | null;
  /** Jump the recording player to a timestamp (seconds) - used by timeline rows. */
  onSeek?: (seconds: number) => void;
  /** Campaign the session belongs to: entity names link to its wiki pages. */
  campaignId?: string;
  /** Campaign page index: known entity names become links. */
  linkIndex?: LinkIndex | null;
  /** Prompt version the deployment currently uses (from /api/content/llm):
   *  the badge tracks prompt evolution instead of the version frozen on the
   *  last generation. */
  currentPromptVersion?: string | null;
  /** Session status: the review tools are frozen while the pipeline runs. */
  sessionStatus?: string;
  /** DM (or dev): may rewrite and confirm the summary. */
  canReview?: boolean;
  /** Rebuild the summary applying the collected review requests. */
  onRegenerateSummary?: (edits: SummaryEdit[], text: string) => void;
  /** Accept the summary: the proposed wiki changes are computed from it. */
  onConfirmSummary?: () => void;
  /** Which review action is in flight (disables the buttons). */
  reviewBusy?: "regenerate" | "confirm" | null;
}) {
  // The passages added to the change being composed (the persistent marks).
  const [portions, setPortions] = useState<Portion[]>([]);
  // The passage just highlighted with the mouse, waiting for the "+": it is
  // still the browser's own selection, so the DM sees it highlighted, and the
  // button floats where the mouse stopped.
  const [pending, setPending] = useState<PendingHighlight | null>(null);
  const [instruction, setInstruction] = useState("");
  // The changes queued for the next rewrite: this list - not the box - is what
  // "Regenerate summary" sends.
  const [pendingEdits, setPendingEdits] = useState<PendingEdit[]>([]);
  // The queued change open for editing (keyed, so removing a sibling does not
  // move the editor onto another change).
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const [editTargets, setEditTargets] = useState<string[]>([]);
  // Key moments are the transcript-anchored beats; they are a reference, not
  // the thing being reviewed, so they stay folded until asked for.
  const [showMoments, setShowMoments] = useState(false);
  const narrativeRef = useRef<HTMLDivElement | null>(null);
  const blockRefs = useRef<Array<HTMLParagraphElement | null>>([]);
  const addButtonRef = useRef<HTMLButtonElement | null>(null);

  const revision = summary?.revision ?? 1;
  const updatedAt = summary?.updated_at ?? null;

  // A new revision replaces the text: the highlights and the requests already
  // applied to it are gone.
  useEffect(() => {
    setPortions([]);
    setPending(null);
    setPendingEdits([]);
    setInstruction("");
    setEditingKey(null);
    setEditDraft("");
    setEditTargets([]);
  }, [updatedAt, revision]);

  // A click anywhere but on the "+" (or Escape) drops the pending highlight:
  // the button closes and the selection is removed with it.
  useEffect(() => {
    if (!pending) return;
    function onPointerDown(event: MouseEvent) {
      if (addButtonRef.current?.contains(event.target as Node)) return;
      dismissPending();
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") dismissPending();
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [pending]);

  const blocks = summaryBlocks(summary);
  const narrative = blocksToText(blocks);
  const isDraft = summary?.review_status !== "confirmed";
  const pipelineRunning = sessionStatus === "summarizing" || sessionStatus === "generating_wiki";
  const reviewOpen = Boolean(canReview) && isDraft && blocks.length > 0;
  // The backend refuses a rewrite with more than MAX_CHANGES corrections.
  const atChangeLimit = pendingEdits.length >= MAX_CHANGES;

  /** Drop the pending highlight: the selection goes with it. */
  function dismissPending() {
    setPending(null);
    window.getSelection()?.removeAllRanges();
  }

  /**
   * The DM let go of the mouse over a passage: keep the selection highlighted
   * (the browser still shows it) and float the "+" where the mouse stopped.
   * Nothing is added until they press it.
   */
  function captureSelection(event: React.MouseEvent<HTMLDivElement>) {
    if (!reviewOpen) return;
    // The "+" itself lives inside the narrative: a click on it is not a new
    // selection (its mousedown is prevented, so the selection survives).
    if (addButtonRef.current?.contains(event.target as Node)) return;
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
      setPending(null);
      return;
    }
    const range = selection.getRangeAt(0);
    const container = narrativeRef.current;
    if (!container || !contains(container, range.commonAncestorContainer)) return;
    const text = selection.toString().replace(/\s+/g, " ").trim();
    if (text.length < MIN_SELECTION_CHARS) {
      setPending(null);
      return;
    }
    const spans: Portion["spans"] = [];
    blockRefs.current.forEach((node, index) => {
      if (!node) return;
      const offsets = offsetsWithin(node, range);
      if (offsets) spans.push({ block: index, start: offsets.start, end: offsets.end });
    });
    if (!spans.length) {
      setPending(null);
      return;
    }
    const bounds = container.getBoundingClientRect();
    setPending({
      text,
      spans,
      x: event.clientX - bounds.left,
      y: event.clientY - bounds.top,
    });
  }

  /** The "+" was pressed: the passage becomes part of the change. */
  function addPending() {
    if (!pending) return;
    setPortions((prev) => [
      ...prev,
      { key: nextKey("portion"), text: pending.text, spans: pending.spans },
    ]);
    setPending(null);
    window.getSelection()?.removeAllRanges();
  }

  function dropPortion(key: string) {
    setPortions((prev) => prev.filter((portion) => portion.key !== key));
  }

  /** Queue the composer (highlighted passages + text) as one change. */
  function addEdit() {
    const text = instruction.trim();
    if (!text || pendingEdits.length >= MAX_CHANGES) return;
    setPendingEdits((prev) => [
      ...prev,
      { key: nextKey("change"), targets: portions.map((portion) => portion.text), instruction: text },
    ]);
    setInstruction("");
    setPortions([]);
  }

  function removeEdit(key: string) {
    setPendingEdits((prev) => prev.filter((edit) => edit.key !== key));
    if (editingKey === key) cancelEdit();
  }

  function startEdit(edit: PendingEdit) {
    setEditingKey(edit.key);
    setEditDraft(edit.instruction);
    setEditTargets(edit.targets);
  }

  function cancelEdit() {
    setEditingKey(null);
    setEditDraft("");
    setEditTargets([]);
  }

  /** Write the open editor back into its queued change. An empty instruction
   *  is refused: the rewrite endpoint rejects a change without text. */
  function saveEdit() {
    const text = editDraft.trim();
    if (editingKey === null || !text) return;
    setPendingEdits((prev) =>
      prev.map((edit) =>
        edit.key === editingKey ? { ...edit, targets: editTargets, instruction: text } : edit,
      ),
    );
    cancelEdit();
  }

  function submitReview() {
    if (!onRegenerateSummary) return;
    // Whatever is still in the box is queued first: nothing typed is dropped
    // silently, and the list always shows exactly what is being sent.
    const text = instruction.trim();
    const queued: PendingEdit[] =
      text && pendingEdits.length < MAX_CHANGES
        ? [
            ...pendingEdits,
            { key: nextKey("change"), targets: portions.map((portion) => portion.text), instruction: text },
          ]
        : pendingEdits;
    if (!queued.length) return;
    if (queued !== pendingEdits) {
      setPendingEdits(queued);
      setInstruction("");
      setPortions([]);
      cancelEdit();
    }
    // The list stays on screen until the new revision lands (or the request
    // fails): a failed rewrite stays retryable, a successful one clears it
    // through the revision effect above.
    onRegenerateSummary(
      queued.map(({ targets, instruction: body }) => ({ targets, instruction: body })),
      narrative,
    );
  }

  if (!summary || !summary.summary) {
    return (
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="rl-title text-lg">Session summary</h2>
        </div>
        <div className="mt-4">
          <EmptyState>
            The AI summary appears here once the transcript has been distilled —
            it is the draft you review before any wiki page is created.
          </EmptyState>
        </div>
      </Card>
    );
  }

  const hasEvents = (summary.events?.length ?? 0) > 0;
  const hasTimeline = (summary.timeline_entries?.length ?? 0) > 0;
  const hasCharacters = (summary.characters?.length ?? 0) > 0;
  const hasLocations = (summary.locations?.length ?? 0) > 0;

  // The badge shows the prompt version the deployment uses NOW; when the
  // summary was generated by an older prompt, that version is called out too.
  const storedVersion = summary.prompt_version;
  const promptBadge = currentPromptVersion ? (
    <span
      title={
        storedVersion && storedVersion !== currentPromptVersion
          ? "generated with " + storedVersion
          : undefined
      }
    >
      <Badge tone="slate">
        prompt {currentPromptVersion}
        {storedVersion && storedVersion !== currentPromptVersion ? " · made with " + storedVersion : ""}
      </Badge>
    </span>
  ) : storedVersion ? (
    <Badge tone="slate">prompt {storedVersion}</Badge>
  ) : null;

  return (
    <Card>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="rl-title text-lg">Session summary</h2>
          {isDraft ? (
            <span title="Nothing has been written to the wiki yet — review the story below, then confirm.">
              <Badge tone="amber">draft · awaiting review</Badge>
            </span>
          ) : (
            <span title="Confirmed: the wiki content of this session is built from this text — proposed below, and written to the wiki once you confirm that set.">
              <Badge tone="green">confirmed</Badge>
            </span>
          )}
          {summary.revision > 1 ? <Badge tone="slate">revision {summary.revision}</Badge> : null}
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {summary.llm_provider && summary.llm_model ? (
            <Badge tone="slate">
              {summary.llm_provider} / {summary.llm_model.replace(/^.*\//, "")}
            </Badge>
          ) : null}
          {summary.confidence !== null && summary.confidence !== undefined ? (
            <Badge tone="green">confidence {fmtPercent(summary.confidence)}</Badge>
          ) : null}
          {promptBadge}
        </div>
      </div>

      {pipelineRunning ? (
        <p className="mb-3 text-xs rl-text-item">
          {sessionStatus === "summarizing"
            ? "Rebuilding the summary…"
            : "Summary confirmed — computing the proposed wiki changes you review next…"}
        </p>
      ) : null}

      {reviewOpen ? (
        <p className="mb-3 text-xs text-[color:var(--rl-text-on-parchment-muted)]">
          Highlight a passage of the story with the mouse — any passage, mid-sentence
          included — then press the + that appears to add it to the change; clicking
          elsewhere drops the highlight. Highlight nothing to talk about the summary as a
          whole. The rewrite applies every listed change at once, and the wiki is only
          generated once you confirm.
        </p>
      ) : null}

      {/* The narrative: one story, in the scenes it happens in. */}
      <div
        ref={narrativeRef}
        onMouseUp={reviewOpen ? captureSelection : undefined}
        className={"rl-narrative relative space-y-3" + (reviewOpen ? " select-text" : "")}
      >
        {blocks.map((block, index) => (
          <div key={index}>
            {block.location ? (
              <div className="mb-1 flex items-center gap-2">
                <span title="Where this part of the session happens">
                  <RlTag tint="place">
                    <RlIcon name="pin" size={12} />
                    {block.location}
                  </RlTag>
                </span>
              </div>
            ) : null}
            <p
              ref={(node) => {
                blockRefs.current[index] = node;
              }}
              className="text-sm leading-relaxed text-[color:var(--rl-text-on-parchment-primary)]"
            >
              {segments(
                block.text,
                portions
                  .flatMap((portion) => portion.spans)
                  .filter((span) => span.block === index)
              ).map((segment, part) =>
                segment.marked ? (
                  <mark key={part} className="rl-mark">
                    <LinkedText text={segment.text} campaignId={campaignId} index={linkIndex} />
                  </mark>
                ) : (
                  <LinkedText
                    key={part}
                    text={segment.text}
                    campaignId={campaignId}
                    index={linkIndex}
                  />
                )
              )}
            </p>
          </div>
        ))}

        {/* The "+" that turns the highlighted passage into part of the change:
            it floats where the mouse was let go, and clicking anywhere else
            drops it (and the highlight) without adding anything.

            It lives INSIDE this container on purpose: the coordinates are
            measured against this element's rect, so it has to be the one the
            button is positioned against (as an absolute child of a sibling it
            was placed against the card instead, and landed far above the
            cursor). marginTop is zeroed because the container's space-y would
            otherwise push it down by the gap. */}
        {pending ? (
          <button
            ref={addButtonRef}
            type="button"
            onMouseDown={(event) => event.preventDefault()}
            onClick={addPending}
            style={{ left: pending.x + 6, top: pending.y - 10, marginTop: 0 }}
            className="absolute z-20 flex h-6 w-6 items-center justify-center rounded-full bg-[color:var(--rl-accent-500)] text-base font-bold leading-none text-[color:var(--rl-text-on-dark-primary)] shadow-[var(--rl-shadow-raised)] transition hover:bg-[color:var(--rl-accent-600)]"
            title="Add this passage to the change"
            aria-label="Add this passage to the change"
          >
            +
          </button>
        ) : null}
      </div>

      {reviewOpen ? (
        <div className="mt-4 rounded-xl border border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-bg-parchment-sunk)] p-4">
          <div className="mb-2 text-xs font-bold uppercase tracking-wider text-[color:var(--rl-text-on-parchment-muted)]">
            Ask for a change
          </div>
          <p className="mb-2 text-xs text-[color:var(--rl-text-on-parchment-muted)]">
            {portions.length === 0
              ? "Nothing highlighted: the request concerns the whole summary."
              : portions.length +
                " passage" +
                (portions.length > 1 ? "s" : "") +
                " highlighted."}
          </p>
          {portions.length ? (
            <ul className="mb-2 space-y-1">
              {portions.map((portion) => (
                <li key={portion.key} className="flex items-start gap-1.5">
                  <span className="text-xs italic text-[color:var(--rl-text-on-parchment-muted)]">
                    “{portion.text}”
                  </span>
                  <button
                    type="button"
                    className="shrink-0 text-[color:var(--rl-text-on-parchment-muted)] hover:text-[color:var(--rl-cat-villain)]"
                    onClick={() => dropPortion(portion.key)}
                    title="Drop this passage from the request"
                    aria-label="Drop this passage from the request"
                  >
                    ✕
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          <TextArea
            rows={3}
            value={instruction}
            placeholder={'What should change? e.g. "It wasn\'t Character A, it was Character B."'}
            onChange={(e) => setInstruction(e.target.value)}
            disabled={Boolean(reviewBusy) || pipelineRunning}
          />
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <Button
              variant="secondary"
              onClick={addEdit}
              disabled={!instruction.trim() || atChangeLimit || Boolean(reviewBusy) || pipelineRunning}
              title="Queue this request below"
            >
              Add change
            </Button>
            <span className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">
              {atChangeLimit
                ? "That is the maximum of " + MAX_CHANGES + " changes at a time."
                : "The highlighted passages are attached to the change."}
            </span>
          </div>

          {/* The queued changes: one entry per request, each editable and
              removable. This list is what the rewrite is built from. */}
          <div className="mt-4 border-t border-[color:var(--rl-border-parchment)] pt-3">
            <div className="mb-2 flex items-center gap-2">
              <span className="text-xs font-bold uppercase tracking-wider text-[color:var(--rl-text-on-parchment-muted)]">
                Changes to apply
              </span>
              <Badge tone={pendingEdits.length ? "amber" : "slate"}>{pendingEdits.length}</Badge>
            </div>

            {pendingEdits.length === 0 ? (
              <p className="rounded-lg border border-dashed border-[color:var(--rl-border-parchment)] px-3 py-2.5 text-xs text-[color:var(--rl-text-on-parchment-muted)]">
                No change added yet. Describe the correction above and press “Add change” — the
                rewrite is built from this list only.
              </p>
            ) : (
              <ol className="space-y-2">
                {pendingEdits.map((edit, i) => {
                  const isEditing = editingKey === edit.key;
                  return (
                    <li
                      key={edit.key}
                      className={
                        "rounded-lg border px-3 py-2.5 text-xs transition " +
                        (isEditing
                          ? "border-[color:color-mix(in_srgb,var(--rl-accent-500)_50%,transparent)] bg-[color:var(--rl-bg-card)]"
                          : "border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-bg-card)]")
                      }
                    >
                      {isEditing ? (
                        <div>
                          <div className="mb-1.5 text-xs font-semibold rl-text-accent">
                            Editing change {i + 1}
                          </div>
                          <TextArea
                            rows={3}
                            autoFocus
                            value={editDraft}
                            onChange={(e) => setEditDraft(e.target.value)}
                            disabled={Boolean(reviewBusy) || pipelineRunning}
                          />
                          <div className="mt-2 text-[color:var(--rl-text-on-parchment-muted)]">
                            <div className="mb-1 flex flex-wrap items-center gap-2">
                              <span>Passages concerned:</span>
                              <button
                                type="button"
                                className={ROW_ACTION}
                                disabled={!portions.length}
                                onClick={() => setEditTargets(portions.map((portion) => portion.text))}
                                title="Replace them with the passages highlighted in the summary"
                              >
                                use the {portions.length} highlighted
                              </button>
                              {editTargets.length ? (
                                <button
                                  type="button"
                                  className={ROW_ACTION}
                                  onClick={() => setEditTargets([])}
                                  title="Make the change about the summary as a whole"
                                >
                                  whole summary
                                </button>
                              ) : null}
                            </div>
                            {editTargets.length ? (
                              <ul className="space-y-1">
                                {editTargets.map((passage) => (
                                  <li key={passage} className="flex items-start gap-1.5">
                                    <span className="italic text-[color:var(--rl-text-on-parchment-muted)]">“{passage}”</span>
                                    <button
                                      type="button"
                                      className="shrink-0 text-[color:var(--rl-text-on-parchment-muted)] hover:text-[color:var(--rl-cat-villain)]"
                                      onClick={() =>
                                        setEditTargets((prev) => prev.filter((p) => p !== passage))
                                      }
                                      title="Drop this passage from the change"
                                      aria-label="Drop this passage from the change"
                                    >
                                      ✕
                                    </button>
                                  </li>
                                ))}
                              </ul>
                            ) : (
                              <p>The change concerns the whole summary.</p>
                            )}
                          </div>
                          <div className="mt-2 flex flex-wrap items-center gap-2">
                            <button
                              type="button"
                              className={ROW_ACTION_PRIMARY}
                              onClick={saveEdit}
                              disabled={!editDraft.trim() || Boolean(reviewBusy) || pipelineRunning}
                            >
                              Save
                            </button>
                            <button
                              type="button"
                              className={ROW_ACTION}
                              onClick={cancelEdit}
                              disabled={Boolean(reviewBusy) || pipelineRunning}
                            >
                              Cancel
                            </button>
                          </div>
                        </div>
                      ) : (
                        <div>
                          <div className="flex items-start gap-2">
                            <span className="mt-px inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-[color:var(--rl-bg-parchment-sunk)] text-[10px] font-semibold text-[color:var(--rl-text-on-parchment-primary)]">
                              {i + 1}
                            </span>
                            <p className="flex-1 whitespace-pre-wrap leading-relaxed text-[color:var(--rl-text-on-parchment-primary)]">
                              {edit.instruction}
                            </p>
                            <div className="flex shrink-0 items-center gap-1">
                              <button
                                type="button"
                                className={ROW_ACTION}
                                onClick={() => startEdit(edit)}
                                disabled={Boolean(reviewBusy) || pipelineRunning}
                                title="Edit this change"
                              >
                                Edit
                              </button>
                              <button
                                type="button"
                                className={ROW_ACTION_DANGER}
                                onClick={() => removeEdit(edit.key)}
                                disabled={Boolean(reviewBusy) || pipelineRunning}
                                title="Remove this change"
                              >
                                Remove
                              </button>
                            </div>
                          </div>
                          <div className="mt-1.5 pl-6">
                            <TargetPills targets={edit.targets} />
                          </div>
                        </div>
                      )}
                    </li>
                  );
                })}
              </ol>
            )}
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button
              onClick={submitReview}
              disabled={Boolean(reviewBusy) || pipelineRunning || pendingEdits.length === 0}
              title={
                pendingEdits.length
                  ? "Rebuild the summary applying the changes listed above"
                  : "Add at least one change first"
              }
            >
              {reviewBusy === "regenerate" ? "Regenerating…" : "Regenerate summary"}
            </Button>
            <span className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">
              {pendingEdits.length === 0
                ? "Add at least one change to regenerate."
                : instruction.trim()
                  ? "The text still in the box is added as a final change."
                  : "All the changes above are sent together."}
            </span>
          </div>
        </div>
      ) : null}

      {canReview && isDraft ? (
        <div className="mt-4 flex flex-wrap items-center justify-end gap-2 border-t border-[color:var(--rl-border-parchment)] pt-4">
          <Button
            variant="danger"
            disabled={Boolean(reviewBusy) || pipelineRunning}
            title="Turn this summary into the proposed wiki changes you review before anything is written"
            onClick={() => {
              if (
                !window.confirm(
                  "Confirm this summary?\n\nThe proposed wiki changes are computed from it — nothing is written to the wiki until you review that set and confirm it.",
                )
              ) {
                return;
              }
              onConfirmSummary?.();
            }}
          >
            {reviewBusy === "confirm" ? "Proposing changes…" : "Confirm summary & propose the wiki changes"}
          </Button>
        </div>
      ) : null}

      {hasTimeline ? (
        <div className="mt-5">
          <button
            type="button"
            onClick={() => setShowMoments((v) => !v)}
            aria-expanded={showMoments}
            className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left transition hover:bg-[color:var(--rl-bg-parchment-sunk)]"
          >
            <span
              aria-hidden
              className={"inline-block text-xs text-[color:var(--rl-text-on-parchment-muted)] transition-transform " + (showMoments ? "rotate-90" : "")}
            >
              ▶
            </span>
            <h3 className="text-xs font-bold uppercase tracking-wider text-[color:var(--rl-text-on-parchment-muted)]">
              Key moments
            </h3>
            <Badge tone="slate">{summary.timeline_entries.length}</Badge>
            <span className="ml-auto text-xs text-[color:var(--rl-text-on-parchment-muted)]">{showMoments ? "Hide" : "Show"}</span>
          </button>
          {showMoments ? (
            <ul className="mt-1 space-y-1.5">
              {summary.timeline_entries.map((entry, i) => {
                const seconds = parseTimeToSeconds(entry.time);
                return (
                  <li key={i}>
                    <div className="group flex w-full items-start gap-3 rounded-lg px-2 py-1.5 text-left transition hover:bg-[color:var(--rl-bg-parchment-sunk)]">
                      {seconds !== null && onSeek ? (
                        <button
                          onClick={() => onSeek(seconds)}
                          title="Jump to this moment in the recording"
                          className="mt-0.5 shrink-0 font-mono text-xs text-[color:var(--rl-text-on-parchment-muted)] tabular-nums hover:text-[color:var(--rl-accent-500)]"
                        >
                          {entry.time}
                        </button>
                      ) : (
                        <span className="mt-0.5 shrink-0 font-mono text-xs text-[color:var(--rl-text-on-parchment-muted)] tabular-nums">
                          {entry.time}
                        </span>
                      )}
                      <span className="text-sm leading-relaxed text-[color:var(--rl-text-on-parchment-primary)]">
                        <LinkedText text={entry.summary} campaignId={campaignId} index={linkIndex} />
                      </span>
                    </div>
                  </li>
                );
              })}
            </ul>
          ) : null}
        </div>
      ) : null}

      {hasEvents ? (
        <div className="mt-5">
          <h3 className="mb-2 text-xs font-bold uppercase tracking-wider text-[color:var(--rl-text-on-parchment-muted)]">
            Important events
          </h3>
          <ul className="space-y-3">
            {summary.events.map((event, i) => (
              <li key={i} className="rounded-lg border border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-bg-parchment-sunk)] px-3 py-2.5">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-semibold text-[color:var(--rl-text-on-parchment-primary)]">
                    <LinkedText text={event.title} campaignId={campaignId} index={linkIndex} />
                  </span>
                  {event.confidence !== null && event.confidence !== undefined ? (
                    <Badge tone="green">{fmtPercent(event.confidence)}</Badge>
                  ) : null}
                </div>
                {event.description ? (
                  <p className="mt-1 text-sm leading-relaxed text-[color:var(--rl-text-on-parchment-primary)]">
                    <LinkedText text={event.description} campaignId={campaignId} index={linkIndex} />
                  </p>
                ) : null}
                {event.participants?.length ? (
                  <div className="mt-1.5 flex flex-wrap gap-1.5">
                    {event.participants.map((p) => (
                      <LinkedChip
                        key={p}
                        name={p}
                        campaignId={campaignId}
                        index={linkIndex}
                        className="rounded-full bg-[color:var(--rl-bg-parchment-sunk)] px-2 py-0.5 text-xs text-[color:var(--rl-text-on-parchment-primary)]"
                      />
                    ))}
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {hasCharacters || hasLocations ? (
        <div className="mt-5 flex flex-wrap gap-6">
          {hasCharacters ? (
            <div>
              <h3 className="mb-1.5 text-xs font-bold uppercase tracking-wider text-[color:var(--rl-text-on-parchment-muted)]">Characters</h3>
              <div className="flex flex-wrap gap-1.5">
                {summary.characters.map((c) => (
                  <LinkedChip
                    key={c.name}
                    name={c.name}
                    campaignId={campaignId}
                    index={linkIndex}
                    className="rounded-full rl-bg-place-soft px-2 py-0.5 text-xs rl-text-place"
                  />
                ))}
              </div>
            </div>
          ) : null}
          {hasLocations ? (
            <div>
              <h3 className="mb-1.5 text-xs font-bold uppercase tracking-wider text-[color:var(--rl-text-on-parchment-muted)]">Locations</h3>
              <div className="flex flex-wrap gap-1.5">
                {summary.locations.map((l) => (
                  <LinkedChip
                    key={l.name}
                    name={l.name}
                    campaignId={campaignId}
                    index={linkIndex}
                    className="rounded-full rl-bg-item-soft px-2 py-0.5 text-xs rl-text-item"
                  />
                ))}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}
