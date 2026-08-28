"""Notification delivery worker.

Consumes ``notification.events`` and fans out to in-app inbox + channels.

Implementation sketch:

1. Map event types to recipients:
   - ``wiki.draft_ready`` / ``speaker.pending`` → the campaign's DM.
   - ``session.published`` / ``wiki.published`` → campaign members (players).
   (Membership lookups via campaign-service internal API.)
2. Persist a ``notifications`` row (type, payload, user_id, campaign_id).
3. Dispatch through channels (in-app always; email/webhook/push behind a
   pluggable ``Channel`` interface — stubbed for v1).
"""

import asyncio
import logging

from dnd_common.events import Event, connect_rabbitmq, consume

from app.core.config import get_settings

logger = logging.getLogger(__name__)


async def handle(event: Event) -> None:
    logger.info("Delivering %s", event.type)
    # TODO: implement the pipeline above.
    raise NotImplementedError("notification delivery not implemented yet")


async def main() -> None:
    settings = get_settings()
    connection = await connect_rabbitmq(settings.rabbitmq_url)
    await consume(connection, "notification.events", handle)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
