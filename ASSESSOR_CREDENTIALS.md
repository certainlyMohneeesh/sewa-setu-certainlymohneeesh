# Assessor credentials

Departmental (admin) panel of the Sewa Setu portal: `/admin`

| | |
|---|---|
| Username | `swd-admin` |
| Password | `LCH1BY1OAJZKf4G6qLLr` |

These replace the previous default (`admin` / `sewasetu@123`), which was hard-coded
in the source and no longer works. The session-signing key was rotated at the same
time, so any session cookie signed with the old key is rejected.

- Admin pages: `/admin/dashboard`, `/admin/applications`, `/admin/application/<id>`,
  `/admin/approve/<id>`, `/admin/reject/<id>`.
- Decisions made here are recorded in `applications.decided_by` as `swd-admin`.
- The SMS gateway console, `/__gateway/`, is intentionally public (no login).

**Handling:** this file contains a live password. Keep the repository private and
rotate the password after the assessment: generate a hash with
`python -c "from werkzeug.security import generate_password_hash as g; print(g('NEW'))"`
and put it in `app/config/app.ini` (`[admin] password_hash`), or set the
`ADMIN_PASSWORD` environment variable on the app container. In production, also set
`SECRET_KEY` from the environment instead of relying on the value in `app.ini`.
