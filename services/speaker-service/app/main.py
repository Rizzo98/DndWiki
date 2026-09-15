"""speaker-service — FastAPI entrypoint (health + enrollment helpers)."""

from fastapi import FastAPI

from app.api import internal
from app.core.config import get_settings

settings = get_settings()
app = FastAPI(title="speaker-service", version="0.1.0")
app.include_router(internal.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": settings.service_name}


@app.get("/api/speakers/embedding-model")
async def embedding_model_info():
    return {
        "model": settings.voice_embedding_model,
        "threshold": settings.speaker_match_threshold,
        "collection": "voiceprints",
    }
