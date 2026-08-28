# notification-service

Turns domain events into user notifications: an in-app inbox plus (future)
email / webhook / push channels.

## Responsibilities

- Consumes `notification.events` (`wiki.draft_ready`, `speaker.pending`,
  `session.published`, `wiki.published`).
- Stores a `notifications` row per recipient; `GET /api/notifications` serves
  the in-app inbox with `read_at` tracking.
- Channel dispatch is pluggable: email (SMTP), webhook, FCM/APNs push — all
  behind a small `Channel` abstraction (stubs for v1).

## Owns

- PostgreSQL database `dnd_content` (only the `notifications` table)

## Local dev

```bash
pip install -e libs/python/dnd_common
pip install -e services/notification-service
python -m app.workers.deliver
uvicorn app.main:app --reload --port 8009
```

## TODO

- [ ] Channel abstraction (email via SMTP, webhook, push)
- [ ] Inbox endpoint with pagination
