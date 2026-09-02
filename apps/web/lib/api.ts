// Typed client for every DnD Wiki backend endpoint, proxied through the
// Next.js BFF route handler (app/api/backend/[...path]) so the browser never
// talks to the services directly (no CORS middleware server-side).
//
// Types mirror the FastAPI schemas; core domain types come from @dnd-wiki/sdk.

import {
  ApiError,
  Campaign,
  CampaignMember,
  Role,
  Session,
  SessionStatus,
  User,
  WikiPage,
  WikiPageKind,
  WikiPageStatus,
  WikiVisibility,
} from "@dnd-wiki/sdk";

export { ApiError } from "@dnd-wiki/sdk";
export type {
  Campaign,
  CampaignMember,
  Role,
  Session,
  SessionStatus,
  User,
  WikiPage,
  WikiPageKind,
  WikiPageStatus,
  WikiVisibility,
} from "@dnd-wiki/sdk";

// ---------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------

async function request<T>(token: string, path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
  const isForm = init.body instanceof FormData;
  if (!isForm && init.body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(`/api/backend${path}`, { ...init, headers });
  } catch (err) {
    throw new ApiError(0, "Network error", String(err));
  }
  if (!res.ok) {
    let detail: unknown;
    try {
      detail = await res.json();
    } catch {
      /* no body */
    }
    throw new ApiError(res.status, res.statusText, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function jsonInit(method: string, body?: unknown): RequestInit {
  return { method, body: body === undefined ? undefined : JSON.stringify(body) };
}

/**
 * Rewrite a presigned MinIO URL (host http://minio:9000/...) to the local
 * object proxy so the browser can play/download it. The proxy forwards the
 * request server-side (the web container is on the same Docker network) and
 * supports Range requests for audio seeking.
 */
export function objectUrl(presigned: string | null | undefined): string | null {
  if (!presigned) return null;
  return `/api/object?url=${encodeURIComponent(presigned)}`;
}

// ---------------------------------------------------------------------------
// Additional API shapes (not exported by the SDK)
// ---------------------------------------------------------------------------

export interface Invite {
  id: string;
  campaign_id: string;
  email: string | null;
  role: Role;
  token: string;
  expires_at: string | null;
  used_at: string | null;
  created_at: string;
}

export interface VoiceProfile {
  id: string;
  user_id: string;
  campaign_id: string;
  sample_uri: string;
  sample_url: string | null;
  embedding_version: number;
  created_at: string;
}

export interface SessionDetail extends Session {
  raw_audio_url: string | null;
  transcript_url: string | null;
  diarization_url: string | null;
}

export interface SpeakerAssignment {
  id: string;
  session_id: string;
  speaker_label: string;
  /** The campaign member this voice belongs to (may lack a user account). */
  member_id: string | null;
  user_id: string | null;
  confidence: number | null;
  status: "pending" | "auto" | "confirmed";
  assigned_by: string | null;
  created_at: string;
  updated_at: string;
}

export type PageSummary = Omit<WikiPage, "content_json" | "created_by" | "updated_by">;
export type PageOut = WikiPage;

/** Page detail response: WikiPage plus a freshly presigned portrait URL. */
export type PageDetail = WikiPage & { image_url: string | null };

export interface PageVersion {
  id: string;
  page_id: string;
  version_no: number;
  content_json: Record<string, unknown>;
  change_note: string | null;
  created_by: string | null;
  created_at: string;
}

export interface PageRelation {
  id: string;
  page_id: string;
  related_page_id: string;
  relation_type: string;
  created_at: string;
  related_title: string | null;
  related_slug: string | null;
}

export interface TimelineEvent {
  id: string;
  campaign_id: string;
  page_id: string | null;
  /** Title/slug of the linked event page (filled by the API). */
  page_title: string | null;
  page_slug: string | null;
  in_world_date: string | null;
  summary: string;
  approved: boolean;
  source_session_id: string | null;
  created_at: string;
}

export interface GenerationJob {
  id: string;
  status: "queued" | "running" | "done" | "failed";
  llm_provider: string;
  llm_model: string;
  prompt_version: string;
  draft_ids: string[];
  confidence: number | null;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ContentJobResponse {
  session_id: string;
  job: GenerationJob | null;
}

// Mirrors the merged extraction persisted by content-service (app/merger.py).
export interface SummaryEntity {
  name: string;
  aliases: string[];
  description: string;
  facts: string[];
  session_facts?: string[];
  mentions: number;
  confidence: number | null;
}

export interface SummaryEvent {
  title: string;
  description: string;
  participants: string[];
  confidence: number | null;
}

export interface SummaryTimelineEntry {
  time: string;
  summary: string;
  characters: string[];
}

export interface SessionSummary {
  id: string;
  session_id: string;
  generation_job_id: string | null;
  summary: string;
  characters: SummaryEntity[];
  locations: SummaryEntity[];
  events: SummaryEvent[];
  timeline_entries: SummaryTimelineEntry[];
  confidence: number | null;
  llm_provider: string | null;
  llm_model: string | null;
  prompt_version: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ContentSummaryResponse {
  session_id: string;
  summary: SessionSummary | null;
}

// ---------------------------------------------------------------------------
// campaign-service
// ---------------------------------------------------------------------------

export const campaignsApi = {
  list: (token: string) => request<Campaign[]>(token, "/api/campaigns"),
  create: (
    token: string,
    body: {
      name: string;
      slug?: string;
      description?: string;
      /** Session language - required; the DM picks it at creation. */
      language: string;
      settings?: Record<string, unknown>;
      /** Players added at creation (required, at least one). */
      members: { player_name: string; character_name: string; character_description: string; user_id?: string | null }[];
    },
  ) => request<Campaign>(token, "/api/campaigns", jsonInit("POST", body)),
  get: (token: string, id: string) => request<Campaign>(token, `/api/campaigns/${id}`),
  update: (token: string, id: string, body: { name?: string; slug?: string; description?: string; language?: string; settings?: Record<string, unknown> }) =>
    request<Campaign>(token, `/api/campaigns/${id}`, jsonInit("PATCH", body)),
  archive: (token: string, id: string) => request<Campaign>(token, `/api/campaigns/${id}/archive`, jsonInit("POST")),
  restore: (token: string, id: string) => request<Campaign>(token, `/api/campaigns/${id}/restore`, jsonInit("POST")),

  members: (token: string, campaignId: string) => request<CampaignMember[]>(token, `/api/campaigns/${campaignId}/members`),
  addMember: (token: string, campaignId: string, body: { player_name: string; character_name: string; character_description: string; user_id?: string | null }) =>
    request<CampaignMember>(token, `/api/campaigns/${campaignId}/members`, jsonInit("POST", body)),
  updateMember: (token: string, campaignId: string, memberId: string, body: { player_name?: string; character_name?: string; character_description?: string; user_id?: string | null; unlink_user?: boolean }) =>
    request<CampaignMember>(token, `/api/campaigns/${campaignId}/members/${memberId}`, jsonInit("PATCH", body)),
  removeMember: (token: string, campaignId: string, memberId: string) =>
    request<void>(token, `/api/campaigns/${campaignId}/members/${memberId}`, { method: "DELETE" }),

  invites: (token: string, campaignId: string) => request<Invite[]>(token, `/api/campaigns/${campaignId}/invites`),
  createInvite: (token: string, campaignId: string, body: { email?: string | null; role?: Role }) =>
    request<Invite>(token, `/api/campaigns/${campaignId}/invites`, jsonInit("POST", body)),
  revokeInvite: (token: string, campaignId: string, inviteId: string) =>
    request<void>(token, `/api/campaigns/${campaignId}/invites/${inviteId}`, { method: "DELETE" }),
  acceptInvite: (token: string, inviteToken: string) =>
    request<Campaign>(token, `/api/campaigns/invites/${inviteToken}/accept`, jsonInit("POST")),
};

// ---------------------------------------------------------------------------
// campaign session languages
// ---------------------------------------------------------------------------

/** Session languages the DM may pick at creation (mirrors campaign-service
 * SUPPORTED_LANGUAGES / LANGUAGE_PATTERN). */
export const CAMPAIGN_LANGUAGES: { code: string; label: string }[] = [
  { code: "en", label: "English" },
  { code: "it", label: "Italiano" },
  { code: "de", label: "Deutsch" },
  { code: "fr", label: "Français" },
  { code: "es", label: "Español" },
  { code: "pt", label: "Português" },
];

/** Human label for a campaign language code (falls back to the code). */
export function campaignLanguageLabel(code: string | null | undefined): string {
  if (!code) return "—";
  return CAMPAIGN_LANGUAGES.find((l) => l.code === code)?.label ?? code;
}

// ---------------------------------------------------------------------------
// user-service
// ---------------------------------------------------------------------------

export const usersApi = {
  me: (token: string) => request<User>(token, "/api/users/me"),
  updateMe: (token: string, displayName: string) =>
    request<User>(token, "/api/users/me", jsonInit("PATCH", { display_name: displayName })),
  uploadAvatar: (token: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<User>(token, "/api/users/me/avatar", { method: "PUT", body: fd });
  },
  get: (token: string, userId: string) => request<User>(token, `/api/users/${userId}`),
  search: (token: string, q: string) => request<User[]>(token, `/api/users/search?q=${encodeURIComponent(q)}`),
};

export const voiceApi = {
  enroll: (token: string, campaignId: string, file: File) => {
    const fd = new FormData();
    fd.append("campaign_id", campaignId);
    fd.append("file", file);
    return request<VoiceProfile>(token, "/api/voice/enroll", { method: "POST", body: fd });
  },
  profiles: (token: string, campaignId?: string) =>
    request<VoiceProfile[]>(
      token,
      `/api/voice/profiles${campaignId ? `?campaign_id=${campaignId}` : ""}`,
    ),
  remove: (token: string, profileId: string) =>
    request<void>(token, `/api/voice/profiles/${profileId}`, { method: "DELETE" }),
};

// ---------------------------------------------------------------------------
// session-service
// ---------------------------------------------------------------------------

export const sessionsApi = {
  list: (token: string, campaignId: string) =>
    request<Session[]>(token, `/api/sessions?campaign_id=${campaignId}`),
  create: (token: string, body: { campaign_id: string; title?: string | null; session_no?: number | null }) =>
    request<Session>(token, "/api/sessions", jsonInit("POST", body)),
  get: (token: string, id: string) => request<SessionDetail>(token, `/api/sessions/${id}`),
  update: (token: string, id: string, body: { title?: string | null; session_no?: number | null }) =>
    request<Session>(token, `/api/sessions/${id}`, jsonInit("PATCH", body)),
  uploadRecording: (token: string, id: string, file: File, durationSec?: number) => {
    const fd = new FormData();
    fd.append("file", file);
    if (durationSec !== undefined && durationSec > 0) fd.append("duration_sec", String(durationSec));
    return request<SessionDetail>(token, `/api/sessions/${id}/recording`, { method: "PUT", body: fd });
  },
  speakers: (token: string, sessionId: string) =>
    request<SpeakerAssignment[]>(token, `/api/sessions/${sessionId}/speakers`),
  assignSpeaker: (token: string, sessionId: string, speakerLabel: string, memberId: string, enrolledVoiceprint = false) =>
    request<SpeakerAssignment>(
      token,
      `/api/sessions/${sessionId}/speakers/${encodeURIComponent(speakerLabel)}/assign`,
      jsonInit("POST", { member_id: memberId, enrolled_voiceprint: enrolledVoiceprint }),
    ),
};

// ---------------------------------------------------------------------------
// wiki-service
// ---------------------------------------------------------------------------

export const PAGE_KINDS: WikiPageKind[] = ["character", "location", "faction", "item", "quest", "event"];

/** Menu labels for the wiki categories (singular values, plural display). */
export const PAGE_KIND_LABELS: Record<WikiPageKind, string> = {
  character: "Characters",
  location: "Locations",
  faction: "Factions",
  item: "Items",
  quest: "Quests",
  event: "Events",
};

/** Singular titles for badges on the page detail view. */
export const PAGE_KIND_TITLES: Record<WikiPageKind, string> = {
  character: "Character",
  location: "Location",
  faction: "Faction",
  item: "Item",
  quest: "Quest",
  event: "Event",
};

/** Structured attributes per kind, mirrored from wiki-service
 * (services/wiki-service/app/page_attributes.py). Rendered as a dedicated
 * block on the page detail instead of raw JSON. */
export interface PageAttributes {
  character_type?: "npc" | "player";
  race?: string | null;
  class?: string | null;
  /** Physical traits (free text, DM-curated): static info table fields. */
  gender?: string | null;
  height?: string | null;
  weight?: string | null;
  age?: string | null;
  location_type?:
    | "city"
    | "town"
    | "village"
    | "region"
    | "continent"
    | "world"
    | "building"
    | "structure"
    | "dungeon"
    | "wilderness"
    | "other";
  region?: string | null;
  latitude?: number | null;
  longitude?: number | null;
  /** Type-specific location fields (mirrors wiki-service
   * LocationAttributes). Every field is optional; which ones a page may
   * carry depends on location_type. */
  founded?: string | null;
  population?: string | null;
  government?: string | null;
  ruler?: string | null;
  demographics?: string | null;
  economy?: string | null;
  defenses?: string | null;
  religion?: string | null;
  districts?: string[] | null;
  notable_locations?: string[] | null;
  capital?: string | null;
  terrain?: string | null;
  climate?: string | null;
  pantheon?: string | null;
  planes?: string | null;
  purpose?: string | null;
  entrance?: string | null;
  levels?: string | null;
  hazards?: string | null;
  flora_fauna?: string | null;
  faction_type?: string | null;
  leader?: string | null;
  headquarters?: string | null;
  item_type?: string | null;
  rarity?: string | null;
  owner?: string | null;
  quest_status?: "open" | "in_progress" | "completed" | "failed" | string;
  giver?: string | null;
  reward?: string | null;
  // event pages (mirrors wiki-service EventAttributes)
  event_type?: "battle" | "negotiation" | "discovery" | "quest" | "catastrophe" | "political" | "other" | string;
  in_world_date?: string | null;
  participants?: string[] | null;
  event_status?: "ongoing" | "resolved" | "unknown" | string;
  [key: string]: unknown;
}
export const PAGE_STATUSES: WikiPageStatus[] = ["draft", "pending_review", "published", "archived"];
export const VISIBILITIES: WikiVisibility[] = ["public", "dm_only", "hidden"];

export const wikiApi = {
  pages: (
    token: string,
    campaignId: string,
    opts: { kind?: WikiPageKind; status?: WikiPageStatus; q?: string; limit?: number; offset?: number } = {},
  ) => {
    const qs = new URLSearchParams({ campaign_id: campaignId });
    if (opts.kind) qs.set("kind", opts.kind);
    if (opts.status) qs.set("status", opts.status);
    if (opts.q) qs.set("q", opts.q);
    if (opts.limit) qs.set("limit", String(opts.limit));
    if (opts.offset) qs.set("offset", String(opts.offset));
    return request<PageSummary[]>(token, `/api/wiki/pages?${qs.toString()}`);
  },
  page: (token: string, pageId: string) => request<PageDetail>(token, `/api/wiki/pages/${pageId}`),
  uploadImage: (token: string, pageId: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<PageDetail>(token, `/api/wiki/pages/${pageId}/image`, { method: "PUT", body: fd });
  },
  createPage: (
    token: string,
    body: {
      campaign_id: string;
      kind: WikiPageKind;
      title: string;
      slug?: string;
      content_json?: Record<string, unknown>;
      status?: WikiPageStatus;
      visibility?: WikiVisibility;
      confidence?: number;
      source_session_id?: string;
      change_note?: string;
    },
  ) => request<PageOut>(token, "/api/wiki/pages", jsonInit("POST", body)),
  updatePage: (
    token: string,
    pageId: string,
    body: { title?: string; slug?: string; content_json?: Record<string, unknown>; change_note?: string },
  ) => request<PageOut>(token, `/api/wiki/pages/${pageId}`, jsonInit("PATCH", body)),
  approve: (token: string, pageId: string) =>
    request<PageOut>(token, `/api/wiki/pages/${pageId}/approve`, jsonInit("POST")),
  archive: (token: string, pageId: string) =>
    request<PageOut>(token, `/api/wiki/pages/${pageId}/archive`, jsonInit("POST")),
  visibility: (token: string, pageId: string, visibility: WikiVisibility) =>
    request<PageOut>(token, `/api/wiki/pages/${pageId}/visibility`, jsonInit("PATCH", { visibility })),
  versions: (token: string, pageId: string) =>
    request<PageVersion[]>(token, `/api/wiki/pages/${pageId}/versions`),
  relations: (token: string, pageId: string) =>
    request<PageRelation[]>(token, `/api/wiki/pages/${pageId}/relations`),
  createRelation: (token: string, pageId: string, relatedPageId: string, relationType: string) =>
    request<PageRelation>(token, `/api/wiki/pages/${pageId}/relations`, jsonInit("POST", { related_page_id: relatedPageId, relation_type: relationType })),
  deleteRelation: (token: string, pageId: string, relationId: string) =>
    request<void>(token, `/api/wiki/pages/${pageId}/relations/${relationId}`, { method: "DELETE" }),

  timeline: (token: string, campaignId: string) =>
    request<TimelineEvent[]>(token, `/api/wiki/timeline?campaign_id=${campaignId}`),
  createTimelineEvent: (
    token: string,
    body: { campaign_id: string; page_id?: string | null; in_world_date?: string | null; summary: string; source_session_id?: string | null; approved?: boolean },
  ) => request<TimelineEvent>(token, "/api/wiki/timeline", jsonInit("POST", body)),
  updateTimelineEvent: (
    token: string,
    eventId: string,
    body: { page_id?: string | null; in_world_date?: string | null; summary?: string; approved?: boolean },
  ) => request<TimelineEvent>(token, `/api/wiki/timeline/${eventId}`, jsonInit("PATCH", body)),

  // DEBUG (developer accounts): hard-delete every page of a campaign.
  deleteAllPages: (token: string, campaignId: string) =>
    request<{ campaign_id: string; deleted: number }>(
      token,
      "/api/wiki/campaigns/" + campaignId + "/pages",
      { method: "DELETE" },
    ),
  // DEBUG (developer accounts): hard-delete every timeline event of a campaign.
  deleteAllTimelineEvents: (token: string, campaignId: string) =>
    request<{ campaign_id: string; deleted: number }>(
      token,
      "/api/wiki/campaigns/" + campaignId + "/timeline",
      { method: "DELETE" },
    ),
};

// ---------------------------------------------------------------------------
// content-service (job status + llm config)
// ---------------------------------------------------------------------------

export interface RegenerateResponse {
  session_id: string;
  campaign_id: string;
  queued: boolean;
  speakers: number;
  llm_model: string;
  prompt_version: string;
}

export const contentApi = {
  job: (token: string, sessionId: string) =>
    request<ContentJobResponse>(token, `/api/content/jobs/${sessionId}`),
  summary: (token: string, sessionId: string) =>
    request<ContentSummaryResponse>(token, `/api/content/summaries/${sessionId}`),
  llm: (token: string) =>
    request<{ provider: string; model: string; prompt_version: string }>(token, "/api/content/llm"),

  // DEBUG (developer accounts): re-run wiki generation from the same transcript.
  regenerate: (token: string, sessionId: string) =>
    request<RegenerateResponse>(
      token,
      "/api/content/sessions/" + sessionId + "/regenerate",
      jsonInit("POST"),
    ),
};

// ---------------------------------------------------------------------------
// transcription / speaker status endpoints
// ---------------------------------------------------------------------------

export const systemApi = {
  transcriptionModels: (token: string) =>
    request<{ asr_model: string; diarization_model: string; compute_type: string }>(
      token,
      "/api/transcription/models",
    ),
  transcriptionStatus: (token: string) =>
    request<{ device: string | null; asr_loaded: boolean; diarizer_loaded: boolean }>(
      token,
      "/api/transcription/status",
    ),
  speakerModel: (token: string) =>
    request<{ model: string; threshold: number; collection: string }>(
      token,
      "/api/speakers/embedding-model",
    ),
};

export type { ApiError as SdkApiError };