"""MinIO (S3-compatible) object storage for transcript input.

The worker downloads the named transcript JSON (``transcripts/<session_id>/transcript.json``,
produced by transcription-service) straight into memory — a few MB at most.
Everything else that needs object storage uses presigned URLs from the
session-service instead.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aioboto3

from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)


class ObjectStorage:
    """Thin wrapper over the S3 client for the transcripts bucket."""

    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings
        self._session = aioboto3.Session()

    @property
    def _endpoint_url(self) -> str:
        scheme = "https" if self._settings.minio_secure else "http"
        return f"{scheme}://{self._settings.minio_endpoint}"

    def _client_kwargs(self) -> dict[str, Any]:
        return {
            "endpoint_url": self._endpoint_url,
            "aws_access_key_id": self._settings.minio_access_key,
            "aws_secret_access_key": self._settings.minio_secret_key,
            "region_name": "us-east-1",
            "use_ssl": self._settings.minio_secure,
        }

    async def read_json(self, bucket: str, key: str) -> dict[str, Any]:
        """Fetch and parse a JSON object ("bucket/key") into a dict."""
        async with self._session.client("s3", **self._client_kwargs()) as s3:
            response = await s3.get_object(Bucket=bucket, Key=key)
            body = await response["Body"].read()
        logger.info("downloaded s3://%s/%s (%d bytes)", bucket, key, len(body))
        return json.loads(body)
