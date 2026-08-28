# Keycloak realm provisioning

The realm `dnd` is imported automatically on first boot from
`realm-export/dnd-realm.json` (mounted at `/opt/keycloak/data/import`).

It defines:

- Realm roles **`dm`** and **`player`** — the entire permission model maps onto
  these two roles plus campaign membership.
- Client **`dnd-web`** (public) — browser login/refresh for the Next.js app.
- Client **`dnd-mobile`** (public) — login + `dndwiki://callback` for the Expo app.
- Client **`dnd-services`** (confidential, service account) — internal
  service-to-service calls; its secret comes from `KEYCLOAK_SERVICE_CLIENT_SECRET`
  (must match the `secret` in the realm export for dev).

## First run

```bash
docker compose up -d keycloak
# console: http://localhost:8080  admin / admin (see .env)
```

Create a test user in the `dnd` realm and assign it the `player` role, then log in
from the web app. If you change the realm file, restart keycloak with a fresh
volume (`docker compose down -v && docker compose up -d keycloak`).

> **Production:** export the realm (`kc.sh export`) and manage it via Terraform /
> Helm values instead of hand-maintained JSON.
