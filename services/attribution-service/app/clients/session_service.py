"""session-service internal API: pipeline status, history, compatibility view."""

from __future__ import annotations

from typing import Any

from app.clients.base import BaseClient
from app.core.config import ServiceSettings

STATUS_ATTRIBUTING = "attributing"
STATUS_ATTRIBUTION_READY = "attribution_ready"
STATUS_ATTRIBUTION_REVIEW = "attribution_review"
STATUS_SUMMARIZING = "summarizing"
STATUS_FAILED = "failed"


class SessionServiceClient(BaseClient):
    """Drives the attribution stage of the session state machine."""

    def __init__(self, settings: ServiceSettings) -> None:
        super().__init__(settings, settings.session_service_url)

    async def update_status(
        self, session_id: str, status: str, error: str | None = None
    ) -> dict[str, Any]:
        return await self.request(
            "PATCH",
            f"/internal/sessions/{session_id}/status",
            json={"status": status, "error": error},
        )

    async def speaker_history(
        self,
        campaign_id: str,
        *,
        exclude_session_id: str | None = None,
        limit_sessions: int = 50,
        confirmed_only: bool = False,
    ) -> list[dict[str, Any]]:
        """The campaign's labelled history, for calibration.

        The existing enrollment read caps at five sessions and returns only
        confirmed, USER-linked assignments. Calibration wants ALL the labelled
        history and can use member-keyed labels that carry no user at all, so it
        asks for the uncapped internal read rather than widening the one the
        enrollment path still depends on (attribution-plan S3). Returns [] when
        the endpoint is not deployed yet: calibration then falls back to its
        cold-start defaults, which is a degrade and not a failure.
        """
        params: dict[str, Any] = {
            "limit_sessions": limit_sessions,
            "confirmed_only": str(confirmed_only).lower(),
            "include_member_keyed": "true",
        }
        if exclude_session_id:
            params["exclude_session_id"] = exclude_session_id
        try:
            payload = await self.request(
                "GET", f"/internal/campaigns/{campaign_id}/speaker-history", params=params
            )
        except Exception:  # noqa: BLE001 - history is an enhancement, never a requirement
            return []
        return payload if isinstance(payload, list) else payload.get("entries", [])

    async def compatibility_assignments(self, session_id: str) -> list[dict[str, Any]]:
        """The derived speaker_assignments view (what the legacy panel reads)."""
        payload = await self.request(
            "GET", f"/internal/sessions/{session_id}/speaker-assignments"
        )
        return payload if isinstance(payload, list) else []

    async def write_compatibility_view(
        self, session_id: str, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Project the belief into the legacy view - the ONLY writer of it.

        The compatibility layer must never call the old upsert path again: it
        overwrites status unconditionally but only replaces user_id/confidence
        when the incoming value is non-None, so a redelivery silently downgrades
        a confirmed row back to 'auto' while keeping the previous run's user
        attached - the DM's work undone and the row internally inconsistent
        (attribution-plan S8). Here the view is a pure projection of the belief.
        """
        return await self.request(
            "PUT", f"/internal/sessions/{session_id}/speaker-assignments", json=rows
        )
