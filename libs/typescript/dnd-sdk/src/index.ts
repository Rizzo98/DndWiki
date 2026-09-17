/**
 * dnd-sdk — shared types + a thin fetch client for the DnD Wiki frontends.
 *
 * Consumed by apps/web and apps/mobile. Property names match the wire format
 * returned by the FastAPI services (snake_case), mirroring docs/data-model.md
 * and the Pydantic response schemas.
 */

export type Role = "dm" | "player";

export interface User {
  id: string;
  email: string | null;
  display_name: string;
  avatar_uri: string | null;
  avatar_url: string | null;
  created_at: string;
}

export interface Campaign {
  id: string;
  name: string;
  slug: string;
  description: string | null;
  /** Session language chosen by the DM at creation (e.g. "en", "it"). */
  language: string;
  dm_user_id: string;
  status: "active" | "archived";
  settings: Record<string, unknown>;
  created_at: string;
  /** Filled by the API with the calling user's role in this campaign. */
  my_role: Role | null;
  /**
   * Cover art, as a freshly presigned URL (null when the campaign has none).
   * Short-lived: never cached, never persisted — refetch the campaign when it
   * matters. Set by the DM through PUT /api/campaigns/{id}/cover.
   */
  cover_url?: string | null;
}

export interface CampaignMember {
  /** Surrogate member id (stable regardless of the user link). */
  id: string;
  campaign_id: string;
  /** Linked platform user (Keycloak subject) or null for unlinked players. */
  user_id: string | null;
  role: Role;
  /** DM-curated player name. */
  player_name: string;
  /** DM-curated character name. */
  character_name: string;
  /** DM-curated physical description of the character (refiner context). */
  character_description: string | null;
  joined_at: string;
}

export type SessionStatus =
  | "uploaded"
  | "recorded"
  | "transcribing"
  | "transcribed"
  | "refining"
  | "refined"
  | "identifying_speakers"
  | "speakers_identified"
  /** @deprecated retired as a BLOCKING state by the attribution redesign and
   * accepted for one release as an alias of "attribution_review". */
  | "speaker_pending"
  /** The attribution engine is computing a revision. */
  | "attributing"
  /** The belief is written down; the review is about to be offered. */
  | "attribution_ready"
  /** RESTING: the DM answers the engine's questions. Skippable, NEVER blocking. */
  | "attribution_review"
  /** The session summary is being (re)built from the transcript. */
  | "summarizing"
  /** Draft summary waiting for the DM's review: the wiki is not generated yet. */
  | "summary_ready"
  /** The proposed wiki changes are being computed from the confirmed summary. */
  | "generating_wiki"
  /** Proposed changes waiting for the DM's review: nothing is in the wiki yet. */
  | "wiki_plan_ready"
  /** The DM confirmed the changes; the wiki is being written. */
  | "applying_wiki"
  | "content_ready"
  | "reviewed"
  | "published"
  | "failed";

export interface Session {
  id: string;
  campaign_id: string;
  title: string | null;
  session_no: number | null;
  recorded_at: string | null;
  status: SessionStatus;
  raw_audio_uri: string | null;
  transcript_uri: string | null;
  diarization_uri: string | null;
  duration_sec: number | null;
  error: string | null;
  /** True until the session's wiki updates are generated: only then may the
   * DM delete it (its pages would otherwise outlive the session). */
  can_delete: boolean;
  created_at: string;
  updated_at: string;
}

/** The six wiki categories. 'event' pages back the campaign timeline:
 * every timeline event links to an event page. */
export type WikiPageKind =
  | "character"
  | "location"
  | "faction"
  | "item"
  | "quest"
  | "event";

export type WikiPageStatus = "draft" | "pending_review" | "published" | "archived";
export type WikiVisibility = "public" | "dm_only" | "hidden";

export interface WikiPage {
  id: string;
  campaign_id: string;
  kind: WikiPageKind;
  title: string;
  slug: string;
  content_json: Record<string, unknown>;
  status: WikiPageStatus;
  visibility: WikiVisibility;
  confidence: number | null;
  source_session_id: string | null;
  created_by: string | null;
  updated_by: string | null;
  created_at: string;
  updated_at: string;
}

// ---------------------------------------------------------------------------
// Speaker attribution (docs/attribution-model.md)
// ---------------------------------------------------------------------------

/**
 * How sure the engine is about one utterance, and therefore what the wiki may
 * do with it. The order is the monotonicity rule: a consumer may never promote
 * an utterance to a status it was not assigned.
 *
 * - `user_confirmed`  the DM answered a question covering this utterance
 * - `auto_high`       high posterior AND a wide margin AND a second channel
 * - `propagated`      inferred from somebody else's answer (stricter bar)
 * - `auto_low`        plausible, but not enough to write a character fact
 * - `unresolved`      the engine does not know; describe at party level
 */
export type AttributionStatus =
  | "user_confirmed"
  | "auto_high"
  | "propagated"
  | "auto_low"
  | "unresolved";

