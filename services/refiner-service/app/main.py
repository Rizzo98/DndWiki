"""refiner-service - FastAPI entrypoint (health)."""

from fastapi import FastAPI

from app.core.config import get_settings

settings = get_settings()
app = FastAPI(title="refiner-service", version="0.1.0")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": settings.service_name,
        "refiner_enabled": settings.refiner_enabled,
        "model": settings.effective_model,
    }
