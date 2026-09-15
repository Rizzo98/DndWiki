"""Service-to-service clients for the attribution engine."""

from app.clients.base import BaseClient, ConflictTransition, ServiceError
from app.clients.campaign_service import CampaignServiceClient
from app.clients.session_service import (
    STATUS_ATTRIBUTING,
    STATUS_ATTRIBUTION_READY,
    STATUS_ATTRIBUTION_REVIEW,
    STATUS_FAILED,
    STATUS_SUMMARIZING,
    SessionServiceClient,
)
from app.clients.speaker_service import SpeakerServiceClient
from app.clients.wiki_service import WikiServiceClient

__all__ = [
    "STATUS_ATTRIBUTING",
    "STATUS_ATTRIBUTION_READY",
    "STATUS_ATTRIBUTION_REVIEW",
    "STATUS_FAILED",
    "STATUS_SUMMARIZING",
    "BaseClient",
    "CampaignServiceClient",
    "ConflictTransition",
    "ServiceError",
    "SessionServiceClient",
    "SpeakerServiceClient",
    "WikiServiceClient",
]
