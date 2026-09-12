// /campaigns/[id]/sessions/[sessionId] — session detail: upload the recording,
// listen to it, watch the pipeline, review the session summary, review the
// proposed wiki changes it implies, read the transcript, and (DM) name
// speakers + confirm both review layers.

"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Badge, Button, Card, EmptyState, Field, FileInput, Select, SessionStatusBadge, TextInput, fmtDate, fmtDuration, fmtPercent } from "@/components/ui";
import { AudioPlayer } from "@/components/session/audio-player";
import { SessionPlanCard } from "@/components/session/session-plan";
import { SessionSummaryCard } from "@/components/session/session-summary";
import { LOW_SPEAKER_CONFIDENCE, TranscriptViewer, speakerConfidenceByLabel } from "@/components/session/transcript";
import { AuthGate, useAuth } from "@/lib/auth";
import { campaignsApi, contentApi, objectUrl, sessionsApi, usersApi, wikiApi, type Campaign, type CampaignMember, type PageSummary, type PlanChangeEdit, type PlanRelationEdit, type SessionDetail, type SessionPlan, type SessionSummary, type SpeakerAssignment, type SummaryEdit } from "@/lib/api";
import { buildLinkIndex, type LinkIndex } from "@/components/linked-text";
import { errMessage, useAsyncData } from "@/lib/use-async";

const ACTIVE_STATUSES = new Set([
  "uploaded",
  "recorded",
  "transcribing",
  "transcribed",
  "refining",
  "refined",
  "identifying_speakers",
  "speakers_identified",
  "speaker_pending",
  "summarizing",
  "generating_wiki",
  "applying_wiki",
]);

// While the pipeline is still transcribing/refining, the object behind
// transcript_url holds the RAW transcription (progressive chunks, then the
// pre-refiner output). It is never surfaced: the page only shows the
// transcript once the refiner has rewritten it (status 'refined' or later).
const TRANSCRIPT_PENDING_STATUSES = new Set([
  "transcribing",
  "transcribed",
  "refining",
]);

