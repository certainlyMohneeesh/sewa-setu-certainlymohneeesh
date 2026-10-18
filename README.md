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

### Demo: one citizen, end to end

With the stack running, `scripts/demo_flow.py` walks one citizen through the whole
journey using only the standard library:

```
python3 scripts/demo_flow.py            # citizen journey: apply, OTP, forms, upload, slip, status
python3 scripts/demo_flow.py --approve  # also log in as staff and approve the application
```

It reads the OTP from the SMS gateway console, the same way a citizen would read their
phone. Each run creates real application records, so use it on a local stack only.
The demo mobile numbers start with `3`, which the probe cleanup does not touch. Options:
`--mobile`, `--name`, `--dob` (DD/MM/YYYY), `--document` (PDF, JPG or PNG). The acknowledgment
slip is saved to the temp directory. Admin credentials come from `.env`.

Production server logs (Jan–Jun 2026) are provided in `logs/`.

Listen to `dc-briefing.mp3` first (the DC's briefing), then read `BRIEF.md`.
