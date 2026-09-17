// Sessions tab: one panel per recording.
//
// A session is not a row of metadata — it is a piece of audio that walks through
// a six-stage pipeline and, at three points in that walk, stops and waits for
// the DM. The card is built to say which stage it is on and whether it is
// waiting for you, and it is filled out with the recording's own envelope so a
// list of them reads as a shelf of tapes rather than a table.
//
// A session can be DELETED until its wiki updates exist ('can_delete' from the
// API): once the pages are written they would outlive the session that made them.

"use client";

import Link from "next/link";
import { FormEvent, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Field,
  SessionStatusBadge,
  TextInput,
  fmtDuration,
} from "@/components/ui";
import {
  RlEmptyState,
  RlIcon,
  RlIconChip,
  RlTag,
  fmtRelative,
  rlTintVars,
  sessionStamp,
  sessionStatusTint,
} from "@/components/ravenlore";
import { useAuth } from "@/lib/auth";
import { sessionsApi, type Campaign, type Session } from "@/lib/api";
import { SESSION_STAGES, sessionIsWorking, sessionNeedsDm, sessionStage, sessionStatusNote } from "@/lib/session-status";
import { waveformPeaks } from "@/lib/waveform";
import { errMessage, useAsyncData } from "@/lib/use-async";

/** Bars in a card's decorative envelope (the player draws a wider one). */
const CARD_BARS = 54;

