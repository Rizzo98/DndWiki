// The recording panel: the product's signature surface.
//
// It wraps a plain <audio> element (the page keeps the ref so transcript and
// summary timestamps can seek into it) and paints a waveform scrubber over it,
// because "where in the three hours was that?" is the question this page exists
// to answer.
//
// The waveform is a DECORATIVE envelope derived from a stable seed — the app
// does not compute real audio peaks (that needs an ffmpeg/audiowaveform pass in
// the pipeline). What is real: the progress, the seek target, the times and the
// playback state all come from the audio element.

"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type Ref } from "react";
import { RlIcon, RlIconChip } from "@/components/ravenlore";
import { fmtClock } from "@/components/session/transcript";
import { waveformPeaks } from "@/lib/waveform";

/** Bars in the waveform. Enough to read as a shape, few enough to be cheap. */
const BARS = 132;
/** How far the skip buttons jump. */
const SKIP_SEC = 10;

export function AudioPlayer({
  src,
  audioRef,
  seed,
  meta = "Session audio",
  durationSec,
}: {
  src: string | null;
  audioRef?: Ref<HTMLAudioElement>;
  /** Stable per-session key: the envelope must not change when URLs rotate. */
  seed: string;
  meta?: string;
  /** Fallback duration until the audio element reports its own. */
  durationSec?: number | null;
}) {
  const localRef = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(false);
  const [current, setCurrent] = useState(0);
  const [duration, setDuration] = useState(0);
  const waveRef = useRef<HTMLDivElement>(null);
  const scrubbing = useRef(false);

  // One ref for the page and for this component: the transcript seeks through
  // the page's handle, the scrubber through ours.
  const attach = useCallback(
    (node: HTMLAudioElement | null) => {
      localRef.current = node;
      if (typeof audioRef === "function") audioRef(node);
      else if (audioRef) (audioRef as { current: HTMLAudioElement | null }).current = node;
    },
    [audioRef],
  );

  useEffect(() => {
    const audio = localRef.current;
    if (!audio) return;
    const syncTime = () => setCurrent(audio.currentTime);
    const syncDuration = () => {
      if (Number.isFinite(audio.duration) && audio.duration > 0) setDuration(audio.duration);
    };
    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    audio.addEventListener("timeupdate", syncTime);
    audio.addEventListener("loadedmetadata", syncDuration);
    audio.addEventListener("durationchange", syncDuration);
    audio.addEventListener("play", onPlay);
    audio.addEventListener("pause", onPause);
    audio.addEventListener("ended", onPause);
    syncDuration();
    return () => {
      audio.removeEventListener("timeupdate", syncTime);
      audio.removeEventListener("loadedmetadata", syncDuration);
      audio.removeEventListener("durationchange", syncDuration);
      audio.removeEventListener("play", onPlay);
      audio.removeEventListener("pause", onPause);
      audio.removeEventListener("ended", onPause);
    };
  }, [src]);

  const total = duration > 0 ? duration : durationSec ?? 0;
  const progress = total > 0 ? Math.min(1, current / total) : 0;

  const bars = useMemo(() => waveformPeaks(seed, BARS), [seed]);

  const seekToFraction = useCallback(
    (fraction: number) => {
      const audio = localRef.current;
      if (!audio || total <= 0) return;
      const t = Math.max(0, Math.min(total, fraction * total));
      audio.currentTime = t;
      setCurrent(t);
    },
    [total],
  );

  const fractionFromEvent = (clientX: number) => {
    const box = waveRef.current?.getBoundingClientRect();
    if (!box || box.width === 0) return 0;
    return (clientX - box.left) / box.width;
  };

  function onWaveDown(e: ReactPointerEvent<HTMLDivElement>) {
    if (total <= 0) return;
    scrubbing.current = true;
    e.currentTarget.setPointerCapture(e.pointerId);
    seekToFraction(fractionFromEvent(e.clientX));
  }

  function onWaveMove(e: ReactPointerEvent<HTMLDivElement>) {
    if (!scrubbing.current) return;
    seekToFraction(fractionFromEvent(e.clientX));
  }

  function onWaveUp(e: ReactPointerEvent<HTMLDivElement>) {
    if (!scrubbing.current) return;
    scrubbing.current = false;
    e.currentTarget.releasePointerCapture(e.pointerId);
  }

  const toggle = () => {
    const audio = localRef.current;
    if (!audio) return;
    if (audio.paused) void audio.play().catch(() => undefined);
    else audio.pause();
  };

  const skip = (delta: number) => {
    const audio = localRef.current;
    if (!audio) return;
    audio.currentTime = Math.max(0, Math.min(total || audio.duration || 0, audio.currentTime + delta));
  };

  const toggleMute = () => {
    const audio = localRef.current;
    if (!audio) return;
    audio.muted = !audio.muted;
    setMuted(audio.muted);
  };

  if (!src) {
    return (
      <div className="rl-dark-panel flex min-h-[112px] items-center justify-center text-center">
        <p className="rl-dark-note">Preparing the recording…</p>
      </div>
    );
  }

  const playedBars = Math.round(progress * bars.length);

  return (
    <section className="rl-audio-panel">
      <audio ref={attach} src={src} preload="metadata" className="hidden" />

      <div className="rl-audio-header">
        <RlIconChip name="mic" tint="accent" size={34} iconSize={17} />
        <div className="min-w-0 flex-1">
          <p className="rl-audio-title truncate">Session recording</p>
          <p className="rl-audio-sub">
            {meta}
            {total > 0 ? " · " + fmtClock(total) : ""}
          </p>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={toggleMute}
            className="rl-audio-icon"
            aria-label={muted ? "Unmute" : "Mute"}
            aria-pressed={muted}
          >
            <RlIcon name={muted ? "mute" : "volume"} size={17} />
          </button>
          <a
            href={src}
            target="_blank"
            rel="noreferrer"
            className="rl-audio-icon"
            aria-label="Download the recording"
            title="Download the recording"
          >
            <RlIcon name="download" size={17} />
          </a>
        </div>
      </div>

      <div className="rl-audio-body">
        <button type="button" onClick={() => skip(-SKIP_SEC)} className="rl-audio-skip hidden sm:grid" aria-label={`Back ${SKIP_SEC} seconds`}>
          <RlIcon name="back10" size={20} />
        </button>
        <button type="button" onClick={toggle} className="rl-audio-play" aria-label={playing ? "Pause" : "Play"}>
          <RlIcon name={playing ? "pause" : "play"} size={20} />
        </button>
        <button type="button" onClick={() => skip(SKIP_SEC)} className="rl-audio-skip hidden sm:grid" aria-label={`Forward ${SKIP_SEC} seconds`}>
          <RlIcon name="forward10" size={20} />
        </button>

        <div className="min-w-0 flex-1">
          <div
            ref={waveRef}
            className="rl-wave"
            role="slider"
            tabIndex={0}
            aria-label="Seek in the recording"
            aria-valuemin={0}
            aria-valuemax={Math.round(total)}
            aria-valuenow={Math.round(current)}
            aria-valuetext={fmtClock(current)}
            onPointerDown={onWaveDown}
            onPointerMove={onWaveMove}
            onPointerUp={onWaveUp}
            onPointerCancel={onWaveUp}
            onKeyDown={(e) => {
              if (e.key === "ArrowRight") skip(SKIP_SEC);
              else if (e.key === "ArrowLeft") skip(-SKIP_SEC);
              else if (e.key === " " || e.key === "Enter") {
                e.preventDefault();
                toggle();
              }
            }}
          >
            {bars.map((height, i) => (
              <span
                key={i}
                className={"rl-wave-bar" + (i < playedBars ? " rl-wave-bar--played" : "")}
                style={{ height: Math.round(height * 100) + "%" }}
              />
            ))}
          </div>
          <div className="mt-1.5 flex items-center justify-between">
            <span className="rl-audio-time">{fmtClock(current)}</span>
            <span className="rl-audio-time">{total > 0 ? fmtClock(total) : "--:--"}</span>
          </div>
        </div>
      </div>
    </section>
  );
}