/** The statuses that may back a character-level fact on a page. */
export const CONFIDENT_ATTRIBUTION_STATUSES: readonly AttributionStatus[] = [
  "user_confirmed",
  "auto_high",
  "propagated",
];

export interface AttributionSpeaker {
  member_id: string | null;
  character_name: string | null;
  player_name: string | null;
  role: Role;
  mode: string | null;
  label: string | null;
}

export interface AttributedUtterance {
  id: string;
  start: number;
  end: number;
  text: string;
  speaker: AttributionSpeaker | null;
  status: AttributionStatus;
  confidence: number | null;
  kind: string | null;
  stakes: number;
  decided_by: string | null;
}

export interface AttributedTranscript {
  session_id: string;
  campaign_id: string;
  language: string;
  attribution_revision: number;
  coverage: number;
  unresolved_stakes: number;
  roster: Array<{
    member_id: string;
    player_name: string | null;
    character_name: string | null;
    role: Role;
    label: string;
  }>;
  utterances: AttributedUtterance[];
}

/** One thing the DM can pick. *I don't know* is always the last option. */
export interface ReviewOption {
  key: string;
  label: string;
  why?: string;
}

/**
 * What a question is about.
 *
 * TWO kinds, and both are about one MOMENT of the session: an action somebody
 * took, or a line somebody said. Two families are RETIRED and no longer
 * generated, each because it was measured rather than argued:
 *
 * - the voice-identity kinds (same_voice, different_voice, who_is_voice,
 *   new_person): the diarization clusters they asked about turned out to be
 *   mixtures of several people, so "who is this voice?" had no true answer;
 * - "presence" ("was <name> there?" over a stretch): a scene holds almost
 *   everybody almost always, so the answer was a foregone "yes" - while, being
 *   priced at half a click, it crowded every question that could have settled a
 *   moment out of the ranking.
 *
 * Sessions computed before either rollback still hold those rows, and the
 * service drops them on load rather than asking them again.
 */
export type ReviewQuestionKind = "who_did" | "who_said";

export interface ReviewQuestion {
  id: string | null;
  kind: ReviewQuestionKind;
  prompt_text: string;
  options: ReviewOption[];
  target_utterances: string[];
  target_voices: string[];
  hook: {
    quote?: string;
    /** The moment's own span: every question plays the thing it asks about. */
    audio?: { start: number; end: number };
    ref?: string;
    note?: string;
  };
  cost: number;
  mean_stakes: number;
  expected_gain?: number;
  score?: number;
}

export interface ReviewStop {
  stop: boolean;
  reason: string;
  detail: Record<string, unknown>;
}

export interface ReviewStatus {
  coverage: number;
  unresolved: number;
  buckets: Partial<Record<AttributionStatus, number>>;
  questions_asked: number;
  questions_planned: number;
  finished: boolean;
  /** The DM's own progress: TWO FACTS - how many questions they have answered
   *  and how many one review asks. No "N to finish": the length of a review
   *  depends on the answers, so no honest estimate exists in advance (S9.4). */
  plan: {
    answered: number;
    max_questions: number;
    budget_spent: boolean;
  };
  run: {
    status: string;
    questions_planned: number | null;
    questions_asked: number;
    coverage_before: number | null;
    coverage_after: number | null;
    stop_reason: string | null;
    engine_version: string | null;
  };
}

export interface ReviewAnswerResult {
  resolved_utterances: number;
  resolved_sec: number;
  coverage: number;
  learned: { capabilities: number; voice_centroids: number };
  next_question: { prompt_text: string; kind: ReviewQuestionKind } | null;
  stop: ReviewStop;
}

/** A voice identity: anonymous, session-scoped, and NEVER shown as a person.
 *
 * The guess fields are what the ENGINE makes of this voice right now - a guess
 * with a number, which is exactly the kind of claim the DM is allowed to
 * disagree with. They are absent when the engine has not run, which is why they
 * are optional rather than zero-filled.
 */
export interface VoiceIdentitySummary {
  id: string;
  handle: string;
  speech_sec: number;
  purity: number | null;
  observations: number;
  status: string;
  /** Candidate key the engine leans towards ('member:<id>' or 'unknown'). */
  guess?: string;
  /** That candidate as a name the DM recognises. */
  guess_label?: string;
  /** Pooled posterior of the voice's own utterances, 0..1. */
  guess_confidence?: number;
  /** Set when the DM placed this voice by hand; null when they have not. */
  confirmed?: string | null;
}

/** Typed API error. */
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public detail?: unknown,
  ) {
    super(message);
  }
}

/** Minimal authenticated fetch wrapper against an API base URL. */
export async function apiFetch<T>(
  baseUrl: string,
  path: string,
  accessToken: string,
  init: RequestInit = {},
): Promise<T> {
  const res = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
      ...init.headers,
    },
  });
  if (!res.ok) {
    throw new ApiError(res.status, res.statusText, await res.json().catch(() => undefined));
  }
  return res.json() as Promise<T>;
}
