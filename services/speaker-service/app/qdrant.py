"""Qdrant access for voiceprints (collection 'voiceprints', cosine, 192-d).

Owned by user-service: enrollment upserts embeddings here; speaker-service
queries them for matching and adds session-derived voiceprints when the DM
names a speaker. qdrant-client is imported lazily so the worker (and its
tests) run without it installed.

Every store calls ensure_collection() before it touches Qdrant - READS
included. A read that skipped it 404'd on a collection nothing had written to
yet, which is the normal state of a campaign that has never enrolled anyone:
the error was caught and logged, so nothing broke, but the log said "search
failed" for what is really "there is nothing to search yet".
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

        await self.ensure_collection()
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

        await self.ensure_collection()
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

class VoiceObservationStore:
    """Per-observation voice vectors (collection 'voice_observations').

    One point per speaker turn, keyed by the deterministic id of
    app.observations.observation_point_id so re-identifying a session
    overwrites rather than duplicates. The per-turn vectors of one identity are
    the evidence behind its purity estimate (attribution-model S5.1), and the
    payload carries the observation link the engine needs to build the
    same_voice edges (S4.3).
    """

    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings
        self._client = None
        self._ensured = False

    @property
    def collection(self) -> str:
        return self._settings.voice_observation_collection

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
        if not await client.collection_exists(self.collection):
            await client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(
                    size=self._settings.embedding_dim,
                    distance=models.Distance.COSINE,
                ),
            )
            logger.info("created Qdrant collection %s", self.collection)
        self._ensured = True

    async def upsert_many(self, points: list[tuple[str, list[float], dict]]) -> None:
        """Upsert (point_id, vector, payload) triples in one request."""
        if not points:
            return
        from qdrant_client import models  # lazy

        client = self._get_client()
        await self.ensure_collection()
        await client.upsert(
            collection_name=self.collection,
            points=[
                models.PointStruct(id=point_id, vector=vector, payload=payload)
                for point_id, vector, payload in points
            ],
        )
        logger.info("upserted %d voice observation(s) into %s", len(points), self.collection)

    async def list_session(self, session_id: str, limit: int = 4096) -> list:
        """Every observation vector of one session (vectors + payloads)."""
        from qdrant_client import models  # lazy

        await self.ensure_collection()
        client = self._get_client()
        points: list = []
        offset = None
        while True:
            batch, offset = await client.scroll(
                collection_name=self.collection,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="session_id", match=models.MatchValue(value=str(session_id))
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

    async def delete_session(self, session_id: str) -> None:
        """Drop a session's observations (session delete / re-identify)."""
        if not self._ensured:
            return
        from qdrant_client import models  # lazy

        client = self._get_client()
        await client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="session_id", match=models.MatchValue(value=str(session_id))
                        )
                    ]
                )
            ),
        )


class MemberVoiceModelStore:
    """Per-centroid member voice models (collection 'member_voice_models').

    The direct fix for 'one player, many clusters' (attribution-model S12.2):
    every print is kept as its own centroid instead of being averaged into a
    single member vector, and the engine scores a member by their best (or
    top-k mean) centroid. Keyed on member_id, not user_id, so a member without
    a linked account can finally be learned (S12.3 defect 4).
    """

    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings
        self._client = None
        self._ensured = False

    @property
    def collection(self) -> str:
        return self._settings.member_voice_model_collection

    def _get_client(self):
        if self._client is None:
            from qdrant_client import AsyncQdrantClient  # lazy: heavy/optional dep

            self._client = AsyncQdrantClient(url=self._settings.qdrant_url)
        return self._client

    async def ensure_collection(self) -> None:
        if self._ensured:
            return
        from qdrant_client import models  # lazy

        client = self._get_client()
        if not await client.collection_exists(self.collection):
            await client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(
                    size=self._settings.embedding_dim,
                    distance=models.Distance.COSINE,
                ),
            )
            logger.info("created Qdrant collection %s", self.collection)
        self._ensured = True

    async def upsert(self, point_id: str, vector: list[float], payload: dict) -> None:
        from qdrant_client import models  # lazy

        client = self._get_client()
        await self.ensure_collection()
        await client.upsert(
            collection_name=self.collection,
            points=[models.PointStruct(id=point_id, vector=vector, payload=payload)],
        )

    async def search(self, embedding: list[float], campaign_id: str, limit: int = 8) -> list:
        """Best centroids for a campaign, best cosine first.

        More than one hit on purpose: a member owns several centroids, and the
        engine scores them with top-k aggregation instead of trusting the single
        best print (attribution-model S12.2).
        """
        from qdrant_client import models  # lazy

        await self.ensure_collection()
        client = self._get_client()
        response = await client.query_points(
            collection_name=self.collection,
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

    async def list_campaign(self, campaign_id: str, limit: int = 4096) -> list:
        from qdrant_client import models  # lazy

        await self.ensure_collection()
        client = self._get_client()
        points: list = []
        offset = None
        while True:
            batch, offset = await client.scroll(
                collection_name=self.collection,
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

    async def delete_session(self, session_id: str) -> None:
        if not self._ensured:
            return
        from qdrant_client import models  # lazy

        client = self._get_client()
        await client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="session_id", match=models.MatchValue(value=str(session_id))
                        )
                    ]
                )
            ),
        )

