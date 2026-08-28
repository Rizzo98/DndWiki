// Recording player widget: plays the uploaded session audio (proxied through
// /api/object so the docker-internal MinIO host is reachable from the browser).

"use client";

import type { Ref } from "react";

export function AudioPlayer({
  src,
  audioRef,
  title = "Session recording",
}: {
  src: string | null;
  audioRef?: Ref<HTMLAudioElement>;
  title?: string;
}) {
  if (!src) {
    return (
      <div className="flex min-h-24 items-center justify-center rounded-lg border border-dashed border-slate-800 px-4 py-6 text-center text-sm text-slate-500">
        No recording uploaded yet — upload one above to start the pipeline.
      </div>
    );
  }
  return (
    <div className="space-y-3">
      <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</p>
      {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
      <audio ref={audioRef} controls preload="metadata" src={src} className="w-full" />
      <div className="flex items-center justify-between text-xs text-slate-500">
        <span>Click a transcript segment below to jump to that moment.</span>
        <a href={src} target="_blank" rel="noreferrer" className="text-ember-400 hover:underline">
          Download
        </a>
      </div>
    </div>
  );
}
