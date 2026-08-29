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
  dm_user_id: string;
  status: "active" | "archived";
  settings: Record<string, unknown>;
  created_at: string;
  /** Filled by the API with the calling user's role in this campaign. */
  my_role: Role | null;
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
  | "identifying_speakers"
  | "speakers_identified"
  | "speaker_pending"
  | "generating_wiki"
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
