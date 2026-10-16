# Sewa Setu — Old Age Pension Portal

Handover copy. Vendor maintenance contract ended 31/01/2026.

## Running

Secrets are not stored in the repository. Create a private `.env` once, then start:

```
sh scripts/gen-env.sh        # random POSTGRES_PASSWORD, SECRET_KEY, ADMIN_PASSWORD -> .env
docker compose up --build
```

If the `pgdata` volume was created earlier with a different database password, either
remove it (`docker compose down -v`, which reloads the seed) or change the password in
place: `docker compose exec db psql -U sewasetu -c "ALTER USER sewasetu PASSWORD '...'"`.

Tests: `pip install -r requirements.txt` then
`SEWASETU_DB_HOST=127.0.0.1 SEWASETU_DB_PORT=5432 SEWASETU_DB_PASSWORD=... pytest`.

- Portal: http://localhost:8000
- SMS gateway console (simulated telecom): http://localhost:8000/__gateway/ —
  "sent" OTPs appear here once delivered. The gateway runs on the internal
  network and is served through the portal, so only one port is exposed.
- Database: Postgres, loaded from `seed/seed.sql` on first boot

Production server logs (Jan–Jun 2026) are provided in `logs/`.

Listen to `dc-briefing.mp3` first (the DC's briefing), then read `BRIEF.md`.
