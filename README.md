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

### Accessibility for elderly and low-literacy citizens

- Every form field has a **Listen** button that reads the instruction aloud (English, Hindi
  or Assamese, chosen in the bar at the top) using the browser's built-in speech synthesis.
  Nothing is recorded. If the device has no voice for the chosen language, it reads in
  English and says so. Assamese is spoken only where the device offers an Assamese or Bengali voice.
- The bank account number is shown in groups of four, like the passbook. The spaces are
  removed before posting, and the server ignores them as well.
- Typing an IFSC code shows the bank and branch. `GET /api/ifsc/<code>` answers from a bundled
  bank-code table, and adds the branch from a public IFSC directory (`IFSC_LOOKUP_URL`, set it
  empty to stay fully offline). Only the public IFSC code leaves the server; if the directory
  is down the bank name still appears and the form is never blocked.
- Inputs and buttons are at least 56 px tall, text meets WCAG AAA contrast (7:1), and every
  field has a visible label and hint instead of placeholder text.

Production server logs (Jan–Jun 2026) are provided in `logs/`.

Listen to `dc-briefing.mp3` first (the DC's briefing), then read `BRIEF.md`.
