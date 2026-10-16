# Agent instructions

This repository was worked on with an AI coding agent. The full prompt it was given is
[PROMPT.md](./PROMPT.md) (also loaded as `.github/copilot-instructions.md`); the assignment is
[BRIEF.md](./BRIEF.md). In short:

- **Test first, never squash.** Every fix is two commits: a `test:` commit whose test fails
  on the current code, then the `fix:` commit that makes it pass.
- **Keep the probe's contract.** `/admin/*` and `/__gateway/*` stay functional (no login or
  rate limit on the gateway); URL routes, form field names and session cookies do not change;
  the whole system is served on port 8000.
- **Protect the data.** The ~100,000 seeded citizens are never deleted or rewritten, except to
  heal PENDING applications past the 15-day RTPS deadline.
- **No secrets in git.** Credentials come from the untracked `.env` (`scripts/gen-env.sh`).
- **Run the tests:** `pip install -r requirements.txt`, then `pytest` (needs the seeded
  Postgres; see README). `tests/test_probe_simulation.py` needs the stack running:
  `docker compose up -d --build`.

The five fixes, in order of citizen impact: (1) RTPS deemed approval restored and the backlog
healed, (2) Indian-script names stored whole and printed in the PDF, (3) citizen data
locked down (IDOR, unauthenticated PDFs, staff actions, SQL injection, rotated secrets),
(4) month-end load (connection pool, indexes, cached PDFs), (5) form traps (DD/MM/YYYY,
widows, 5 MB uploads, clear errors, edit/withdraw).
