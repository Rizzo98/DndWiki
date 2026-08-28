// /campaigns/[id]/sessions/[sessionId] — session detail: upload the recording,
// listen to it, watch the pipeline, read the transcript, and (DM) name speakers.

"use client";

import Link from "next/link";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Badge, Button, Card, EmptyState, Field, FileInput, Select, SessionStatusBadge, TextInput, fmtDate, fmtDuration } from "@/components/ui";
import { AudioPlayer } from "@/components/session/audio-player";
import { SessionSummaryCard } from "@/components/session/session-summary";
import { TranscriptViewer } from "@/components/session/transcript";
import { AuthGate, useAuth } from "@/lib/auth";
import { campaignsApi, contentApi, objectUrl, sessionsApi, usersApi, wikiApi, type Campaign, type CampaignMember, type PageSummary, type SessionDetail, type SessionSummary, type SpeakerAssignment } from "@/lib/api";
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
  "generating_wiki",
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

  // Campaign page index: entity names in the session summary link to their
  // wiki pages (drafts produced by this session included).
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
  const [regenBusy, setRegenBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  // DEBUG (developer accounts): queue a fresh generation run for the SAME
  // transcript. The pipeline poll below picks the new status up automatically.
  async function regenerateWiki() {
    if (!token) return;
    if (
      !window.confirm(
        "Re-run wiki generation from this session's transcript?\n\nThe current AI summary will be overwritten and fresh pending drafts created.",
      )
    ) {
      return;
    }
    setRegenBusy(true);
    setFormError(null);
    try {
      const res = await contentApi.regenerate(token, params.sessionId);
      setNotice(
        "Generation queued (" +
          res.llm_model +
          ", prompt " +
          res.prompt_version +
          ") — the summary refreshes as the pipeline runs.",
      );
      reload();
      reloadSummary();
      reloadSpeakers();
      reloadPages();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setRegenBusy(false);
    }
  }

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

  // Poll while the pipeline is still moving. Keyed on the status string only
  // (not the whole session object), so steady-state refreshes do not restart
  // the interval and background reloads never flicker the page to "Loading".
  useEffect(() => {
    if (!session?.status || !ACTIVE_STATUSES.has(session.status)) return;
    const t = window.setInterval(() => {
      reload();
      reloadSummary();
      reloadSpeakers();
      // Draft pages created by the running pipeline can appear at any point —
      // keep the link index fresh so summary names resolve to them.
      reloadPages();
    }, 10_000);
    return () => window.clearInterval(t);
  }, [session?.status, reload, reloadSummary, reloadSpeakers, reloadPages]);

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
      setAssignForm((prev) => ({ ...prev, [speakerLabel]: { member_id: "" } }));
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
  // information — hide it entirely (it only exists to drive the assignment).
  const allSpeakersAssigned =
    (speakers?.length ?? 0) > 0 && (speakers ?? []).every((s) => s.member_id || s.user_id);

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
        onSeek={(seconds) => {
          const audio = audioRef.current;
          if (!audio) return;
          audio.currentTime = seconds;
          void audio.play().catch(() => undefined);
        }}
        {...(isDeveloper ? { onRegenerate: regenerateWiki, regenerating: regenBusy } : {})}
      />

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
          transcriptUrl={session.transcript_url}
          speakerNames={speakerNames}
          onSeek={(seconds) => {
            const audio = audioRef.current;
            if (!audio) return;
            audio.currentTime = seconds;
            void audio.play().catch(() => undefined);
          }}
        />
      </Card>

      {!allSpeakersAssigned ? (
        <Card>
          <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-lg font-semibold">Speakers</h2>
            <span className="text-xs text-slate-500">
              Naming a user-linked speaker enrolls their voice — future sessions are named automatically.
            </span>
          </div>
          {speakers && speakers.length === 0 ? (
            <EmptyState>No diarized speakers yet — they appear after transcription.</EmptyState>
          ) : (
            <div className="divide-y divide-slate-800">
              {speakers?.map((s) => {
                const unassigned = !s.member_id && !s.user_id;
                const f = assignForm[s.speaker_label] ?? { member_id: "" };
                const assignedName = s.user_id
                  ? userNames[s.user_id] ?? s.user_id
                  : s.member_id
                    ? memberNames[s.member_id] ?? s.member_id
                    : null;
                return (
                  <div key={s.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
                    <div>
                      <div className="flex items-center gap-2 text-sm font-medium text-slate-100">
                        <span className="font-mono">{s.speaker_label}</span>
                        <Badge tone={s.status === "confirmed" ? "green" : s.status === "auto" ? "blue" : "amber"}>{s.status}</Badge>
                      </div>
                      <div className="mt-0.5 text-xs text-slate-500">
                        {assignedName ? (
                          <span className="font-medium text-slate-300">{assignedName}</span>
                        ) : (
                          "not assigned — needs a name"
                        )}
                        {s.confidence !== null && s.confidence !== undefined ? ` · match ${Math.round(s.confidence * 100)}%` : ""}
                      </div>
                    </div>
                    {unassigned && isDm ? (
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
                        <Button onClick={() => assign(s.speaker_label)} disabled={busy || !f.member_id}>Name speaker</Button>
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