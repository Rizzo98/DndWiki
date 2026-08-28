"""search-service — FastAPI entrypoint."""

from fastapi import FastAPI

from app.core.config import get_settings

settings = get_settings()
app = FastAPI(title="search-service", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok", "service": settings.service_name}


@app.get("/api/search")
async def search(q: str, campaign_id: str):
    """Search published+public wiki pages within a campaign (Meilisearch).

    TODO: client = Client(cfg.meili_url, api_key=cfg.meili_master_key)
          client.index("wiki_pages").search(q, {"filter": f"campaign_id = {campaign_id}"})
    """
    return {"q": q, "campaign_id": campaign_id, "hits": []}
