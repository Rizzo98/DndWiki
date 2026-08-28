"""Qdrant access for voiceprints (collection 'voiceprints', cosine, 192-d).

Owned by user-service: enrollment upserts embeddings here; speaker-service
queries them for matching and adds session-derived voiceprints when the DM
names a speaker. qdrant-client is imported lazily so the worker (and its
tests) run without it installed.
"""

from __future__ import annotations

import logging

from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)


class VoiceprintStore:
    """Search/upsert/delete of voiceprint vectors in the 'voiceprints' collection."""

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

    async def search(
        self, embedding: list[float], campaign_id: str, limit: int = 1
    ) -> list:
        """Best voiceprint matches for a campaign, best cosine first."""
        from qdrant_client import models  # lazy

        client = self._get_client()
        # qdrant-client >= 1.12 renamed search() to query_points()
        response = await client.query_points(
            collection_name=self._settings.voiceprint_collection,
            query=embedding,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="campaign_id", match=models.MatchValue(value=str(campaign_id))
                    )
                ]
            ),
            limit=limit,
        )
        return list(response.points)

    async def list_campaign(self, campaign_id: str, limit: int = 1000) -> list:
        """Every voiceprint point in a campaign (vectors + payloads).

        Used to build per-user enrollment centroids for re-clustering anchors;
        enrollment + session-derived points all live here, so the centroids
        accumulate across sessions (the campaign vector space).
        """
        from qdrant_client import models  # lazy

        client = self._get_client()
        points: list = []
        offset = None
        while True:
            batch, offset = await client.scroll(
                collection_name=self._settings.voiceprint_collection,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="campaign_id",
                            match=models.MatchValue(value=str(campaign_id)),
                        )
                    ]
                ),
                limit=limit,
                offset=offset,
                with_vectors=True,
                with_payload=True,
            )
            points.extend(batch)
            if offset is None or not batch:
                break
        return points

    async def upsert(self, point_id: str, vector: list[float], payload: dict) -> None:
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
        logger.info("upserted voiceprint point %s", point_id)

    async def delete(self, point_id: str) -> None:
        """Remove a voiceprint point (re-enrollment / profile deletion)."""
        if not self._ensured:
            return  # nothing was ever written; nothing to remove
        client = self._get_client()
        await client.delete(
            collection_name=self._settings.voiceprint_collection,
            points_selector=[point_id],
        )
