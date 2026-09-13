"""session-service — FastAPI entrypoint.

Wires the RabbitMQ publisher, MinIO storage and campaign client into
app.state; the API routers pull them from FastAPI dependencies.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import internal, sessions
from app.broker import EventPublisher
from app.clients.campaigns import CampaignServiceClient
from app.clients.content import ContentServiceClient
from app.core.config import get_settings
from app.storage import ObjectStorage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start external clients; RabbitMQ connect failure is non-fatal (lazy retry)."""
    publisher = EventPublisher(settings.rabbitmq_url)
    try:
        await publisher.connect()
    except Exception:  # noqa: BLE001 — broker may be starting; publish() retries lazily
        logger.warning("RabbitMQ not reachable at startup; will retry on first publish")
    app.state.publisher = publisher
    app.state.storage = ObjectStorage(settings)
    app.state.campaign_client = CampaignServiceClient(settings.campaign_service_url, settings)
    app.state.content_client = ContentServiceClient(settings.content_service_url, settings)
    try:
        yield
    finally:
        await publisher.close()


app = FastAPI(title="session-service", version="0.2.0", lifespan=lifespan)

app.include_router(sessions.router)
app.include_router(internal.router)
app.include_router(internal.campaigns_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": settings.service_name}
