"""Qdrant access for voiceprints (collection 'voiceprints', cosine, 192-d).

Owned by user-service: enrollment upserts embeddings here; speaker-service
queries them for matching. qdrant-client is imported lazily so the API (and
tests) run without it installed - only enrollment touches Qdrant.
"""

from __future__ import annotations

import logging

from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)


class VoiceprintStore:
    """Upsert/delete of voiceprint vectors in the 'voiceprints' collection."""

    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings
        self._client = None
        self._ensured = False

    def _get_client(self):
        if self._client is None:
            from qdrant_client import AsyncQdrantClient  # lazy: heavy/optional dep

            self._client = AsyncQdrantClient(url=self._settings.qdrant_url)
        return self._client

    async def ensure_collection(self) -> None:
        """Create the collection (192-d cosine) on first use, if missing."""
        if self._ensured:
            return
        from qdrant_client import models  # lazy

        client = self._get_client()
        exists = await client.collection_exists(self._settings.voiceprint_collection)
        if not exists:
            await client.create_collection(
                collection_name=self._settings.voiceprint_collection,
                vectors_config=models.VectorParams(
                    size=self._settings.embedding_dim,
                    distance=models.Distance.COSINE,
                ),
            )
            logger.info(
                "created Qdrant collection %s (dim=%s)",
                self._settings.voiceprint_collection,
                self._settings.embedding_dim,
            )
        self._ensured = True

    async def upsert(
        self, point_id: str, vector: list[float], payload: dict
    ) -> None:
        """Upsert one voiceprint vector with its payload."""
        from qdrant_client import models  # lazy

        client = self._get_client()
        await self.ensure_collection()
        await client.upsert(
            collection_name=self._settings.voiceprint_collection,
            points=[
                models.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload=payload,
                )
            ],
        )

    async def delete(self, point_id: str) -> None:
        """Remove a voiceprint point (re-enrollment / profile deletion)."""
        if not self._ensured:
            return  # nothing was ever written; nothing to remove
        client = self._get_client()
        await client.delete(
            collection_name=self._settings.voiceprint_collection,
            points_selector=[point_id],
        )
