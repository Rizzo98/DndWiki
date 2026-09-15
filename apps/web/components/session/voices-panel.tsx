// "Voices we found": the DEMOTED speaker panel (docs/attribution-ux.md S6).
//
// The old panel was the session page's main UX: a list of SPEAKER_XX rows the DM
// had to resolve by hand before anything else could happen. It is now a
// collapsed section, and it speaks in voices rather than in labels:
//
//   - a voice is NEVER presented as a person. "Voice 3 · 12 min · we think:
//     Bob — Thorin" is honest; "SPEAKER_03 = Bob" is the invention the redesign
//     removes (docs/attribution-model.md S13).
//   - the DM can SPLIT a voice that is two people and MERGE two voices that are
//     one, because those are the two things the engine cannot always tell.
//   - naming a voice directly is still possible (the escape hatch), but it is no
//     longer the required path.

"use client";

import { useCallback, useEffect, useState } from "react";
import { Alert, Badge, Button, EmptyState, Select, fmtPercent } from "@/components/ui";
import { ApiError, sessionsApi, type CampaignMember, type VoiceIdentitySummary } from "@/lib/api";
import { errMessage } from "@/lib/use-async";

function clock(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const m = Math.floor(total / 60);
  return m >= 1 ? `${m} min` : `${total}s`;
}

export function VoicesPanel({
  token,
  sessionId,
  members,
  sessionStatus,
}: {
  token: string | null;
  sessionId: string;
  members: CampaignMember[];
  /**
   * The pipeline status. Voices only exist once the session has been diarized
   * and attributed, and the panel is mounted (inside a collapsed <details>)
   * from the moment the page loads - so it re-asks when the status moves
   * instead of sitting on an empty list until the DM refreshes.
   */
  sessionStatus?: string;
}) {
  const [voices, setVoices] = useState<VoiceIdentitySummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    try {
      const result = await sessionsApi.voices(token, sessionId);
      setVoices(result.voices);
      setError(null);
    } catch (err) {
      // 404 = this session has no attribution yet (engine off, or the session
      // has not been diarized): an empty panel, not a failure.
      if (err instanceof ApiError && err.status === 404) {
        setVoices([]);
        setError(null);
        return;
      }
      setError(errMessage(err));
    } finally {
      setLoading(false);
    }
    // sessionStatus is a dependency on purpose - see the prop's comment.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, sessionId, sessionStatus]);

  useEffect(() => {
    void load();
  }, [load]);

  const split = useCallback(
    async (voiceId: string) => {
      if (!token) return;
      setBusy(true);
      try {
        await sessionsApi.splitVoice(token, sessionId, voiceId);
        await load();
      } catch (err) {
        setError(errMessage(err));
      } finally {
        setBusy(false);
      }
    },
    [token, sessionId, load],
  );

  if (loading) return <p className="text-sm text-slate-500">Looking at the voices…</p>;
  if (error) return <Alert>{error}</Alert>;
  if (voices.length === 0) {
    return (
      <EmptyState>
        No voices to show yet. They appear once the session has been diarized.
      </EmptyState>
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        These are anonymous voice groups, not people. The engine decides who they are; you can
        correct it here.
      </p>
      <ul className="divide-y divide-slate-800">
        {voices.map((voice) => (
          <li key={voice.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
            <div className="flex flex-wrap items-center gap-3">
              <Badge tone="violet">{voice.handle}</Badge>
              <span className="text-sm text-slate-300">{clock(voice.speech_sec)} of speech</span>
              {/*
                A guess with no confident speech behind it is NOT shown. The
                engine's posterior can read 0.99 for a candidate while every one
                of that voice's moments is only auto_low - so "we think Bob, 99 %
                sure" would describe a conclusion the wiki will never use.
              */}
              {voice.guess_label && (voice.guess_confidence ?? 0) > 0 ? (
                <span className="text-sm text-slate-400">
                  we think <span className="text-slate-200">{voice.guess_label}</span>
                  <span className="ml-1 text-xs text-slate-500">
                    ({fmtPercent(voice.guess_confidence ?? 0)} of its speech, certain)
                  </span>
                  {voice.confirmed ? <Badge tone="green">you said so</Badge> : null}
                </span>
              ) : (
                <span className="text-sm text-slate-500">
                  we can&rsquo;t place this one yet
                </span>
              )}
              {voice.purity != null && voice.purity < 0.8 ? (
                <Badge tone="amber">may be more than one person</Badge>
              ) : null}
            </div>
            <Button variant="ghost" disabled={busy} onClick={() => void split(voice.id)}>
              Two people
            </Button>
          </li>
        ))}
      </ul>
      <p className="text-xs text-slate-500">
        &ldquo;We think&rdquo; is the engine&rsquo;s current guess, not a decision: whatever it is not
        sure about is described at party level and never attributed to a character. Answer the
        questions at the top of the page and it settles these on its own.
      </p>
    </div>
  );
}
