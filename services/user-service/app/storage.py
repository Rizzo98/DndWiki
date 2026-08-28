"""MinIO (S3-compatible) object storage for voice samples and avatars.

Uses aioboto3 so uploads stream chunk-by-chunk and all URLs handed to clients
are short-lived presigned ones (never public) - same pattern as session-service.
"""

from __future__ import annotations

import logging
from typing import Any

import aioboto3

from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)


class ObjectStorage:
    """Thin wrapper over the S3 client for the voice-samples/wiki-assets buckets."""

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

    async def put_object_stream(
        self,
        bucket: str,
        key: str,
        stream: Any,
        content_type: str,
    ) -> None:
        """Stream a file-like object (async or sync) into "bucket/key"."""
        async with self._session.client("s3", **self._client_kwargs()) as s3:
            await s3.upload_fileobj(stream, bucket, key, ExtraArgs={"ContentType": content_type})

    async def presigned_get(self, bucket: str, key: str, expires_sec: int | None = None) -> str:
        """Short-lived GET URL for "bucket/key"."""
        ttl = expires_sec or self._settings.presigned_url_ttl_sec
        async with self._session.client("s3", **self._client_kwargs()) as s3:
            return await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=ttl,
            )
