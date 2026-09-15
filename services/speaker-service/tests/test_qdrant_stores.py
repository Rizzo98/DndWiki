"""The Qdrant stores: a READ must not 404 on an empty campaign.

Every store creates its collection on first use. The write paths always did;
the read paths did not, so the first read of a campaign that had never
enrolled anyone asked Qdrant for a collection that did not exist yet. The
caller caught the exception, so nothing broke - but the log said "search
failed" for a state that is completely normal, which is how a real fault hides.
"""

import asyncio
from types import SimpleNamespace

from app.qdrant import MemberVoiceModelStore, VoiceObservationStore, VoiceprintStore


class _Client:
    """Enough of AsyncQdrantClient for the read paths."""

    def __init__(self):
        self.queries = 0
        self.scrolls = 0

    async def query_points(self, **kwargs):
        self.queries += 1
        return SimpleNamespace(points=[])

    async def scroll(self, **kwargs):
        self.scrolls += 1
        return [], None


class _Store:
    """Mixin: record ensure_collection() and serve a stub Qdrant."""

    def __init__(self, **settings):
        self._settings = SimpleNamespace(**settings)
        self._client = _Client()
        self._ensured = False
        self.ensures = 0

    async def ensure_collection(self):
        self.ensures += 1
        self._ensured = True

    def _get_client(self):
        return self._client


class Voiceprints(_Store, VoiceprintStore):
    pass


class Observations(_Store, VoiceObservationStore):
    pass


class MemberModels(_Store, MemberVoiceModelStore):
    pass


def test_voiceprint_search_ensures_the_collection():
    store = Voiceprints(voiceprint_collection="voiceprints")
    asyncio.run(store.search([0.1, 0.2], "camp-1"))
    assert store.ensures == 1


def test_voiceprint_list_campaign_ensures_the_collection():
    store = Voiceprints(voiceprint_collection="voiceprints")
    asyncio.run(store.list_campaign("camp-1"))
    assert store.ensures == 1


def test_observation_list_session_ensures_the_collection():
    store = Observations(voice_observation_collection="voice_observations")
    asyncio.run(store.list_session("sess-1"))
    assert store.ensures == 1


def test_member_model_search_ensures_the_collection():
    """This is the one that filled the log with 404s: member_voice_models is
    created on the first ENROLLMENT, which may be sessions away."""
    store = MemberModels(member_voice_model_collection="member_voice_models")
    asyncio.run(store.search([0.1, 0.2], "camp-1"))
    assert store.ensures == 1


def test_member_model_list_campaign_ensures_the_collection():
    store = MemberModels(member_voice_model_collection="member_voice_models")
    asyncio.run(store.list_campaign("camp-1"))
    assert store.ensures == 1
