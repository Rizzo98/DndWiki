"""MinIO (S3-compatible) object storage for raw audio and transcript artifacts.

The worker downloads the session recording to a staging file (for speaker-clip
extraction) and reads diarization/transcript JSON documents back.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aioboto3

from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)


class ObjectStorage:
    """Thin wrapper over the S3 client for the recordings/transcripts buckets."""

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

    async def download_file(self, bucket: str, key: str, dest_path: str) -> None:
        """Stream "bucket/key" into a local file (no full-file buffering)."""
        async with self._session.client("s3", **self._client_kwargs()) as s3:
            # aioboto3/botocore need a synchronous file object (the transfer
            # runs in a worker thread); open() itself is trivial.
            with open(dest_path, "wb") as f:  # noqa: ASYNC230
                await s3.download_fileobj(bucket, key, f)
        logger.info("downloaded s3://%s/%s -> %s", bucket, key, dest_path)

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
