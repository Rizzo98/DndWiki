"""notification-service — FastAPI entrypoint."""

from fastapi import FastAPI

from app.core.config import get_settings

settings = get_settings()
app = FastAPI(title="notification-service", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok", "service": settings.service_name}


@app.get("/api/notifications")
async def list_notifications(unread_only: bool = False):
    """In-app inbox for the current user. TODO: read dnd_content.notifications."""
    return []


@app.post("/api/notifications/{notification_id}/read")
async def mark_read(notification_id: str):
    # TODO
    return {"notification_id": notification_id, "read": True}
