# ADR-0001: Microservice architecture and technology stack

- **Status:** Accepted
- **Date:** 2025-01
- **Deciders:** Platform team

## Context

The platform must record D&D sessions from a phone, transcribe + diarize the audio
(WhisperX), attribute speakers via voice embeddings, and use LLMs to generate a
curated campaign wiki (characters, locations, events, timelines) with a
DM-admin/player-view permission split.

## Decision

Adopt a **microservice architecture** (9 backend services + 2 frontends) with the
following technology stack:

1. **Python 3.12 + FastAPI** for all backend services — the ML/LLM ecosystem is
   Python-native, and a single language across services maximizes code sharing via
   `libs/python/dnd_common`.
2. **PostgreSQL 16 with database-per-service** — relational integrity where it
   matters (memberships, sessions, wiki, versions); JSONB for page bodies.
3. **RabbitMQ (topic exchange `dnd.events`)** — queue-driven workers
   (transcription, speaker identification, LLM generation) decouple heavy compute
   from the API; domain events drive search + notifications. Kafka was considered
   and rejected for v1 (no replay/streaming requirement, extra ops cost).
4. **MinIO (S3 API)** for raw audio, transcripts, and generated assets — S3
   compatibility makes a future managed-S3 migration free.
5. **Qdrant** for voiceprint similarity search (ECAPA-TDNN embeddings) and future
   semantic wiki search.
6. **Redis** for caching (JWKS, session status) and rate limiting.
7. **Meilisearch** for typo-tolerant full-text wiki search.
8. **Keycloak 24 (OIDC)** — identity with realm roles `dm`/`player`; passwords
   never stored in our services.
9. **Traefik v3** as the edge gateway; each service validates JWTs via the shared
   auth lib (authorization stays close to the data).
10. **WhisperX** for ASR + diarization (product requirement), **SpeechBrain
    ECAPA-TDNN** for speaker embeddings, **LiteLLM** as the LLM abstraction
    (provider-swappable: OpenAI / Anthropic / Ollama / vLLM).
11. **Next.js 14** (web) and **Expo/React Native** (mobile) frontends.
12. **Docker Compose** for local dev (single command, full stack) with a GPU
    override; **Kubernetes** reserved for production.

## Consequences

**Positive**

- Heavy GPU/LLM work is isolated in queue-driven workers that scale independently.
- The async pipeline (record → transcribe → identify → generate → review) is
  resumable and observable at each step via `sessions.status`.
- Permissions are enforced at the data boundary (wiki-service) using roles from
  Keycloak + campaign membership.

**Negative / trade-offs**

- Nine services mean more moving parts; mitigated by the shared `dnd_common` lib,
  one compose file, and identical service skeletons.
- WhisperX on CPU is slow — GPU is the recommended path (`.gpu.yml` override).
- Distributed transactions are avoided by design; the session state machine is a
  saga with retries + DLQs.

## Alternatives considered

- **Monolith first**: rejected — the team explicitly wants microservices; the
  pipeline boundaries (audio processing vs. content) are natural scaling seams.
- **Kafka instead of RabbitMQ**: rejected for v1 (see above); the contract
  documents allow a later swap.
- **pgvector instead of Qdrant**: viable; Qdrant chosen for voiceprint-specific
  ergonomics and isolation from the OLTP DB.
- **Node/Go services**: rejected for language uniformity with the ML stack.
