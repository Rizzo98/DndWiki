"""MinIO (S3-compatible) object storage for the transcript artifacts.

The refiner reads the diarized transcript (transcripts/<session>/...) and
writes the refined version back over the same objects, so the URIs the UI
polls never change.
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
        """Fetch and parse a JSON object ("bucket/key")."""
        async with self._session.client("s3", **self._client_kwargs()) as s3:
            resp = await s3.get_object(Bucket=bucket, Key=key)
            body = await resp["Body"].read()
        return json.loads(body.decode("utf-8"))

    async def put_json(self, bucket: str, key: str, obj: dict[str, Any]) -> None:
        """Write a JSON object ("bucket/key")."""
        data = json.dumps(obj).encode("utf-8")
        async with self._session.client("s3", **self._client_kwargs()) as s3:
            await s3.put_object(
                Bucket=bucket, Key=key, Body=data, ContentType="application/json"
            )
        logger.info("wrote s3://%s/%s", bucket, key)
