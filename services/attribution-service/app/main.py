"""attribution-service - FastAPI entrypoint (review, voices, internal API).

The heavy pass lives in app.workers.compute; this process serves the review the
DM answers and the artifacts content-service reads.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import internal, review
from app.broker import EventPublisher
from app.core.config import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the event publisher; a broker that is not up yet is not fatal."""
    publisher = EventPublisher(settings.rabbitmq_url)
    try:
        await publisher.connect()
    except Exception:  # noqa: BLE001 - publish() retries lazily
        logger.warning("RabbitMQ not reachable at startup; will retry on first publish")
    app.state.publisher = publisher
    try:
        yield
    finally:
        await publisher.close()


app = FastAPI(title="attribution-service", version="0.1.0", lifespan=lifespan)
app.include_router(review.router)
app.include_router(internal.router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": settings.service_name,
        "attribution_enabled": settings.attribution_enabled,
        "engine_version": settings.engine_version,
    }


@app.get("/api/attribution/config")
async def attribution_config():
    """The thresholds in force, so the UI can explain a verdict without guessing."""
    return {
        "enabled": settings.attribution_enabled,
        "engine_version": settings.engine_version,
        "auto_high_pmin": settings.auto_high_pmin,
        "auto_high_margin": settings.auto_high_margin,
        "auto_low_pmin": settings.auto_low_pmin,
        "propagated_pmin": settings.propagated_pmin,
        "propagated_margin": settings.propagated_margin,
        "max_questions": settings.max_questions,
        "gain_floor_bits": settings.gain_floor_bits,
        "target_unresolved": settings.target_unresolved,
        "evidence_prompt_version": settings.prompt_version,
    }
