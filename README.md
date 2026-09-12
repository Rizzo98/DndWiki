# DnD Wiki — Campaign Wiki Platform

A platform for D&D groups to record their sessions and automatically turn them into a
living campaign wiki.

Players drop a phone at the center of the table and hit record. The audio is uploaded,
transcribed with speaker diarization (WhisperX), speakers are matched to player
voiceprints, and LLMs distill the named transcript into a **session summary** the
Dungeon Master reviews line by line — correcting the AI and regenerating it until
it reads right. Only the confirmed summary is turned into wiki content —
characters, locations, events, and a campaign timeline. The DM curates everything;
players get read-only access.

## High-level flow

```
phone recording ──▶ session-service ──▶ MinIO (raw audio)
                        │
                        ▼ RabbitMQ (transcription.jobs)
                transcription-service (WhisperX: VAD → ASR → diarize → align)
                        │  or a cloud API backend (docker-compose.transcription.yml):
                        │     Deepgram Nova 3 (default) | OpenAI gpt-4o-transcribe-diarize
                        │     | AssemblyAI Universal-3.5 Pro (TRANSCRIPTION_PROVIDER)
                        │  transcripts + diarized segments → MinIO
                        ▼ RabbitMQ (transcripts.refine)
                refiner-service (LLM contextual diarization: corrected text, stable speaker labels)
                        ▼ RabbitMQ (speakers.identify)
                speaker-service (ECAPA-TDNN embeddings ↔ voiceprints in Qdrant)
                        │  unknown speakers flagged for DM assignment
                        ▼ RabbitMQ (content.generate)
                content-service (LLM structured extraction → DRAFT session summary)
                        │  DM reviews/regenerates it on the session page
                        │  summary confirmed (summary.confirmed) → wiki drafts
                        │  drafts (pending_review)
                        ▼
                wiki-service (DM approves/edits → published) ──▶ search-service
```

## Repository layout

```
DnDWiki/
├── apps/                  # User-facing applications
│   ├── web/               #   Next.js wiki UI (players + DM console)
│   └── mobile/            #   Expo app (session recording + upload)
├── services/              # Backend microservices (one folder per service)
│   ├── user-service/      #   Users, profiles, voiceprint enrollment
│   ├── campaign-service/  #   Campaigns, memberships, roles (DM/player)
│   ├── session-service/   #   Recording upload, session state machine
│   ├── transcription-service/  # WhisperX worker (GPU, on-prem)
│   ├── transcription-openai-service/  # Cloud API variant (OpenAI speech-to-text)
│   ├── transcription-deepgram-service/  # Cloud API variant (Deepgram Nova 3 + diarization)
│   ├── transcription-assemblyai-service/  # Cloud API variant (AssemblyAI Universal-3.5 Pro + diarization)
│   ├── refiner-service/   #   LLM contextual diarization (transcript + speaker-label fixes)
│   ├── speaker-service/   #   Speaker identification from voiceprints
│   ├── content-service/   #   LLM transcript → session summary → wiki drafts
│   ├── wiki-service/      #   Wiki CRUD, versions, visibility, approval
│   ├── search-service/    #   Meilisearch indexing
│   └── notification-service/   # Emails / webhooks / push
├── libs/                  # Shared code
│   ├── python/dnd_common/ #   FastAPI/JWT/RabbitMQ/SQLAlchemy common layer
│   └── typescript/dnd-sdk/#   Typed client + shared types for frontends
├── infrastructure/        # Infra configs (postgres, rabbitmq, keycloak, monitoring)
├── ml/                    # Model notes (WhisperX, pyannote, ECAPA-TDNN)
├── docs/                  # Architecture, data model, event contracts, ADRs
├── docker-compose.yml     # Full dev stack
└── docker-compose.gpu.yml # GPU override for transcription
```

## Quickstart (local dev)

Prerequisites: Docker Desktop (or Docker Engine + compose v2).

```bash
cp .env.example .env        # tweak secrets if you like
docker compose up -d --build
docker compose ps
```

