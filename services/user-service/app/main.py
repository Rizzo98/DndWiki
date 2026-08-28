"""user-service — FastAPI entrypoint.

Wires MinIO storage, the Qdrant voiceprint store, the SpeechBrain embedder
and the campaign-service client into app.state; the API routers pull them
from FastAPI dependencies. Qdrant/SpeechBrain startup failures are non-fatal
(lazy retry on first use).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import internal, users, voice
from app.clients.campaigns import CampaignServiceClient
from app.core.config import get_settings
from app.embedding import VoiceEmbedder
from app.qdrant import VoiceprintStore
from app.storage import ObjectStorage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start external clients; Qdrant unavailability is non-fatal (lazy retry)."""
    voiceprints = VoiceprintStore(settings)
    try:
        await voiceprints.ensure_collection()
    except Exception:  # noqa: BLE001 - Qdrant may be starting; upsert() retries
        logger.warning("Qdrant not reachable at startup; will retry on first enrollment")
    app.state.storage = ObjectStorage(settings)
    app.state.voiceprints = voiceprints
    app.state.embedder = VoiceEmbedder(settings)
    app.state.campaign_client = CampaignServiceClient(settings.campaign_service_url, settings)
    yield


app = FastAPI(title="user-service", version="0.2.0", lifespan=lifespan)

app.include_router(users.router)
app.include_router(voice.router)
app.include_router(internal.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": settings.service_name}
