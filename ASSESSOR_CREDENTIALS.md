# Assessor credentials

Departmental (admin) panel of the Sewa Setu portal: `/admin`

| | |
|---|---|
| Username | `swd-admin` |
| Password | supplied with the submission (application form); not stored in this repository |

The vendor's original default login no longer works, and the session-signing key
has been rotated, so cookies signed with the old key are rejected.

- Admin pages: `/admin/dashboard`, `/admin/applications`, `/admin/application/<id>`,
  `/admin/approve/<id>`, `/admin/reject/<id>`.
- Decisions made here are recorded in `applications.decided_by` as the username above.
- The SMS gateway console, `/__gateway/`, is intentionally public (no login).

## Where the password lives

Nothing secret is committed. The deployment reads its secrets from environment
variables, which `docker compose` takes from an untracked `.env` file:

```
sh scripts/gen-env.sh      # creates .env with random POSTGRES_PASSWORD, SECRET_KEY, ADMIN_PASSWORD
docker compose up --build
```

The admin password is the `ADMIN_PASSWORD` line of that `.env` (or set
`ADMIN_PASSWORD_HASH` to a Werkzeug hash instead:
`python -c "from werkzeug.security import generate_password_hash as g; print(g('NEW'))"`).
To rotate it, change the value and recreate the app container.