| What | URL | Credentials (dev) |
|---|---|---|
| Web UI | http://localhost:3000 (or http://dnd.localhost) | — |
| API gateway (Traefik) | http://api.dnd.localhost | — |
| Keycloak (identity) | http://localhost:8080 | `admin` / `admin` |
| MinIO (object storage) | http://localhost:19001 | `dndminio` / `dndminio123` |
| RabbitMQ (broker) | http://localhost:15672 | `dnd` / `dnd` |
| Meilisearch (search) | http://localhost:7700 | key: `dndsearchkey` |
| Qdrant (vectors) | http://localhost:6333 | — |
| Traefik dashboard | http://localhost:8090 | — |

> **Windows note:** `*.localhost` resolves to loopback automatically on Windows (RFC 6761),
> so no hosts file entries are needed — the pretty hostnames work out of the box.

GPU (recommended for transcription):

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

Requires Docker Desktop with WSL2 + NVIDIA support (or `nvidia-container-toolkit`).
Set `HF_TOKEN` in `.env` — the pyannote diarization model is gated on Hugging Face.

API transcription (no local ML models — cloud Speech-to-Text):

```bash
# Deepgram Nova 3 (default; ASR + speaker diarization in one request)
docker compose -f docker-compose.yml -f docker-compose.transcription.yml up -d --build
# OpenAI gpt-4o-transcribe-diarize
TRANSCRIPTION_PROVIDER=openai docker compose -f docker-compose.yml -f docker-compose.transcription.yml up -d --build
```

One override file, two interchangeable backends: `TRANSCRIPTION_PROVIDER`
(deepgram | openai) selects the engine, so switching is a single variable.
Both swap the WhisperX worker for a thin HTTP worker calling a cloud
Speech-to-Text API: no GPU, no model downloads. Speaker identification still
runs locally (lightweight ECAPA-TDNN voiceprints vs Qdrant), so the
voiceprint naming system is preserved. Set `DEEPGRAM_API_KEY` (Deepgram) or
`OPENAI_API_KEY` (OpenAI) in `.env`. See
[services/transcription-deepgram-service/README.md](services/transcription-deepgram-service/README.md)
and
[services/transcription-openai-service/README.md](services/transcription-openai-service/README.md).
The GPU and API overrides are mutually exclusive — use one or the other
(`docker-compose.api.yml` remains as the OpenAI-only form).

Monitoring stack (optional):

```bash
docker compose --profile monitoring up -d
# Grafana: http://localhost:3001  (admin/admin)
```

## Repository hygiene

- **Secrets are never committed.** `.env` and all `.env.*` variants are ignored
  (only `.env.example` is tracked, per service); same for local dev tokens
  (`e2e-*.txt`, `kc-token.txt`), certificates/keys (`*.pem`, `*.key`, ...), and
  build logs (`rebuild-log.txt`).
- Caches and build output (`__pycache__`, `.pytest_cache`, `.ruff_cache`,
  `.next/`, `dist/`, `node_modules/`, virtualenvs) are gitignored repo-wide.
- Empty directories that must exist in a fresh checkout are kept with a
  `.gitkeep` placeholder (e.g. `apps/web/public/.gitkeep`).

## Documentation

- [docs/architecture.md](docs/architecture.md) — service catalog, technology decisions, data flow
- [docs/data-model.md](docs/data-model.md) — logical schema per service
- [docs/event-contracts.md](docs/event-contracts.md) — message contracts on the broker
- [docs/adr/](docs/adr/) — architecture decision records

## Tech stack at a glance

| Layer | Technology |
|---|---|
| Business services | Python 3.12 · FastAPI · SQLAlchemy 2 (async) |
| AI/ML services | WhisperX · pyannote 3.1 (on-prem), or cloud API transcription: Deepgram Nova 3 (default) / OpenAI `gpt-4o-transcribe-diarize` · SpeechBrain ECAPA-TDNN · LiteLLM |
| Identity | Keycloak 24 (OIDC, realm roles `dm` / `player`) |
| Primary DB | PostgreSQL 16 (database-per-service) |
| Vector DB | Qdrant (voiceprints + future semantic search) |
| Object storage | MinIO (S3 API): `recordings`, `transcripts`, `wiki-assets` |
| Broker | RabbitMQ 3.13 (topic exchange `dnd.events`) |
| Cache | Redis 7 |
| Search | Meilisearch |
| Gateway | Traefik v3 (edge routing, JWT enforced per service) |
| Frontends | Next.js 14 (web) · React Native / Expo (mobile) |
| Observability | Prometheus · Grafana · Loki · Tempo (profile) |