export default function SessionDetailPage({ params }: { params: { id: string; sessionId: string } }) {
  const { token, isDeveloper } = useAuth();
  const { data: campaign } = useAsyncData<Campaign>((t) => campaignsApi.get(t, params.id), [params.id]);
  const { data: session, error, loading, reload } = useAsyncData<SessionDetail>((t) => sessionsApi.get(t, params.sessionId), [params.sessionId]);
  const { data: speakers, reload: reloadSpeakers } = useAsyncData<SpeakerAssignment[]>((t) => sessionsApi.speakers(t, params.sessionId), [params.sessionId]);
  const { data: members } = useAsyncData<CampaignMember[]>((t) => campaignsApi.members(t, params.id), [params.id]);
  const { data: summary, reload: reloadSummary } = useAsyncData<SessionSummary | null>(
    async (t) => {
      try {
        return (await contentApi.summary(t, params.sessionId)).summary;
      } catch {
        return null; // content-service may be down; not fatal for the page
      }
    },
    [params.sessionId],
  );

  // The proposed wiki changes: the 'git status' of the session. Nothing is
  // written to the wiki until the DM confirms this set.
  const { data: plan, reload: reloadPlan } = useAsyncData<SessionPlan | null>(
    async (t) => {
      try {
        return (await contentApi.plan(t, params.sessionId)).plan;
      } catch {
        return null; // not a DM, or content-service down: no plan to show
      }
    },
    [params.sessionId],
  );

  // Campaign page index: entity names in the session summary link to their
  // wiki pages (the pages written when the changes are confirmed included).
  const { data: pages, reload: reloadPages } = useAsyncData<PageSummary[]>((t) => wikiApi.pages(t, params.id, { limit: 500 }), [params.id]);
  const linkIndex: LinkIndex | null = useMemo(() => buildLinkIndex(pages ?? []), [pages]);

  // The prompt version the deployment currently uses — the summary badge
  // tracks prompt evolution instead of the version frozen on the last run.
  const { data: llmConfig } = useAsyncData<{ provider: string; model: string; prompt_version: string }>((t) => contentApi.llm(t), []);
  const currentPromptVersion = llmConfig?.prompt_version ?? null;

  const [file, setFile] = useState<File | null>(null);
  const [replacing, setReplacing] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editForm, setEditForm] = useState({ title: "", session_no: "" });
  const [assignForm, setAssignForm] = useState<Record<string, { member_id: string }>>({});
  const [busy, setBusy] = useState(false);
  const [reviewBusy, setReviewBusy] = useState<"regenerate" | "confirm" | null>(null);
  const [planBusy, setPlanBusy] = useState<"save" | "confirm" | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  // The session is polled while the pipeline runs, and every poll returns a
  // freshly signed presigned URL. The <audio> src must stay stable across those
  // refreshes (same object path) or playback would restart every 10 s.
  const [audioSrc, setAudioSrc] = useState<string | null>(null);
  useEffect(() => {
    if (!session?.raw_audio_url) return;
    const next = objectUrl(session.raw_audio_url);
    setAudioSrc((prev) => {
      if (prev && prev.split("?")[0] === next?.split("?")[0]) return prev;
      return next;
    });
  }, [session?.raw_audio_url]);
  const audioRef = useRef<HTMLAudioElement>(null);
  const seek = useCallback((seconds: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = seconds;
    void audio.play().catch(() => undefined);
  }, []);

  // user_id -> display_name for campaign members + assigned speakers, so the
  // speaker list and the member picker show names instead of uuids.
  const [userNames, setUserNames] = useState<Record<string, string>>({});
  useEffect(() => {
    if (!token) {
      setUserNames({});
      return;
    }
    let cancelled = false;
    (async () => {
      const ids: string[] = [];
      const addId = (id: string) => {
        if (!ids.includes(id)) ids.push(id);
      };
      members?.forEach((m) => {
        if (m.user_id) addId(m.user_id);
      });
      speakers?.forEach((s) => {
        if (s.user_id) addId(s.user_id);
      });
      const names: Record<string, string> = {};
      await Promise.all(
        ids.map(async (uid) => {
          try {
            const u = await usersApi.get(token, uid);
            if (!cancelled) names[uid] = u.display_name;
          } catch {
            /* fall back to the raw id */
          }
        }),
      );
      if (!cancelled) setUserNames(names);
    })();
    return () => {
      cancelled = true;
    };
  }, [token, members, speakers]);

  // member_id -> display label: the linked user display name when the
  // member has an account, otherwise the DM-curated player name.
  const memberNames = useMemo(() => {
    const names: Record<string, string> = {};
    for (const m of members ?? []) {
      names[m.id] = m.user_id ? (userNames[m.user_id] ?? m.player_name) : m.player_name;
    }
    return names;
  }, [members, userNames]);

  // speaker_label -> display name for the transcript chips.
  const speakerNames = useMemo(() => {
    const names: Record<string, string> = {};
    for (const s of speakers ?? []) {
      if (s.user_id) names[s.speaker_label] = userNames[s.user_id] ?? s.user_id;
      else if (s.member_id) names[s.speaker_label] = memberNames[s.member_id] ?? s.member_id;
    }
    return names;
  }, [speakers, userNames, memberNames]);

  // Diarization confidence per speaker label (min across the label's parts),
  // read from diarization.json — the Deepgram worker records speaker_confidence
  // per segment; the refiner/speaker-service preserve the field. Best-effort:
  // a failed fetch only disables the confidence highlighting, nothing else.
  const [diarizationSegments, setDiarizationSegments] = useState<Array<{ start: number; end: number; speaker?: string; speaker_confidence?: number | null }> | null>(null);
  useEffect(() => {
    if (!session?.diarization_url) {
      setDiarizationSegments(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const url = objectUrl(session.diarization_url);
        const r = await fetch(url!);
        if (!r.ok) throw new Error('diarization fetch failed (' + r.status + ')');
        const doc = (await r.json()) as { segments?: Array<{ start: number; end: number; speaker?: string; speaker_confidence?: number | null }> };
        if (!cancelled) setDiarizationSegments(doc.segments ?? []);
      } catch {
        if (!cancelled) setDiarizationSegments(null);
      }
    })();
    return () => {
      cancelled = true;
    };
    // Refetch when the pipeline moves (status) or the object path changes.
    // The signed URL itself rotates every poll — strip the signature.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.id, session?.status, session?.diarization_url?.split('?')[0]]);

  const labelConfidence = useMemo(
    () => speakerConfidenceByLabel(diarizationSegments ?? []),
    [diarizationSegments],
  );

  // Poll while the pipeline is still moving. Keyed on the status string only
  // (not the whole session object), so steady-state refreshes do not restart
  // the interval and background reloads never flicker the page to "Loading".
  useEffect(() => {
    if (!session?.status || !ACTIVE_STATUSES.has(session.status)) return;
    const t = window.setInterval(() => {
      reload();
      reloadSummary();
      reloadSpeakers();
      reloadPlan();
      // Pages written by the running pipeline can appear at any point — keep
      // the link index fresh so summary names resolve to them.
      reloadPages();
    }, 10_000);
    return () => window.clearInterval(t);
  }, [session?.status, reload, reloadSummary, reloadSpeakers, reloadPlan, reloadPages]);

  // The wiki pages only appear once the PROPOSED CHANGES are confirmed
  // (status moves to content_ready, which stops the poll above): refresh the
  // link index on every status change so the names in the summary become
  // clickable as soon as their pages exist.
  useEffect(() => {
    if (!session?.status) return;
    reloadPages();
  }, [session?.status, reloadPages]);

  // A summary review action moves the session through summarizing /
  // generating_wiki and back. While it runs, the page polls faster than the
  // 10 s pipeline interval so the new revision (or the created pages) shows up
  // promptly; the action ends once the queue picked the work up AND the
  // session settled on its next resting state again.
  const settleTarget = reviewBusy === "regenerate" ? "summary_ready" : reviewBusy === "confirm" ? "content_ready" : null;
  const sawWorking = useRef(false);
  useEffect(() => {
    if (!reviewBusy || !settleTarget) {
      sawWorking.current = false;
      return;
    }
    const started = Date.now();
    const t = window.setInterval(() => {
      reload();
      reloadSummary();
      if (settleTarget === "content_ready") reloadPages();
      const status = session?.status;
      if (status === "summarizing" || status === "generating_wiki") sawWorking.current = true;
      const waited = Date.now() - started;
      // settled: back on the resting status after the worker ran; the
      // fallbacks (30 s / 3 min) keep a lost message from locking the UI.
      const settled =
        status === settleTarget &&
        (sawWorking.current || waited > 30_000);
      if (settled || status === "failed" || waited > 180_000) {
        sawWorking.current = false;
        setReviewBusy(null);
        reloadSpeakers();
      }
    }, 2_500);
    return () => window.clearInterval(t);
  }, [reviewBusy, settleTarget, session?.status, reload, reloadSummary, reloadPages, reloadSpeakers]);

  async function regenerateSummary(edits: SummaryEdit[], lines: string[]) {
    if (!token) return;
    setReviewBusy("regenerate");
    setFormError(null);
    setNotice(null);
    try {
      const res = await contentApi.regenerateSummary(token, params.sessionId, {
        edits,
        summary_lines: lines,
      });
      setNotice(
        "Rewriting the summary (revision " +
          res.revision +
          " → " +
          (res.revision + 1) +
          "). It refreshes in a few seconds.",
      );
      reload();
      reloadSummary();
    } catch (err) {
      setReviewBusy(null);
      setFormError(errMessage(err));
    }
  }

  async function confirmSummary() {
    if (!token) return;
    setReviewBusy("confirm");
    setFormError(null);
    setNotice(null);
    try {
      await contentApi.confirmSummary(token, params.sessionId);
      setNotice(
        "Summary confirmed — the pages and events it implies are being proposed for your review.",
      );
      reload();
      reloadSummary();
      reloadPlan();
    } catch (err) {
      setReviewBusy(null);
      setFormError(errMessage(err));
    }
  }

  // The proposed changes are reviewed with the same poll-and-settle pattern as
  // the summary: the session moves through generating_wiki / applying_wiki and
  // back, and the card refreshes when it lands.
  const planSettleTarget =
    planBusy === "confirm" ? "content_ready" : planBusy === "save" ? "wiki_plan_ready" : null;
  const planSawWorking = useRef(false);
  useEffect(() => {
    if (!planBusy || !planSettleTarget) {
      planSawWorking.current = false;
      return;
    }
    const started = Date.now();
    const t = window.setInterval(() => {
      reload();
      reloadPlan();
      const status = session?.status;
      if (status === "generating_wiki" || status === "applying_wiki") {
        planSawWorking.current = true;
      }
      const waited = Date.now() - started;
      const settled =
        status === planSettleTarget && (planSawWorking.current || waited > 30_000);
      if (settled || status === "failed" || waited > 180_000) {
        planSawWorking.current = false;
        setPlanBusy(null);
        if (planSettleTarget === "content_ready") reloadPages();
      }
    }, 2_500);
    return () => window.clearInterval(t);
  }, [planBusy, planSettleTarget, session?.status, reload, reloadPlan, reloadPages]);

  async function savePlan(changes: PlanChangeEdit[], relations: PlanRelationEdit[]) {
    if (!token) return;
    setPlanBusy("save");
    setPlanError(null);
    try {
      await contentApi.updatePlan(token, params.sessionId, { changes, relations });
      setNotice("Review saved.");
      reloadPlan();
    } catch (err) {
      setPlanError(errMessage(err));
    } finally {
      setPlanBusy(null);
    }
  }

  async function confirmPlan(changes: PlanChangeEdit[], relations: PlanRelationEdit[]) {
    if (!token) return;
    setPlanBusy("confirm");
    setPlanError(null);
    setNotice(null);
    try {
      // Unsaved edits first: what the DM sees is what gets written.
      if (changes.length || relations.length) {
        await contentApi.updatePlan(token, params.sessionId, { changes, relations });
      }
      await contentApi.confirmPlan(token, params.sessionId);
      setNotice("Changes confirmed — writing the pages and the timeline entries.");
      reload();
      reloadPlan();
    } catch (err) {
      setPlanBusy(null);
      setPlanError(errMessage(err));
    }
  }

  async function upload(e: FormEvent) {
    e.preventDefault();
    if (!token || !file) return;
    setBusy(true);
    setFormError(null);
    try {
      const updated = await sessionsApi.uploadRecording(token, params.sessionId, file);
      setNotice(`Recording uploaded — status is now "${updated.status}". The pipeline has started.`);
      setFile(null);
      setReplacing(false);
      setAudioSrc(objectUrl(updated.raw_audio_url));
      reload();
      reloadSpeakers();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  function askReplace() {
    if (
      !window.confirm(
        "Replace the recording?\n\nThe current transcript and diarization will be erased and the pipeline will restart from scratch.",
      )
    ) {
      return;
    }
    setReplacing(true);
  }

  function startEdit() {
    setEditForm({
      title: session?.title ?? "",
      session_no: session?.session_no != null ? String(session.session_no) : "",
    });
    setEditing(true);
  }

  async function saveMeta(e: FormEvent) {
    e.preventDefault();
    if (!token) return;
    setBusy(true);
    setFormError(null);
    try {
      await sessionsApi.update(token, params.sessionId, {
        title: editForm.title.trim() || null,
        session_no: editForm.session_no.trim() ? Number(editForm.session_no) : null,
      });
      setEditing(false);
      setNotice("Session details updated.");
      reload();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function assign(speakerLabel: string) {
    if (!token) return;
    const f = assignForm[speakerLabel];
    if (!f?.member_id) return;
    setBusy(true);
    setFormError(null);
    try {
      await sessionsApi.assignSpeaker(token, params.sessionId, speakerLabel, f.member_id);
      const name = memberNames[f.member_id] ?? f.member_id;
      setNotice(`Speaker ${speakerLabel} named "${name}".`);
      // keep the dropdown on the saved choice (the reload below makes the
      // unchanged-check disable the button) — no need to clear it
      setAssignForm((prev) => ({ ...prev, [speakerLabel]: { member_id: f.member_id } }));
      reloadSpeakers();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  if (loading) return <p className="text-sm text-slate-400">Loading session…</p>;
  if (error || !session) return <Alert tone="error">{error ?? "Session not found"}</Alert>;

  const isDm = campaign?.my_role === "dm";

  // Once every diarized speaker has been named, the Speakers section adds no
  // information for players — hide it. The DM always keeps it: assignments can
  // be corrected later (especially the low-confidence labels highlighted
  // below), so the panel must stay reachable after every speaker has a name.
  const allSpeakersAssigned =
    (speakers?.length ?? 0) > 0 && (speakers ?? []).every((s) => s.member_id || s.user_id);
  const showSpeakersCard =
    (speakers?.length ?? 0) > 0 && (isDm || !allSpeakersAssigned);

  // The summary review is the DM's job (developer accounts keep the escape
  // hatch); players read the summary and the links it produces.
  const canReviewSummary = isDm || isDeveloper;

  return (
    <AuthGate>
    <div className="space-y-6">
      <div>
        <Link href={`/campaigns/${params.id}`} className="text-xs text-slate-500 hover:text-slate-300">← back to campaign</Link>
        <div className="mt-1 flex flex-wrap items-center gap-3">
          <h1 className="text-3xl font-bold">{session.title ?? "Untitled session"}</h1>
          {session.session_no ? <Badge tone="slate">#{session.session_no}</Badge> : null}
          <SessionStatusBadge status={session.status} />
        </div>
        <p className="mt-2 text-sm text-slate-400">
          recorded {fmtDate(session.recorded_at)} · duration {fmtDuration(session.duration_sec)}
        </p>
        {session.error ? <p className="mt-2 text-sm text-red-300">error: {session.error}</p> : null}
      </div>

      {notice ? <Alert tone="success">{notice}</Alert> : null}
      {formError ? <Alert tone="error">{formError}</Alert> : null}

      <SessionSummaryCard
        summary={summary}
        campaignId={params.id}
        linkIndex={linkIndex}
        currentPromptVersion={currentPromptVersion}
        sessionStatus={session.status}
        canReview={canReviewSummary}
        reviewBusy={reviewBusy}
        onRegenerateSummary={regenerateSummary}
        onConfirmSummary={confirmSummary}
        onSeek={seek}
      />

      {isDm || isDeveloper ? (
        <SessionPlanCard
          plan={plan}
          campaignId={params.id}
          linkIndex={linkIndex}
          canReview={canReviewSummary}
          busy={planBusy}
          error={planError}
          onSave={savePlan}
          onConfirm={confirmPlan}
        />
      ) : null}

      {isDm ? (
        <Card>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-lg font-semibold">Session details</h2>
            {!editing ? (
              <Button variant="secondary" onClick={startEdit}>Edit</Button>
            ) : null}
          </div>
          {editing ? (
            <form onSubmit={saveMeta} className="mt-4 grid gap-3 sm:grid-cols-[1fr_8rem_auto]">
              <Field label="Title">
                <TextInput value={editForm.title} onChange={(e) => setEditForm({ ...editForm, title: e.target.value })} />
              </Field>
              <Field label="Session #">
                <TextInput type="number" min={1} value={editForm.session_no} onChange={(e) => setEditForm({ ...editForm, session_no: e.target.value })} />
              </Field>
              <div className="flex items-end gap-2">
                <Button type="submit" disabled={busy}>Save</Button>
                <Button variant="ghost" onClick={() => setEditing(false)}>Cancel</Button>
              </div>
            </form>
          ) : null}
        </Card>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-lg font-semibold">Upload recording</h2>
            {session.raw_audio_url && !replacing ? (
              <Button variant="danger" onClick={askReplace}>Replace recording</Button>
            ) : null}
          </div>

          {session.raw_audio_url && !replacing ? (
            <div className="rounded-lg border border-slate-800 bg-slate-800/40 px-4 py-3 text-sm text-slate-400">
              A recording is already attached — the transcript and diarization were generated from it.
              Replacing it erases both and restarts the pipeline, so this is only possible through the
              "Replace recording" button above.
            </div>
          ) : null}

          {!session.raw_audio_url || replacing ? (
            <form onSubmit={upload} className="space-y-3">
              {replacing ? (
                <Alert tone="info">
                  You are replacing the current recording — the existing transcript and diarization will be erased.
                </Alert>
              ) : null}
              <Field label="Audio file" hint="Phone recording (m4a/mp3/wav…)">
                <FileInput accept="audio/*" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
              </Field>
              <div className="flex gap-2">
                <Button type="submit" disabled={busy || !file}>
                  {session.raw_audio_url ? "Replace & restart pipeline" : "Upload & start pipeline"}
                </Button>
                {session.raw_audio_url ? (
                  <Button variant="ghost" type="button" onClick={() => setReplacing(false)}>Cancel</Button>
                ) : null}
              </div>
            </form>
          ) : null}
        </Card>

        <Card>
          <h2 className="mb-4 text-lg font-semibold">Recording</h2>
          <AudioPlayer src={audioSrc} audioRef={audioRef} />
        </Card>
      </div>

      <Card>
        <h2 className="mb-4 text-lg font-semibold">Transcript</h2>
        <TranscriptViewer
          transcriptUrl={TRANSCRIPT_PENDING_STATUSES.has(session.status) ? null : session.transcript_url}
          pendingHint={TRANSCRIPT_PENDING_STATUSES.has(session.status)
            ? "Transcription and refinement in progress — the refined transcript will appear here once it is ready."
            : null}
          speakerNames={speakerNames}
          onSeek={seek}
        />
      </Card>

      {showSpeakersCard ? (
        <Card>
          <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-lg font-semibold">Speakers</h2>
            <span className="text-xs text-slate-500">
              Red labels have low diarization confidence — double-check them. The DM can change any assignment; naming a user-linked speaker also enrolls their voice.
            </span>
          </div>
          {speakers && speakers.length === 0 ? (
            <EmptyState>No diarized speakers yet — they appear after transcription.</EmptyState>
          ) : (
            <div className="divide-y divide-slate-800">
              {speakers?.map((s) => {
                const unassigned = !s.member_id && !s.user_id;
                const currentMember = s.member_id ?? "";
                const f = assignForm[s.speaker_label] ?? { member_id: currentMember };
                const labelConf = labelConfidence[s.speaker_label];
                const lowConfidence = labelConf !== undefined && labelConf < LOW_SPEAKER_CONFIDENCE;
                const assignedName = s.user_id
                  ? userNames[s.user_id] ?? s.user_id
                  : s.member_id
                    ? memberNames[s.member_id] ?? s.member_id
                    : null;
                return (
                  <div key={s.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
                    <div>
                      <div className="flex items-center gap-2 text-sm font-medium text-slate-100">
                        <span className={lowConfidence ? "font-mono text-red-300" : "font-mono"}>{s.speaker_label}</span>
                        <Badge tone={s.status === "confirmed" ? "green" : s.status === "auto" ? "blue" : "amber"}>{s.status}</Badge>
                        {lowConfidence ? (
                          <span title={`Diarization confidence ${fmtPercent(labelConf)} is below the ${Math.round(LOW_SPEAKER_CONFIDENCE * 100)}% threshold — the attribution may be wrong`}>
                            <Badge tone="red">⚠ low confidence</Badge>
                          </span>
                        ) : null}
                      </div>
                      <div className="mt-0.5 text-xs text-slate-500">
                        {assignedName ? (
                          <span className="font-medium text-slate-300">{assignedName}</span>
                        ) : (
                          "not assigned — needs a name"
                        )}
                        {s.confidence !== null && s.confidence !== undefined ? ` · match ${Math.round(s.confidence * 100)}%` : ""}
                        {labelConf !== undefined ? ` · diarization ${fmtPercent(labelConf)}` : ""}
                      </div>
                    </div>
                    {isDm ? (
                      <div className="flex flex-wrap items-end gap-2">
                        <Select
                          className="w-72"
                          value={f.member_id}
                          onChange={(e) => setAssignForm((prev) => ({ ...prev, [s.speaker_label]: { member_id: e.target.value } }))}
                        >
                          <option value="">Choose a member…</option>
                          {/* Every campaign member is assignable — userless players
                              included (their voice is labeled, but cannot be enrolled
                              to a user-keyed voiceprint). */}
                          {members?.map((m) => (
                            <option key={m.id} value={m.id}>
                              {m.user_id ? (userNames[m.user_id] ?? m.player_name) : m.player_name}
                              {m.role === "dm" ? " (DM)" : ""}
                            </option>
                          ))}
                        </Select>
                        <Button
                          onClick={() => assign(s.speaker_label)}
                          disabled={busy || !f.member_id || f.member_id === currentMember}
                        >
                          {unassigned ? "Name speaker" : "Save assignment"}
                        </Button>
                      </div>
                    ) : unassigned ? (
                      <span className="text-xs text-slate-500">awaiting DM assignment</span>
                    ) : null}
                  </div>
                );
              })}
            </div>
          )}
        </Card>
      ) : null}
    </div>
    </AuthGate>
  );
}
