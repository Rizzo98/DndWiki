// The 'plan' toolkit: write notes, then turn the ones you pick into wiki pages.
//
// Two halves, in the order they are used as a TOOL rather than as a workflow:
//
// 1. the proposal (what the tool is for). Its panel is always here, so the state
//    of the plan is the first thing the DM sees: nothing generated yet, reading
//    your notes, awaiting your confirmation, written. The action that starts a
//    generation sits with it, next to the count of what it will read.
// 2. the notes (the material). A list to pick from and an editor for the open
//    one, side by side on a wide screen.
//
// Writing a note never calls a model and never touches the wiki - it is a text
// box - and generating never writes: it proposes. Confirming the proposal is the
// only thing that creates or updates pages, and it goes through the same review
// the session pipeline uses (PlanReviewCard), because it is the same kind of
// change set.

"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert } from "@/components/ui";
import { buildLinkIndex, type LinkIndex } from "@/components/linked-text";
import { NotesList } from "@/components/toolkit/notes-list";
import { NoteEditor } from "@/components/toolkit/note-editor";
import { PlanReviewCard } from "@/components/session/session-plan";
import { AuthGate, useAuth } from "@/lib/auth";
import { useCampaign } from "@/lib/campaign-context";
import {
  RlButton,
  RlEmptyState,
  RlIconChip,
  RlPanel,
  RlPanelHead,
  RlTag,
} from "@/components/ravenlore";
import {
  toolkitApi,
  wikiApi,
  type NotePlan,
  type NoteSummary,
  type PageSummary,
  type PlanChangeEdit,
  type PlanRelationEdit,
} from "@/lib/api";
import { errMessage, useAsyncData } from "@/lib/use-async";

/** How often the plan is re-read while a run is in flight. */
const POLL_MS = 2500;
/** Give up polling after this long; the run is then stuck, not slow. */
const POLL_GIVE_UP_MS = 300_000;

