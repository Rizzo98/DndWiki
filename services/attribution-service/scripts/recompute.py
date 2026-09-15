"""Re-run attribution for one session (operator tool).

The engine is driven by events, so a recompute is a published message - this
script is just a way to send it by hand. Useful when a session was attributed
before the campaign had voiceprints, or after the DM answered questions that
made it worth asking again.

    docker compose cp services/attribution-service/scripts/recompute.py \
        attribution-service:/tmp/recompute.py
    docker compose exec -T -w /app/service attribution-service \
        python /tmp/recompute.py <session-id> <campaign-id>

Both ids are the UUIDs from the session's URL and its campaign. The worker
picks the message off 'attribution.jobs' and moves the session to 'attributing';
watch it with 'docker compose logs -f attribution-service'.
"""

import asyncio
import sys

from dnd_common.events import Event, connect_rabbitmq, publish

DEFAULT_URL = "amqp://dnd:dnd@rabbitmq:5672/%2f"


async def main(session_id: str, campaign_id: str, url: str = DEFAULT_URL) -> None:
    connection = await connect_rabbitmq(url)
    await publish(
        connection,
        Event(
            type="attribution.recompute",
            payload={"session_id": session_id, "campaign_id": campaign_id},
        ),
    )
    await connection.close()
    print(f"published attribution.recompute for {session_id}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("usage: recompute.py <session-id> <campaign-id>")
    asyncio.run(main(sys.argv[1], sys.argv[2]))
