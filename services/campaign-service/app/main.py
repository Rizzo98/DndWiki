"""campaign-service — FastAPI entrypoint.

Campaigns, memberships, roles (dm/player) and invites. Owns the dnd_campaigns
database; exposes the membership check other services depend on. Wires MinIO
storage into app.state for campaign cover art; the API routers pull it from
FastAPI dependencies.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import campaigns, internal
from app.core.config import get_settings
from app.storage import ObjectStorage

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Expose the object storage handle (covers live in campaign-assets)."""
    app.state.storage = ObjectStorage(settings)
    yield


app = FastAPI(title="campaign-service", version="0.2.0", lifespan=lifespan)

app.include_router(campaigns.router)
app.include_router(internal.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": settings.service_name}
