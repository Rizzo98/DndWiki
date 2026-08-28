"""campaign-service — FastAPI entrypoint.

Campaigns, memberships, roles (dm/player) and invites. Owns the dnd_campaigns
database; exposes the membership check other services depend on.
"""

from fastapi import FastAPI

from app.api import campaigns, internal
from app.core.config import get_settings

settings = get_settings()
app = FastAPI(title="campaign-service", version="0.2.0")

app.include_router(campaigns.router)
app.include_router(internal.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": settings.service_name}