export function SessionsTab({ campaign }: { campaign: Campaign }) {
  const { token } = useAuth();
  const { data: sessions, error, loading, reload } = useAsyncData<Session[]>((t) => sessionsApi.list(t, campaign.id), [campaign.id]);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({ title: "", session_no: "" });
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  // Newest first: a landing page should open on the last thing that happened.
  const ordered = useMemo(
    () => [...(sessions ?? [])].sort((a, b) => sessionStamp(b) - sessionStamp(a)),
    [sessions],
  );
  const waiting = ordered.filter((s) => sessionNeedsDm(s.status)).length;
  const runtime = ordered.reduce((total, s) => total + (s.duration_sec ?? 0), 0);
  const summaryLine = [
    ordered.length + (ordered.length === 1 ? " recording" : " recordings"),
    runtime > 0 ? spellRuntime(runtime) : null,
    waiting > 0 ? waiting + " waiting for you" : null,
  ]
    .filter(Boolean)
    .join(" · ");

  async function createSession(e: FormEvent) {
    e.preventDefault();
    if (!token) return;
    setBusy(true);
    setFormError(null);
    try {
      const session = await sessionsApi.create(token, {
        campaign_id: campaign.id,
        title: form.title.trim() || null,
        session_no: form.session_no.trim() ? Number(form.session_no) : null,
      });
      setNotice(`Session created — status ${session.status}.`);
      setForm({ title: "", session_no: "" });
      setShowCreate(false);
      reload();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function deleteSession(session: Session) {
    if (!token) return;
    const label = session.title ?? "Untitled session";
    if (
      !window.confirm(
        `Delete "${label}"?\n\nThe recording, the transcript and everything generated from this session are removed. This cannot be undone.`,
      )
    ) {
      return;
    }
    setBusy(true);
    setFormError(null);
    setNotice(null);
    try {
      await sessionsApi.remove(token, session.id);
      setNotice(`Session "${label}" deleted.`);
      reload();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      {notice ? <Alert tone="success">{notice}</Alert> : null}
      {formError ? <Alert tone="error">{formError}</Alert> : null}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="rl-card-meta">
          {loading ? "Loading recordings…" : ordered.length === 0 ? "Nothing recorded yet" : summaryLine}
        </p>
        <Button
          variant={showCreate ? "ghost" : ordered.length === 0 ? "primary" : "secondary"}
          onClick={() => setShowCreate((v) => !v)}
        >
          {showCreate ? "Cancel" : "New session"}
        </Button>
      </div>

      {showCreate ? (
        <Card>
          <h2 className="rl-title mb-4 text-lg">New session</h2>
          <form onSubmit={createSession} className="grid gap-3 sm:grid-cols-[1fr_8rem_auto]">
            <Field label="Title">
              <TextInput value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} placeholder="The Siege of Vharn" />
            </Field>
            <Field label="Session #">
              <TextInput type="number" min={1} value={form.session_no} onChange={(e) => setForm({ ...form, session_no: e.target.value })} placeholder="3" />
            </Field>
            <div className="flex items-end">
              <Button type="submit" disabled={busy}>Create</Button>
            </div>
          </form>
          <p className="rl-card-meta mt-3">
            The recording is uploaded on the session itself — the pipeline starts as soon as the file is in.
          </p>
        </Card>
      ) : null}

      {loading ? (
        <div className="grid gap-4 sm:grid-cols-2" aria-hidden="true">
          <div className="rl-skeleton h-64" />
          <div className="rl-skeleton h-64" />
        </div>
      ) : error ? (
        <Alert tone="error">{error}</Alert>
      ) : ordered.length === 0 ? (
        <RlEmptyState
          icon="mic"
          tint="accent"
          title="No recordings yet"
          action={
            <Button onClick={() => setShowCreate(true)}>
              Create the first session
            </Button>
          }
        >
          Create a session, then upload the phone recording on its page: the pipeline transcribes it, works out who
          spoke, and proposes the wiki changes it implies.
        </RlEmptyState>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          {ordered.map((session) => (
            <SessionCard
              key={session.id}
              campaign={campaign}
              session={session}
              busy={busy}
              onDelete={() => deleteSession(session)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// -------------------------------------------------------------------- pieces

function SessionCard({
  campaign,
  session,
  busy,
  onDelete,
}: {
  campaign: Campaign;
  session: Session;
  busy: boolean;
  onDelete: () => void;
}) {
  const tint = sessionStatusTint(session.status);
  const stage = sessionStage(session.status);
  const needsDm = sessionNeedsDm(session.status);
  const working = sessionIsWorking(session.status);
  const peaks = useMemo(() => waveformPeaks(session.id, CARD_BARS), [session.id]);

  const label = session.title ?? "Untitled session";
  const href = `/campaigns/${campaign.id}/sessions/${session.id}`;
  const isDm = campaign.my_role === "dm";

  return (
    <div className="rl-card rl-card--linkable relative flex h-full flex-col">
      {/* The card is one big link; interactive children sit above it. */}
      <Link href={href} className="absolute inset-0 z-0" aria-label={"Open " + label} />

      <div className="flex items-start justify-between gap-3">
        <RlIconChip name="mic" tint={tint} size={40} iconSize={20} />
        <span className="flex flex-wrap items-center justify-end gap-2">
          {needsDm ? <RlTag tint="accent">Needs you</RlTag> : null}
          <SessionStatusBadge status={session.status} />
        </span>
      </div>

      <span className="rl-eyebrow rl-tint mt-4" style={rlTintVars(tint)}>
        {session.session_no !== null ? "Session " + session.session_no : "Unnumbered"}
      </span>
      <h3 className="rl-card-title mt-1.5 truncate" title={label}>{label}</h3>
      <p className="rl-card-meta mt-1.5">
        {fmtRelative(session.recorded_at ?? session.created_at)} · {fmtDuration(session.duration_sec)}
      </p>

      {/* The recording's envelope — decorative, see lib/waveform.ts. */}
      {session.raw_audio_uri ? (
        <div className="rl-card-wave mt-3.5" aria-hidden="true">
          {peaks.map((height, i) => (
            <span key={i} className="rl-card-wave-bar" style={{ height: Math.round(height * 100) + "%" }} />
          ))}
        </div>
      ) : (
        <div className="rl-card-wave-empty mt-3.5" aria-hidden="true">
          no recording attached yet
        </div>
      )}

      <div className="mt-auto pt-4">
        <hr className="rl-hairline" />

        <div className="mt-3 flex items-center gap-2">
          <span className="rl-stages" aria-hidden="true">
            {SESSION_STAGES.map((name, i) => (
              <span
                key={name}
                title={name}
                style={i <= stage ? rlTintVars(tint) : undefined}
                className={
                  "rl-stage" + (i <= stage ? " rl-stage--done" : "") + (i === stage && working ? " rl-stage--active" : "")
                }
              />
            ))}
          </span>
          <span className="rl-card-meta">{SESSION_STAGES[stage]}</span>
        </div>

        <p className={"rl-body mt-2 text-xs" + (needsDm ? " rl-text-accent-strong" : "")}>
          {sessionStatusNote(session.status)}
        </p>
        {session.error ? <p className="rl-body mt-1 text-xs rl-text-villain">error: {session.error}</p> : null}

        <div className="mt-3 flex items-center justify-between gap-3">
          <span className="rl-open pointer-events-none">
            Open
            <RlIcon name="arrow" size={13} />
          </span>
          {isDm && session.can_delete ? (
            <Button
              variant="ghost"
              className="relative z-10 px-2 py-1 text-xs rl-text-villain hover:bg-[color:color-mix(in_srgb,var(--rl-cat-villain)_12%,transparent)]"
              disabled={busy}
              onClick={onDelete}
              title="Delete this session (only possible until its wiki updates are generated)"
            >
              Delete
            </Button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

/** "12h 30m" / "45m" — a shelf of recordings, not a stopwatch. */
function spellRuntime(seconds: number): string {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  if (hours > 0) return hours + "h " + minutes + "m";
  return Math.max(1, minutes) + "m";
}
