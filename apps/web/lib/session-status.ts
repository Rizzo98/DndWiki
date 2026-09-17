// How a session's pipeline status reads to a human, and where it sits in the
// pipeline. One place, so the sessions grid and the session page agree.
//
// The stages are the pipeline's spine (see docs/attribution-ux.md): the audio
// lands, gets transcribed, refined, separated by speaker, summarised, and only
// then does anything reach the wiki.

import type { SessionStatus } from "./api";

/** The six stages a recording walks through, in order. */
export const SESSION_STAGES = ["Recorded", "Transcribed", "Refined", "Speakers", "Summary", "Wiki"] as const;

const STAGE_BY_STATUS: Record<SessionStatus, number> = {
  uploaded: 0,
  recorded: 0,
  transcribing: 1,
  transcribed: 1,
  refining: 2,
  refined: 2,
  identifying_speakers: 3,
  speakers_identified: 3,
  speaker_pending: 3,
  attributing: 3,
  attribution_ready: 3,
  attribution_review: 3,
  summarizing: 4,
  summary_ready: 4,
  generating_wiki: 5,
  wiki_plan_ready: 5,
  applying_wiki: 5,
  content_ready: 5,
  reviewed: 5,
  published: 5,
  failed: 0,
};

/** Index into SESSION_STAGES (0-based) for a status. */
export function sessionStage(status: SessionStatus): number {
  return STAGE_BY_STATUS[status] ?? 0;
}

const NOTE_BY_STATUS: Record<SessionStatus, string> = {
  uploaded: "Waiting for a recording",
  recorded: "Recording in — waiting to be transcribed",
  transcribing: "Transcribing the audio…",
  transcribed: "Transcribed — refining the text next",
  refining: "Cleaning up the transcript…",
  refined: "Transcript ready",
  identifying_speakers: "Working out who spoke…",
  speakers_identified: "Voices separated",
  speaker_pending: "Speakers need names",
  attributing: "Matching voices to the table…",
  attribution_ready: "Voice matches ready",
  attribution_review: "Waiting for you — confirm who said what",
  summarizing: "Writing the session summary…",
  summary_ready: "Waiting for you — review the summary",
  generating_wiki: "Deciding what the wiki should say…",
  wiki_plan_ready: "Waiting for you — review the proposed wiki changes",
  applying_wiki: "Writing the wiki pages…",
  content_ready: "Wiki pages written",
  reviewed: "Reviewed and accepted",
  published: "Published",
  failed: "The pipeline stopped — open it to see why",
};

/** One line of what is happening (or what is expected of the DM) right now. */
export function sessionStatusNote(status: SessionStatus): string {
  return NOTE_BY_STATUS[status] ?? status.replace(/_/g, " ");
}

/** True while the session is resting on a decision only the DM can make. */
export function sessionNeedsDm(status: SessionStatus): boolean {
  return (
    status === "speaker_pending" ||
    status === "attribution_review" ||
    status === "summary_ready" ||
    status === "wiki_plan_ready"
  );
}

/** True while the pipeline itself is still working. */
export function sessionIsWorking(status: SessionStatus): boolean {
  return (
    status === "transcribing" ||
    status === "refining" ||
    status === "identifying_speakers" ||
    status === "attributing" ||
    status === "summarizing" ||
    status === "generating_wiki" ||
    status === "applying_wiki"
  );
}
