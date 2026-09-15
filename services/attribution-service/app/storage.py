"""MinIO (S3-compatible) access for the transcription artifacts.

The engine reads the DIARIZED, text-corrected transcript that speaker-service and
refiner-service produced, and writes the attributed artifact that content-service
consumes instead of the label map.
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

    async def read_json(self, key: str, bucket: str | None = None) -> dict[str, Any] | None:
        """Read a JSON object; None when it does not exist (callers degrade)."""
        bucket = bucket or self._settings.minio_transcripts_bucket
        try:
            async with self._session.client("s3", **self._client_kwargs()) as s3:
                resp = await s3.get_object(Bucket=bucket, Key=key)
                body = await resp["Body"].read()
        except Exception:  # noqa: BLE001 - a missing artifact is a normal state here
            logger.warning("could not read s3://%s/%s", bucket, key)
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            logger.exception("s3://%s/%s is not valid JSON", bucket, key)
            return None

    async def put_json(self, key: str, obj: dict[str, Any], bucket: str | None = None) -> None:
        bucket = bucket or self._settings.minio_transcripts_bucket
        data = json.dumps(obj).encode("utf-8")
        async with self._session.client("s3", **self._client_kwargs()) as s3:
            await s3.put_object(
                Bucket=bucket, Key=key, Body=data, ContentType="application/json"
            )
        logger.info("wrote s3://%s/%s", bucket, key)

    async def delete_prefix(self, prefix: str, bucket: str | None = None) -> int:
        """Delete every object under a prefix (session deletion)."""
        bucket = bucket or self._settings.minio_transcripts_bucket
        removed = 0
        async with self._session.client("s3", **self._client_kwargs()) as s3:
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                keys = [{"Key": item["Key"]} for item in page.get("Contents", [])]
                if not keys:
                    continue
                await s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})
                removed += len(keys)
        return removed
