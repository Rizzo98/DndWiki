// Session summary card: the LLM-distilled recap of what happened in the
// session (summary lines + key events + timeline beats), persisted by
// content-service.
//
// The summary is the REVIEW LAYER of the pipeline: it is stored as one line
// per beat while it is a draft, and the DM can
//
//   1. tick the lines that are wrong (or none, for the whole summary),
//   2. describe the change in the box below ("it wasn't Character A, it was
//      Character B"),
//   3. regenerate - the LLM rewrites the whole session record and the DM
//      reviews the new revision,
//
// and only the CONFIRMED summary is turned into wiki pages and timeline
// events. Timeline entries and events are click-to-seek into the recording
// player, mirroring the transcript viewer. Entity names that resolve to
// campaign wiki pages (created once the summary is confirmed) render as
// clickable links.

"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Badge, Button, Card, EmptyState, TextArea, fmtPercent } from "@/components/ui";
import { LinkedText, normalizeName, type LinkIndex } from "@/components/linked-text";
import type { SessionSummary, SummaryEdit } from "@/lib/api";

/** The reviewable lines of a summary (newline separated). */
export function summaryLines(text: string): string[] {
  return (text ?? "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
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
    <Link href={`/campaigns/${campaignId}/pages/${target.id}`} className={`${className} hover:underline`}>
      {name}
    </Link>
  );
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
  /** Jump the recording player to a timestamp (seconds) — used by timeline rows. */
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
  onRegenerateSummary?: (edits: SummaryEdit[], lines: string[]) => void;
  /** Accept the summary: the wiki pages and the events are created from it. */
  onConfirmSummary?: () => void;
  /** Which review action is in flight (disables the buttons). */
  reviewBusy?: "regenerate" | "confirm" | null;
}) {
  const [selected, setSelected] = useState<string[]>([]);
  const [instruction, setInstruction] = useState("");
  const [pendingEdits, setPendingEdits] = useState<SummaryEdit[]>([]);

  const revision = summary?.revision ?? 1;
  const updatedAt = summary?.updated_at ?? null;

  // A new revision replaces the text: the old selection and the requests
  // already applied to it are gone.
  useEffect(() => {
    setSelected([]);
    setPendingEdits([]);
    setInstruction("");
  }, [updatedAt, revision]);

  const lines = summary ? summaryLines(summary.summary) : [];
  const isDraft = summary?.review_status !== "confirmed";
  const pipelineRunning = sessionStatus === "summarizing" || sessionStatus === "generating_wiki";
  const reviewOpen = Boolean(canReview) && isDraft && Boolean(summary?.summary);

  function toggleLine(line: string) {
    setSelected((prev) => (prev.includes(line) ? prev.filter((l) => l !== line) : [...prev, line]));
  }

  function addEdit() {
    const text = instruction.trim();
    if (!text) return;
    setPendingEdits((prev) => [...prev, { targets: selected, instruction: text }]);
    setInstruction("");
    setSelected([]);
  }

  function submitReview() {
    if (!onRegenerateSummary) return;
    // The request collected in the box (with the current selection) goes
    // together with the ones already added below it.
    const text = instruction.trim();
    const edits = text ? [...pendingEdits, { targets: selected, instruction: text }] : pendingEdits;
    if (!edits.length) return;
    onRegenerateSummary(edits, lines);
    setPendingEdits([]);
    setSelected([]);
    setInstruction("");
  }

  if (!summary || !summary.summary) {
    return (
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg font-semibold">Session summary</h2>
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
          ? `generated with ${storedVersion}`
          : undefined
      }
    >
      <Badge tone="slate">
        prompt {currentPromptVersion}
        {storedVersion && storedVersion !== currentPromptVersion ? ` · made with ${storedVersion}` : ""}
      </Badge>
    </span>
  ) : storedVersion ? (
    <Badge tone="slate">prompt {storedVersion}</Badge>
  ) : null;

  return (
    <Card>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-lg font-semibold">Session summary</h2>
          {isDraft ? (
            <span title="Nothing has been written to the wiki yet — review the lines below, then confirm.">
              <Badge tone="amber">draft · awaiting review</Badge>
            </span>
          ) : (
            <span title="Confirmed: the wiki pages, the event pages and the timeline entries were created from this text.">
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
        <p className="mb-3 text-xs text-amber-300">
          {sessionStatus === "summarizing"
            ? "Rebuilding the summary…"
            : "Summary confirmed — creating the wiki pages and the timeline events…"}
        </p>
      ) : null}

      {reviewOpen ? (
        <p className="mb-2 text-xs text-slate-500">
          Tick the lines that are wrong (tick none to talk about the summary as a whole), describe the
          change below and regenerate. The wiki is only generated once you confirm.
        </p>
      ) : null}

      <ul className="space-y-1">
        {lines.map((line, i) => {
          const checked = selected.includes(line);
          return (
            <li
              key={`${i}-${line.slice(0, 24)}`}
              className={`flex items-start gap-3 rounded-lg px-2 py-1.5 text-sm leading-relaxed transition ${
                checked ? "bg-ember-500/10 ring-1 ring-ember-500/40" : "hover:bg-slate-800/50"
              }`}
            >
              {reviewOpen ? (
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => toggleLine(line)}
                  className="mt-1 h-4 w-4 shrink-0 accent-ember-500"
                  aria-label={`select summary line ${i + 1}`}
                />
              ) : (
                <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-600" />
              )}
              <span className={`text-slate-200 ${reviewOpen ? "cursor-pointer" : ""}`} onClick={reviewOpen ? () => toggleLine(line) : undefined}>
                <LinkedText text={line} campaignId={campaignId} index={linkIndex} />
              </span>
            </li>
          );
        })}
      </ul>

      {reviewOpen ? (
        <div className="mt-4 rounded-xl border border-slate-800 bg-slate-800/30 p-4">
          <div className="mb-2 text-xs font-bold uppercase tracking-wider text-slate-500">
            Ask for a change
          </div>
          <p className="mb-2 text-xs text-slate-400">
            {selected.length === 0
              ? "No line selected: the request concerns the whole summary."
              : `${selected.length} line${selected.length > 1 ? "s" : ""} selected.`}
          </p>
          {selected.length ? (
            <ul className="mb-2 space-y-1">
              {selected.map((line) => (
                <li key={line} className="truncate text-xs italic text-slate-400">
                  “{line}”
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
          {pendingEdits.length ? (
            <ul className="mt-3 space-y-1">
              {pendingEdits.map((edit, i) => (
                <li
                  key={i}
                  className="flex items-start justify-between gap-2 rounded-lg bg-slate-900/60 px-2 py-1.5 text-xs text-slate-300"
                >
                  <span>
                    <span className="text-slate-500">
                      {edit.targets.length
                        ? `${edit.targets.length} line${edit.targets.length > 1 ? "s" : ""}: `
                        : "whole summary: "}
                    </span>
                    {edit.instruction}
                  </span>
                  <button
                    className="shrink-0 text-slate-500 hover:text-red-300"
                    onClick={() => setPendingEdits((prev) => prev.filter((_, j) => j !== i))}
                    title="Remove this request"
                  >
                    ✕
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button
              variant="secondary"
              onClick={addEdit}
              disabled={!instruction.trim() || Boolean(reviewBusy) || pipelineRunning}
            >
              Add change
            </Button>
            <Button
              onClick={submitReview}
              disabled={
                Boolean(reviewBusy) ||
                pipelineRunning ||
                (pendingEdits.length === 0 && !instruction.trim())
              }
              title="Rebuild the summary applying the changes"
            >
              {reviewBusy === "regenerate" ? "Regenerating…" : "Regenerate summary"}
            </Button>
          </div>
          <p className="mt-2 text-xs text-slate-500">
            Add several changes before regenerating, or confirm once the summary reads right.
          </p>
        </div>
      ) : null}

      {canReview && isDraft ? (
        <div className="mt-4 flex flex-wrap items-center justify-end gap-2 border-t border-slate-800 pt-4">
          <Button
            variant="danger"
            disabled={Boolean(reviewBusy) || pipelineRunning}
            title="Create the wiki pages and the timeline events from this summary"
            onClick={() => {
              if (
                !window.confirm(
                  "Confirm this summary?\n\nThe wiki pages and the timeline events will be created from it. You can still regenerate the summary afterwards, but the pages will already exist.",
                )
              ) {
                return;
              }
              onConfirmSummary?.();
            }}
          >
            {reviewBusy === "confirm" ? "Creating pages…" : "Confirm summary & create wiki pages"}
          </Button>
        </div>
      ) : null}

      {hasTimeline ? (
        <div className="mt-5">
          <h3 className="mb-2 text-xs font-bold uppercase tracking-wider text-slate-500">
            Key moments
          </h3>
          <ul className="space-y-1.5">
            {summary.timeline_entries.map((entry, i) => {
              const seconds = parseTimeToSeconds(entry.time);
              return (
                <li key={i}>
                  <div className="group flex w-full items-start gap-3 rounded-lg px-2 py-1.5 text-left transition hover:bg-slate-800/60">
                    {seconds !== null && onSeek ? (
                      <button
                        onClick={() => onSeek(seconds)}
                        title="Jump to this moment in the recording"
                        className="mt-0.5 shrink-0 font-mono text-xs text-slate-500 tabular-nums hover:text-ember-400"
                      >
                        {entry.time}
                      </button>
                    ) : (
                      <span className="mt-0.5 shrink-0 font-mono text-xs text-slate-500 tabular-nums">
                        {entry.time}
                      </span>
                    )}
                    <span className="text-sm leading-relaxed text-slate-200">
                      <LinkedText text={entry.summary} campaignId={campaignId} index={linkIndex} />
                    </span>
                  </div>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      {hasEvents ? (
        <div className="mt-5">
          <h3 className="mb-2 text-xs font-bold uppercase tracking-wider text-slate-500">
            Important events
          </h3>
          <ul className="space-y-3">
            {summary.events.map((event, i) => (
              <li key={i} className="rounded-lg border border-slate-800 bg-slate-800/30 px-3 py-2.5">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-semibold text-slate-100">
                    <LinkedText text={event.title} campaignId={campaignId} index={linkIndex} />
                  </span>
                  {event.confidence !== null && event.confidence !== undefined ? (
                    <Badge tone="green">{fmtPercent(event.confidence)}</Badge>
                  ) : null}
                </div>
                {event.description ? (
                  <p className="mt-1 text-sm leading-relaxed text-slate-300">
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
                        className="rounded-full bg-slate-700/50 px-2 py-0.5 text-xs text-slate-300"
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
              <h3 className="mb-1.5 text-xs font-bold uppercase tracking-wider text-slate-500">Characters</h3>
              <div className="flex flex-wrap gap-1.5">
                {summary.characters.map((c) => (
                  <LinkedChip
                    key={c.name}
                    name={c.name}
                    campaignId={campaignId}
                    index={linkIndex}
                    className="rounded-full bg-blue-500/10 px-2 py-0.5 text-xs text-blue-300"
                  />
                ))}
              </div>
            </div>
          ) : null}
          {hasLocations ? (
            <div>
              <h3 className="mb-1.5 text-xs font-bold uppercase tracking-wider text-slate-500">Locations</h3>
              <div className="flex flex-wrap gap-1.5">
                {summary.locations.map((l) => (
                  <LinkedChip
                    key={l.name}
                    name={l.name}
                    campaignId={campaignId}
                    index={linkIndex}
                    className="rounded-full bg-amber-500/10 px-2 py-0.5 text-xs text-amber-300"
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
