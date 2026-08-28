// Transcript viewer: fetches the session transcript JSON (via the /api/object
// proxy) and renders it speaker by speaker, with click-to-seek into the
// recording player.

"use client";

import { useEffect, useState } from "react";
import { Alert, Badge, Button } from "@/components/ui";
import { objectUrl } from "@/lib/api";
import { errMessage } from "@/lib/use-async";

interface Segment {
  start: number;
  end: number;
  text?: string;
  speaker?: string;
  words?: unknown[];
}

interface TranscriptDoc {
  session_id?: string;
  language?: string;
  model?: string;
  segments?: Segment[];
}

const SPEAKER_CHIP_STYLES = [
  "bg-blue-500/15 text-blue-300",
  "bg-emerald-500/15 text-emerald-300",
  "bg-amber-500/15 text-amber-300",
  "bg-violet-500/15 text-violet-300",
  "bg-rose-500/15 text-rose-300",
  "bg-cyan-500/15 text-cyan-300",
  "bg-orange-500/15 text-orange-300",
  "bg-pink-500/15 text-pink-300",
];

function chipStyle(index: number): string {
  return SPEAKER_CHIP_STYLES[index % SPEAKER_CHIP_STYLES.length];
}

export function fmtClock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(sec).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function TranscriptViewer({
  transcriptUrl,
  speakerNames = {},
  onSeek,
}: {
  transcriptUrl: string | null;
  /** speaker_label -> display name (from session speaker assignments). */
  speakerNames?: Record<string, string>;
  onSeek?: (seconds: number) => void;
}) {
  const [transcript, setTranscript] = useState<TranscriptDoc | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retryNonce, setRetryNonce] = useState(0);

  // The session page polls the session while the pipeline runs and passes a
  // freshly signed presigned URL every time. The transcript object is
  // OVERWRITTEN IN PLACE as the pipeline progresses (progressive chunks, and
  // speaker-service re-clustering which re-labels the segments), so we must
  // refetch whenever a fresh URL arrives — the object path alone is not a
  // content fingerprint. `retryNonce` still forces a refetch on Retry.
  useEffect(() => {
    if (!transcriptUrl) {
      setTranscript(null);
      setError(null);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);
    (async () => {
      try {
        const url = objectUrl(transcriptUrl);
        const r = await fetch(url!);
        if (!r.ok) throw new Error(`transcript fetch failed (${r.status})`);
        const t = (await r.json()) as TranscriptDoc;
        if (!cancelled) {
          setTranscript(t);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(errMessage(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [transcriptUrl, retryNonce]);

  if (!transcriptUrl) {
    return (
      <div className="rounded-lg border border-dashed border-slate-800 px-4 py-10 text-center text-sm text-slate-500">
        The transcript appears here once the transcription pipeline finishes.
      </div>
    );
  }

  if (loading && !transcript) {
    return <p className="text-sm text-slate-400">Loading transcript…</p>;
  }

  if (error) {
    return (
      <Alert tone="error">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span>Could not load the transcript: {error} (the presigned link may have expired — reload the session).</span>
          <Button variant="secondary" onClick={() => setRetryNonce((n) => n + 1)}>Retry</Button>
        </div>
      </Alert>
    );
  }

  const segments = transcript?.segments ?? [];
  const totalDuration = segments.length ? Math.max(...segments.map((s) => s.end ?? 0)) : 0;

  // Build an ordered list of speaker labels (first-appearance order) so chips
  // get stable colors.
  const speakerOrder: string[] = [];
  for (const seg of segments) {
    const label = seg.speaker;
    if (label && !speakerOrder.includes(label)) speakerOrder.push(label);
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
        {transcript?.language ? <Badge tone="slate">lang {transcript.language}</Badge> : null}
        {transcript?.model ? <Badge tone="slate">model {transcript.model}</Badge> : null}
        <span>{segments.length} segment{segments.length === 1 ? "" : "s"}</span>
        <span>· {fmtClock(totalDuration)}</span>
        <div className="ml-auto flex gap-2">
          {transcriptUrl ? (
            <a href={objectUrl(transcriptUrl)!} target="_blank" rel="noreferrer" className="text-ember-400 hover:underline">
              transcript JSON
            </a>
          ) : null}
        </div>
      </div>

      {/* Scrollable transcript: long sessions must not stretch the page forever. */}
      <div className="max-h-[60vh] overflow-y-auto pr-1">
        {segments.length === 0 ? (
          <p className="text-sm text-slate-500">The transcript has no segments.</p>
        ) : (
          <ul className="space-y-1.5">
            {segments.map((seg, i) => {
              const label = seg.speaker ?? "speaker";
              const labelIdx = speakerOrder.indexOf(label);
              const name = speakerNames[label] ?? label;
              return (
                <li key={i}>
                  <button
                    onClick={() => onSeek?.(seg.start ?? 0)}
                    title="Jump to this moment in the recording"
                    className="group flex w-full items-start gap-3 rounded-lg px-2 py-1.5 text-left transition hover:bg-slate-800/60"
                  >
                    <span className="mt-0.5 shrink-0 font-mono text-xs text-slate-500 tabular-nums">
                      {fmtClock(seg.start ?? 0)}
                    </span>
                    <span className={`mt-0.5 inline-flex shrink-0 items-center rounded-full px-2 py-0.5 text-xs font-semibold ${chipStyle(labelIdx)}`}>
                      {name}
                    </span>
                    <span className="text-sm leading-relaxed text-slate-200">
                      {seg.text?.trim() || "…"}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
