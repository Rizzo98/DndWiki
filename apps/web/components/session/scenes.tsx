"use client";

import { useCallback, useEffect, useState } from "react";

import { Card, fmtDuration } from "@/components/ui";
import { sessionsApi, type SessionScene } from "@/lib/api";
import { errMessage } from "@/lib/use-async";

/**
 * Where the session happens, and who is there.
 *
 * This panel exists because the engine now EXCLUDES members from moments: when
 * the record says somebody walked off ("Letho si allontana dal gruppo"), that
 * member cannot be the speaker until they come back. An exclusion nobody can see
 * is an exclusion nobody can argue with, and the DM is the only one who knows
 * whether the reading of their own table is right.
 *
 * So it shows the reading as a reading - the place, the moments, the characters,
 * the event that started the stretch - and never as a verdict. It is a
 * disclosure, like the other secondary panels, because the review card is what
 * the DM is meant to be working in.
 */
export function SessionScenesCard({
  token,
  sessionId,
  sessionStatus,
  onSeek,
}: {
  token: string | null;
  sessionId: string;
  /** The panel appears when the engine has run, and follows the pipeline. */
  sessionStatus?: string;
  onSeek?: (seconds: number) => void;
}) {
  const [scenes, setScenes] = useState<SessionScene[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Open by default. It is the only place the engine's exclusions are visible,
  // and a panel nobody opens is a decision nobody can check.
  const [open, setOpen] = useState(true);

  const load = useCallback(async () => {
    if (!token) return;
    try {
      const payload = await sessionsApi.scenes(token, sessionId);
      setScenes(payload.scenes ?? []);
      setError(null);
    } catch (err) {
      // No attribution yet (or the engine is off) is the normal state of a new
      // session, not something to shout about: the panel stays hidden.
      setScenes(null);
      setError(errMessage(err));
    }
  }, [token, sessionId]);

  useEffect(() => {
    void load();
  }, [load, sessionStatus]);

  if (!scenes || scenes.length === 0) return null;

  const exclusions = scenes.filter((scene) => scene.absent.length > 0).length;
  const note =
    exclusions > 0
      ? " \u00b7 in " +
        exclusions +
        " of them somebody is somewhere else, and is not considered a speaker there"
      : "";

  return (
    <Card className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="rl-title text-lg text-[color:var(--rl-text-on-parchment-primary)]">Where this session happens</h2>
          <p className="text-sm text-[color:var(--rl-text-on-parchment-muted)]">
            {scenes.length} stretch{scenes.length === 1 ? "" : "es"} of the session, as read from
            the transcript{note}.
          </p>
        </div>
        <button
          type="button"
          className="text-sm rl-text-accent hover:underline"
          onClick={() => setOpen((value) => !value)}
        >
          {open ? "Hide" : "Show"}
        </button>
      </div>

      {error ? <p className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">{error}</p> : null}

      {open ? (
        <ol className="space-y-3">
          {scenes.map((scene) => (
            <li
              key={scene.index}
              className="rounded-lg border border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-bg-card)] p-3"
            >
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="font-medium text-[color:var(--rl-text-on-parchment-primary)]">
                  {scene.index}. {scene.location}
                </span>
                <span className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">
                  {scene.moments} moments
                  {scene.start_sec != null ? " \u00b7 from " + fmtDuration(scene.start_sec) : ""}
                  {scene.end_sec != null ? " to " + fmtDuration(scene.end_sec) : ""}
                  {scene.start_sec != null && onSeek ? (
                    <>
                      {" \u00b7 "}
                      <button
                        type="button"
                        className="rl-text-accent hover:underline"
                        onClick={() => onSeek(scene.start_sec as number)}
                      >
                        listen
                      </button>
                    </>
                  ) : null}
                </span>
              </div>
              {scene.reason ? (
                <p className="mt-1 text-xs text-[color:var(--rl-text-on-parchment-muted)]">starts: {scene.reason}</p>
              ) : null}
              <p className="mt-2 text-sm text-[color:var(--rl-text-on-parchment-primary)]">
                <span className="text-[color:var(--rl-text-on-parchment-muted)]">there: </span>
                {scene.present.length > 0 ? scene.present.join(", ") : "not stated"}
              </p>
              {scene.absent.length > 0 ? (
                <p className="text-sm rl-text-item">
                  <span className="text-[color:var(--rl-text-on-parchment-muted)]">somewhere else: </span>
                  {scene.absent.join(", ")}
                </p>
              ) : null}
              {scene.npcs.length > 0 ? (
                <p className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">NPCs in play: {scene.npcs.join(", ")}</p>
              ) : null}
            </li>
          ))}
        </ol>
      ) : null}
    </Card>
  );
}