export default function PlanToolkitPage({ params }: { params: { id: string } }) {
  const { campaign } = useCampaign();
  const { token, isDeveloper } = useAuth();
  const canReview = campaign.my_role === "dm" || isDeveloper;

  const {
    data: notes,
    error: notesError,
    loading: notesLoading,
    reload: reloadNotes,
  } = useAsyncData<NoteSummary[]>(
    (t) => toolkitApi.notes(t, params.id).then((r) => r.notes),
    [params.id],
  );

  // The campaign's pages feed the "@" autocomplete in the editor and the links
  // in its preview - the same index the wiki's own pages render with.
  const { data: pages } = useAsyncData<PageSummary[]>(
    (t) => wikiApi.pages(t, params.id, { limit: 500 }),
    [params.id],
  );
  const linkIndex: LinkIndex | null = useMemo(() => buildLinkIndex(pages ?? []), [pages]);

  const [plan, setPlan] = useState<NotePlan | null>(null);
  const [planLoading, setPlanLoading] = useState(true);
  const [planError, setPlanError] = useState<string | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [selected, setSelected] = useState<Record<string, boolean>>({});

  // The list carries an EXCERPT of each note (a list of 40 notes does not need
  // 40 full bodies), so opening one fetches it whole.
  const {
    data: fullNote,
    error: noteError,
    reload: reloadNote,
  } = useAsyncData<NoteSummary | null>(
    (t) =>
      activeId
        ? toolkitApi.note(t, params.id, activeId).then((r) => r.note)
        : Promise.resolve(null),
    [params.id, activeId],
  );
  const [noteBusy, setNoteBusy] = useState<"create" | "save" | "delete" | null>(null);
  const [planBusy, setPlanBusy] = useState<"generate" | "save" | "confirm" | "discard" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [stuck, setStuck] = useState(false);

  const loadPlan = useCallback(async () => {
    if (!token) return;
    try {
      const res = await toolkitApi.plan(token, params.id);
      setPlan(res.plan);
      setPlanError(null);
    } catch (err) {
      setPlanError(errMessage(err));
    } finally {
      setPlanLoading(false);
    }
  }, [token, params.id]);

  useEffect(() => {
    void loadPlan();
  }, [loadPlan]);

  const running = plan?.status === "generating" || plan?.status === "applying";

  // A generation is asynchronous, so the page polls while it runs. The deadline
  // is not decoration: if the worker never picks the job up (a broker that is
  // down, a container that is not running) the row stays 'generating' forever,
  // and a page that spins silently is worse than one that says it gave up.
  const startedAt = useRef<number | null>(null);
  useEffect(() => {
    if (!running) {
      startedAt.current = null;
      setStuck(false);
      return;
    }
    if (startedAt.current === null) startedAt.current = Date.now();
    const timer = window.setInterval(() => {
      if (startedAt.current !== null && Date.now() - startedAt.current > POLL_GIVE_UP_MS) {
        setStuck(true);
        window.clearInterval(timer);
        return;
      }
      void loadPlan();
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [running, loadPlan]);

  // The selection follows the last generation, so the DM can see what it read
  // and regenerate over the same notes (or add one to them) without re-picking.
  const planNoteIds = plan?.note_ids ?? [];
  const planNoteKey = planNoteIds.join(",");
  useEffect(() => {
    if (!planNoteIds.length) return;
    setSelected(Object.fromEntries(planNoteIds.map((id) => [id, true])));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [planNoteKey]);

  const allNotes = notes ?? [];
  // Only the note the fetch was FOR: switching notes must not briefly render the
  // previous one under the new row's title (the editor holds the draft).
  const activeNote = fullNote && fullNote.id === activeId ? fullNote : null;
  const picked = allNotes.filter((note) => selected[note.id]);

  async function createNote() {
    if (!token) return;
    setNoteBusy("create");
    setNotice(null);
    try {
      const res = await toolkitApi.createNote(token, params.id, { title: "", body: "" });
      setActiveId(res.note.id);
      reloadNotes();
    } catch (err) {
      setPlanError(errMessage(err));
    } finally {
      setNoteBusy(null);
    }
  }

  async function saveNote(patch: { title: string; body: string; status: "draft" | "ready" }) {
    if (!token || !activeNote) return;
    setNoteBusy("save");
    setNotice(null);
    try {
      await toolkitApi.updateNote(token, params.id, activeNote.id, patch);
      // Both: the list shows the new title/preview, the editor the saved body
      // (which is also what clears its "unsaved changes" marker).
      reloadNotes();
      reloadNote();
      setNotice("Note saved.");
    } catch (err) {
      setPlanError(errMessage(err));
    } finally {
      setNoteBusy(null);
    }
  }

  async function deleteNote() {
    if (!token || !activeNote) return;
    const name = activeNote.title || "this note";
    if (!window.confirm('Delete "' + name + '"?\n\nThe note is removed for good. Pages already generated from it stay in the wiki.')) {
      return;
    }
    setNoteBusy("delete");
    try {
      await toolkitApi.deleteNote(token, params.id, activeNote.id);
      setActiveId(null);
      setSelected((prev) => {
        const next = { ...prev };
        delete next[activeNote.id];
        return next;
      });
      reloadNotes();
    } catch (err) {
      setPlanError(errMessage(err));
    } finally {
      setNoteBusy(null);
    }
  }

  async function generate() {
    if (!token || picked.length === 0) return;
    setPlanBusy("generate");
    setPlanError(null);
    setNotice(null);
    try {
      const res = await toolkitApi.generatePlan(
        token,
        params.id,
        picked.map((note) => note.id),
      );
      setPlan(res.plan);
      startedAt.current = Date.now();
    } catch (err) {
      setPlanError(errMessage(err));
    } finally {
      setPlanBusy(null);
    }
  }

  async function savePlan(changes: PlanChangeEdit[], relations: PlanRelationEdit[]) {
    if (!token) return;
    setPlanBusy("save");
    try {
      const res = await toolkitApi.updatePlan(token, params.id, { changes, relations });
      setPlan(res.plan);
      setNotice("Review saved.");
    } catch (err) {
      setPlanError(errMessage(err));
    } finally {
      setPlanBusy(null);
    }
  }

  /** Save what the DM typed, then write the set: the review is one act. */
  async function confirmPlan(changes: PlanChangeEdit[], relations: PlanRelationEdit[]) {
    if (!token) return;
    setPlanBusy("confirm");
    setPlanError(null);
    try {
      const saved = await toolkitApi.updatePlan(token, params.id, { changes, relations });
      setPlan(saved.plan);
      const res = await toolkitApi.confirmPlan(token, params.id);
      setPlan(res.plan);
      startedAt.current = Date.now();
      setNotice("Writing the pages into the wiki…");
    } catch (err) {
      setPlanError(errMessage(err));
    } finally {
      setPlanBusy(null);
    }
  }

  async function discardPlan() {
    if (!token) return;
    if (!window.confirm("Discard this proposal?\n\nNothing is written to the wiki, and the notes stay as they are.")) {
      return;
    }
    setPlanBusy("discard");
    try {
      await toolkitApi.discardPlan(token, params.id);
      setPlan(null);
      setNotice("Proposal discarded.");
    } catch (err) {
      setPlanError(errMessage(err));
    } finally {
      setPlanBusy(null);
    }
  }

  return (
    <AuthGate>
      <div className="space-y-6">
        <header>
          <span className="rl-eyebrow">DM toolkit · Plan</span>
          <h1 className="rl-title mt-1 text-[28px]">Plan</h1>
          <p className="rl-body mt-2 max-w-[80ch]">
            Write down what you have in mind — the world before the first session, or the
            ground you are preparing between two of them. Then pick the notes you want to
            build on and generate: the pages they imply are proposed to you, and nothing
            reaches the wiki until you confirm them. Pages the campaign already has are
            proposed as updates, not as duplicates.
          </p>
        </header>

        {notice ? <Alert tone="success">{notice}</Alert> : null}
        {planError ? <Alert tone="error">{planError}</Alert> : null}
        {stuck ? (
          <Alert tone="info">
            This run has been queued for a while without finishing. The worker may not be
            running — check that the content-service worker is up, then generate again.
          </Alert>
        ) : null}

        {/* ------------------------------------------------ the proposal */}
        <section className="space-y-4" id="plan-review">
          <RlPanel>
            <RlPanelHead
              eyebrow="Generate"
              meta={
                picked.length === 0
                  ? "no notes picked"
                  : picked.length + (picked.length === 1 ? " note picked" : " notes picked")
              }
              action={
                plan && plan.status === "draft" ? (
                  <button
                    type="button"
                    onClick={discardPlan}
                    disabled={planBusy !== null}
                    className="rl-btn rl-btn--ghost rl-btn--sm"
                  >
                    Discard proposal
                  </button>
                ) : undefined
              }
            />
            <div className="flex flex-wrap items-center justify-between gap-3 border-t border-[color:var(--rl-border-parchment)] px-4 py-4">
              <p className="rl-body max-w-[60ch]">
                {picked.length === 0
                  ? "Pick the notes you want the wiki built from, below."
                  : "The pages and links these notes imply are proposed below. Nothing is written until you confirm them."}
              </p>
              <RlButton onClick={generate} disabled={picked.length === 0 || planBusy !== null}>
                {planBusy === "generate" ? "Reading your notes…" : "Generate wiki pages"}
              </RlButton>
            </div>
          </RlPanel>

          {planLoading ? (
            <div className="rl-skeleton h-24" aria-hidden="true" />
          ) : running ? (
            // A row exists but carries no proposal yet: while it runs, the
            // review card below would be an empty set that looks like an answer.
            <RlPanel>
              <div className="flex items-center gap-3 px-4 py-4">
                <RlIconChip name="spark" tint="item" />
                <div>
                  <p className="text-sm font-semibold text-[color:var(--rl-text-on-parchment-primary)]">
                    {plan?.status === "applying"
                      ? "Writing the pages into the wiki…"
                      : "Reading your notes…"}
                  </p>
                  <p className="rl-card-meta">
                    {plan?.status === "applying"
                      ? "The pages land published, with their timeline entries."
                      : "This can take a minute. The proposal appears here as soon as it is ready."}
                  </p>
                </div>
              </div>
            </RlPanel>
          ) : plan && plan.status === "failed" && plan.changes.length === 0 ? (
            // A run that never produced a proposal. The review card would render
            // this as "nothing to write", which reads like a verdict on the
            // notes rather than a failure of the run - so it says what happened
            // and leaves the notes exactly as they are.
            <RlPanel>
              <RlPanelHead eyebrow="Generate" action={<RlTag tint="villain">failed</RlTag>} />
              <div className="border-t border-[color:var(--rl-border-parchment)] px-4 py-4">
                <RlEmptyState icon="flame" tint="villain" title="The notes could not be read">
                  {plan.error ??
                    "The run failed before proposing anything. Your notes are untouched — generate again."}
                </RlEmptyState>
              </div>
            </RlPanel>
          ) : (
            <PlanReviewCard
              plan={plan}
              campaignId={params.id}
              linkIndex={linkIndex}
              pages={pages ?? []}
              source="toolkit"
              canReview={canReview}
              busy={planBusy === "save" || planBusy === "confirm" ? planBusy : null}
              // The page-level alert already reports transport/action errors;
              // the card reports the set's OWN recorded error (a failed apply).
              error={null}
              onSave={savePlan}
              onConfirm={confirmPlan}
            />
          )}
        </section>

        {/* -------------------------------------------------- the notes */}
        <section className="grid gap-4 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
          <NotesList
            notes={allNotes}
            activeId={activeId}
            selected={selected}
            loading={notesLoading}
            error={notesError}
            busy={noteBusy !== null}
            campaignId={params.id}
            linkIndex={linkIndex}
            onOpen={setActiveId}
            onToggleSelected={(noteId) =>
              setSelected((prev) => ({ ...prev, [noteId]: !prev[noteId] }))
            }
            onSelectAll={() =>
              setSelected(Object.fromEntries(allNotes.map((note) => [note.id, true])))
            }
            onClearSelection={() => setSelected({})}
            onCreate={createNote}
          />

          {/* While the open note is on its way - a first load OR a switch, which
              must not render the previous note's draft under the new title. */}
          {activeId && !activeNote && !noteError ? (
            <div className="rl-skeleton min-h-[24rem]" aria-hidden="true" />
          ) : activeNote ? (
            <NoteEditor
              // Remounting on a different note is what resets the draft fields;
              // an effect doing it by hand is the same thing, less reliably.
              key={activeNote.id}
              note={activeNote}
              pages={pages ?? []}
              linkIndex={linkIndex}
              campaignId={params.id}
              busy={noteBusy !== null}
              error={null}
              onSave={saveNote}
              onDelete={deleteNote}
            />
          ) : (
            <RlPanel className="grid place-items-center">
              <div className="px-4 py-10">
                <RlEmptyState
                  icon="scroll"
                  tint="muted"
                  title={allNotes.length === 0 ? "No note open" : "Pick a note to read it"}
                >
                  {allNotes.length === 0
                    ? "Create a note and write freely: a place you are inventing, a faction, the shape of the next session. Nothing here is published."
                    : "Open a note from the list to edit it, or create a new one."}
                </RlEmptyState>
              </div>
            </RlPanel>
          )}
        </section>

        <p className="rl-card-meta">
          Notes are yours alone: players never see them. Pages generated from them are
          normal wiki pages — edit, hide or archive them like any other.
        </p>
      </div>
    </AuthGate>
  );
}
