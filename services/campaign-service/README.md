# campaign-service

Campaigns and membership. This is where the **DM vs player** permission split
starts: the campaign creator becomes `dm` (recorded in
`campaigns.dm_user_id` and mirrored by exactly one `campaign_members` row
with role `dm`); everyone else joins as `player`.

## Responsibilities

- **Campaign CRUD** — create (any authenticated user; creator becomes DM),
  update, archive/restore (DM only). Campaigns are never hard-deleted:
  `status` moves `active` <-> `archived`.
- **Membership management** — a member is a player at the table (player name
  + character name), optionally linked to an existing platform user. The DM
  adds players, edits them (rename / link / unlink the account) and removes
  them; players see the roster. Exactly one DM per campaign (transfer/co-DM
  is a TODO).
- **Invites** — DM creates one-time email invites (token + expiry); the
  invited user redeems the token at `POST /api/campaigns/invites/{token}/accept`
  and becomes a member. Accepting an email-pinned invite requires the caller's
  JWT `email` claim to match (case-insensitive).
- **Membership checks for other services** — `GET /internal/membership`
  (dnd-services client token): session/wiki services ask campaign-service for
  a user's role instead of reading `dnd_campaigns` tables.

## Public API (JWT: any authenticated user; DM actions noted)

| Method | Path | Notes |
|---|---|---|
| POST | `/api/campaigns` | create; caller becomes DM (slug auto-derived if omitted) |
| GET | `/api/campaigns` | campaigns the caller is a member of (+ `my_role`) |
| GET | `/api/campaigns/{id}` | member-only detail |
| PATCH | `/api/campaigns/{id}` | DM: rename / slug / description / settings |
| POST | `/api/campaigns/{id}/archive` | DM: `status = archived` |
| POST | `/api/campaigns/{id}/restore` | DM: `status = active` |
| GET | `/api/campaigns/{id}/members` | roster (member-only) |
| POST | `/api/campaigns/{id}/members` | DM: add player `{player_name, character_name, user_id?}` (user link optional) |
| PATCH | `/api/campaigns/{id}/members/{member_id}` | DM: rename / link `{user_id}` / unlink `{unlink_user}` |
| DELETE | `/api/campaigns/{id}/members/{member_id}` | DM: remove player (DM itself cannot be removed) |
| GET | `/api/campaigns/{id}/invites` | DM: list invites (tokens included) |
| POST | `/api/campaigns/{id}/invites` | DM: invite by email `{email, role?}` (v1: player only) |
| DELETE | `/api/campaigns/{id}/invites/{invite_id}` | DM: revoke invite |
| POST | `/api/campaigns/invites/{token}/accept` | redeem a join token |

## Internal API (dnd-services client token)

| Method | Path | Purpose |
|---|---|---|
| GET | `/internal/membership?campaign_id=&user_id=` | role (`"dm"`/`"player"`) or `null`; 404 if the campaign does not exist |
| GET | `/internal/members/{member_id}?campaign_id=` | member row (id, user link, player/character names); 404 if not in the campaign — used by session-service to validate/named speaker assignments |

Consumed by session-service's `CampaignServiceClient` (see
`services/session-service/app/clients/campaigns.py`): 404 and `role: null`
both mean "not a member".

## Invariants

- Exactly one member row carries role `dm`, and it is the user in
  `campaigns.dm_user_id` (set at creation; its user link cannot be changed).
  Removing/demoting the last DM is rejected with 400.
- A member is identified by its surrogate `id`; `user_id` is an optional link
  to a platform account, unique per campaign (a user links at most once).
  Unlinked members need no account — the DM provides player and character
  names, which are also what invite-joiners get until the DM fills them in.
- Invites are single-use (`used_at` set on accept), expire after
  `INVITE_TTL_DAYS` (default 7), and are revoked by hard delete.
- Slugs are unique across all campaigns (`unique_slug` appends `-2`, `-3`
  on collision).
- Campaigns are never hard-deleted; `archived` + `restore` cover removal.

## Owns

- PostgreSQL database `dnd_campaigns` (campaigns, campaign_members,
  invites) — schema managed by Alembic (`alembic upgrade head`).

## Local dev

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -e libs/python/dnd_common -e "services/campaign-service[dev]"
cd services/campaign-service
uv run alembic upgrade head          # against docker-compose Postgres
uv run uvicorn app.main:app --reload --port 8002
```

Tests and lint:

```bash
uv run pytest                        # unit + API tests (SQLite, auth faked)
uv run ruff check .
```

## Layout

```
app/
  api/           public + internal routers (campaigns, membership)
  services/      business logic (invariants, invites, membership)
  models.py      SQLAlchemy models (campaigns, campaign_members, invites)
  schemas.py     Pydantic request/response models
  deps.py        service-token auth (dnd-services azp check)
migrations/      Alembic (initial: 0001)
tests/           pytest suite
```

## TODO

- [ ] DM transfer / co-DM (invites and member adds currently allow `player` only)
- [ ] Invite email delivery via notification-service (publish a
      `campaign.invite_created` event once the contract is added to
      docs/event-contracts.md)
- [ ] Pagination + search for campaign lists
- [ ] Settings validation (schema for `campaigns.settings` jsonb)
