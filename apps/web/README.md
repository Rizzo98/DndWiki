# apps/web — DnD Wiki web UI

Next.js 14 (App Router) client that drives **every** implemented backend
feature: Keycloak login, campaigns/members/invites, session recording upload +
pipeline tracking, wiki pages with DM review, timeline, voiceprint enrollment,
and system status.

## How it talks to the backend

- The browser authenticates with **Keycloak** (public client `dnd-web`,
  standard redirect flow, S256 PKCE) via `keycloak-js`.
- Every API call goes to `/api/backend/...` — a Next.js route handler that
  forwards the request **server-side** to the Traefik gateway
  (`NEXT_PUBLIC_API_BASE_URL`) together with the user's access token. This
  avoids CORS entirely (the services ship without CORS middleware).
- Presigned MinIO URLs returned by the services point at the docker-internal
  host `http://minio:9000/...`, which the browser cannot resolve. Media
  (audio, transcripts, avatars, voice samples) is instead served through
  `/api/object?url=...` — a route handler that fetches the object server-side
  (the web container is on the same Docker network) and streams it back,
  forwarding `Range` headers so the audio player can seek.
- When running `npm run dev` on the host (outside Docker), add
  `127.0.0.1 minio` to your hosts file so the server can reach MinIO.

## Pages

| Route | Purpose |
|---|---|
| `/` | Landing + Keycloak sign-in |
| `/campaigns` | Campaign list, create (you become DM), accept invite |
| `/campaigns/[id]` | Workspace: overview, wiki, sessions, timeline, members, invites |
| `/campaigns/[id]/pages/[pageId]` | Page viewer + DM edit/approve/archive/visibility/relations/versions |
| `/campaigns/[id]/sessions/[sessionId]` | Recording upload, in-page audio player (click transcript to seek), transcript + diarization viewer, live pipeline status, speaker assignment |
| `/profile` | Display name, avatar, voiceprint enrollment |
| `/system` | Transcription/speaker model + runtime status (read-only) |

DM-only actions (edit/approve/archive/visibility, member & invite management,
speaker assignment) are hidden for players; the backend enforces the same
rules regardless.

## Structure

```
app/
├── api/backend/[...path]/route.ts   # BFF proxy to the API gateway
├── layout.tsx                       # AuthProvider + nav shell
├── page.tsx                         # landing / sign-in
├── campaigns/                       # list + workspace
├── profile/                         # profile + voiceprints
├── system/                          # pipeline status
components/
├── ui.tsx                           # buttons, cards, badges, form controls
├── nav.tsx
├── page-content.tsx                 # renders wiki content_json
└── campaign/                        # workspace tabs (overview/members/invites/sessions/wiki/timeline)
lib/
├── auth.tsx                         # keycloak-js provider (auto token refresh)
├── api.ts                           # typed client for every endpoint
├── use-async.ts                     # data-fetching hook
└── env.ts
```

## Env

Copy `.env.example` to `.env.local` and adjust.

| Var | Purpose |
|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | gateway base (`http://api.dnd.localhost`) |
| `NEXT_PUBLIC_KEYCLOAK_URL` | `http://localhost:8080` |
| `NEXT_PUBLIC_KEYCLOAK_REALM` | `dnd` |
| `NEXT_PUBLIC_KEYCLOAK_CLIENT_ID` | `dnd-web` |

> Windows: `*.localhost` resolves to loopback automatically (RFC 6761), so no hosts
> file entries are needed. The Next.js server reaches the gateway via the compose
> service name (`API_GATEWAY_URL`), so containers never need a hostname mapping.

## Local dev

```bash
# 1. start the backend stack
docker compose up -d --build

# 2. build the local SDK package (the app imports @dnd-wiki/sdk via file:)
cd libs/typescript/dnd-sdk && npm install && npm run build

# 3. run the UI
cd apps/web
npm install
npm run dev          # http://localhost:3000
```

Create a user in the Keycloak console (`http://localhost:8080`, admin/admin),
assign the `player` and/or `dm` realm role, then sign in.

## Notes

- Sessions poll `GET /api/sessions/{id}` every 10 s while the pipeline is
  active (uploaded → … → published/failed).
- `content_json` of wiki pages is rendered structurally (summary, aliases,
  facts, participants, timeline); arbitrary JSON falls back to a pretty print.
