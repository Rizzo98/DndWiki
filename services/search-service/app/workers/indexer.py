"""Meilisearch indexer worker.

Consumes ``search.events`` (wiki.published / wiki.updated / wiki.archived) and
keeps the ``wiki_pages`` index in sync.

Implementation sketch:

1. On ``wiki.published`` / ``wiki.updated``: fetch the page from wiki-service
   (internal API, dnd-services token) and index a search document:

   .. code-block:: python

       from meilisearch import Client

       client = Client(cfg.meili_url, api_key=cfg.meili_master_key)
       client.index(cfg.meili_index).add_documents([{
           "id": page_id, "campaign_id": campaign_id,
           "kind": kind, "title": title, "slug": slug,
           "summary": summary, "updated_at": updated_at,
       }])

2. On ``wiki.archived`` or visibility -> dm_only/hidden: delete the document
   (``client.index(...).delete_document(page_id)``) so players never search
   content they cannot read.

Index settings (once): ``searchableAttributes = [title, summary, content]``,
``filterableAttributes = [campaign_id, kind]``.
"""

import asyncio
import logging

from dnd_common.events import Event, connect_rabbitmq, consume

from app.core.config import get_settings

logger = logging.getLogger(__name__)


async def handle(event: Event) -> None:
    payload = event.payload
    logger.info("Index event %s for page %s", event.type, payload.get("page_id"))
    # TODO: implement the pipeline above.
    raise NotImplementedError("indexer not implemented yet")


async def main() -> None:
    settings = get_settings()
    connection = await connect_rabbitmq(settings.rabbitmq_url)
    await consume(connection, "search.events", handle)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
