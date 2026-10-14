# FORENSICS — Sewa Setu Old Age Pension Portal

Phase 0 findings. Nothing in `app/` has been changed; every number below was produced by
read-only queries and can be regenerated with:

```
python3 forensics/analyze_logs.py                      # cron / app / nginx logs
psql -h <host> -U sewasetu -d sewasetu -f forensics/db_checks.sql   # seed database
```

Reference "now" for overdue counts: **2026-10-15** (system date when analysed; the seed is a
`pg_dump` of 2026-09-30).

## How this was run (and one deviation)

* **Docker was not available** on this machine (`docker: command not found`, no root), so
  `docker compose up --build -d` could **not** be run. Instead `schema.sql` + `seed.sql` were
  loaded into a local **PostgreSQL 16.2** (pip `pgserver` wheel; production is `postgres:15`),
  database `sewasetu`, user `sewasetu`, and `app/app.py` was served directly (Flask dev server)
  against it. Result: DB loads cleanly (**100,002** applications, **100,002** portal users,
  **0** otps); `/`, `/about`, `/apply`, `/status`, `/admin`, `/ack/<id>.pdf` all return `200`.
  `/__gateway/` returned `502` only because no `smsgw` process was running in that setup.
  **The real `docker compose up --build` still has to be verified on a Docker host** before
  deployment.
* Reproducibility finding: `app/requirements.txt` pins `Flask==2.2.5` but not Werkzeug, so a fresh
  install gets Werkzeug 3.x. The app still serves requests, but Flask 2.2.5's **test client crashes**
  (`module 'werkzeug' has no attribute '__version__'`). The test harness pins `Werkzeug==2.2.3`
  in the root `requirements.txt`. The production image is untouched for now.
* The log archive is already extracted at `logs/sewasetu-logs-jan-jun-2026/`
  (`sewasetu-app.log` is byte-identical to `sewasetu-app.log.gz`).

## 1. RTPS deemed approval stopped on 2026-03-15

| Evidence | Value |
|---|---|
| `cron.log` successful runs | **73** (2026-01-01 → **2026-03-14 02:00:02**), 2,248 applications auto-approved in that window |
| `cron.log` failures | **108** tracebacks (2026-03-15 → 2026-06-30, one per night), **all** `FileNotFoundError: '/app/config/settings.ini'` at `scripts/deemed_approval.py:24` |
| Cause | `CR-2026-031` merged `settings.ini` into `app.ini`; `app/app.py` was updated, `deemed_approval.py` still opens `config/settings.ini` (line 22) |
| Tracebacks have no timestamps | first-failure date is inferred: 73 successes = 1 Jan..14 Mar exactly, 108 failures = 15 Mar..30 Jun exactly |

Database (`applications`, 100,002 rows):

| status | decided_by | rows |
|---|---|---|
| APPROVED | admin | 79,128 |
| DEEMED_APPROVED | RTPS-AUTO | 9,000 |
| REJECTED | admin | 7,000 |
| PENDING | NULL | **4,874** |

* The status value the system uses is **`DEEMED_APPROVED`** (admin dashboard links to it);
  last such decision in the dump is `2026-02-27 02:00`.
* PENDING rows **older than 15 days as of 2026-10-15: 4,789** (85 are still inside the window).
  By submission month: 2026-02: 2 · 03: 297 · 04: 495 · 05: 642 · 06: 687 · 07: 810 · 08: 886 · 09: 970.
  Of these, **2,121** were submitted Mar–Jun (the log period) and 2,666 Jul–Sep (also never processed).
  Oldest PENDING: `2026-02-28 09:33`. As of the 2026-09-30 dump date: 4,100 overdue.
* The citizen page (`application.html`) prints the raw status string, so these 4,789 citizens
  currently see `PENDING` although the Act deems them approved.
* Caveat: the cron log reports 2,248 approvals in Jan–Mar 14 2026 but the dump's last
  `DEEMED_APPROVED` decision is 2026-02-27; the dump does not contain the 2026-02-28..03-14 runs'
  results. Not investigated further.

## 2. Names: truncated, mojibake, and "?" on the acknowledgment

Code: `sanitize(value, maxlen=20)` (`app.py:68`) slices **20 UTF-8 bytes**; Bengali/Assamese script
is 3 bytes/char, so names are cut to **6–7 characters**, although `applicant_name` is `VARCHAR(100)`.
`generate_acknowledgment()` uses core font Helvetica (Latin-1); non-Latin text raises inside
`pdf.cell`, the bare `except` writes a literal `?`. Reproduced on seed row `98929`: the PDF text
stream contains `Applicant Name` → `?`.

| Check | Rows |
|---|---|
| Non-ASCII names | **300** (all `APPROVED`, 0 `PENDING`) |
| — truncated Bengali/Assamese (6–7 chars; 58 end in a stray space) | **240** (20 × 6 chars, 220 × 7 chars) |
| — **mojibake** (UTF-8 bytes stored as Latin-1, e.g. `à¦ªà¦¦à§…`, 18–19 chars) | **60** — recoverable: `name.encode('latin-1').decode('utf-8')` succeeds for all 60 (e.g. id 99169 → `পদ্মেশ`) |
| Literal `?` / `????` stored in DB | **0** (the `?` exists only in generated PDFs, never in the DB) |
| Latin names with exactly 20 chars (ceiling, e.g. `Padmeswari Choudhury`) | **195** (max name length in the whole table is 20 → longer names were cut) |
| Names with trailing whitespace | 58 |

The 240 truncated names cannot be reconstructed (the original text is gone); the 60 mojibake names
can be repaired but are also truncated. Per the brief ("do not alter existing citizen records except
where a fix requires it") no row was touched.

## 3. Citizen data scraping ("salesmen")

Code facts: `/ack/<int:app_id>.pdf` has **no authentication or ownership check** (`app.py:424`);
verified live: unauthenticated `GET /ack/1.pdf` → `200 application/pdf`. The PDF contains name,
mobile, DOB, village, block, bank account, IFSC. Sequential integer ids make the whole table
harvestable. `/application/<id>` only checks `logged_in`, not ownership (IDOR).

What the **nginx log** shows (1,515,987 requests, Jan–Jun 2026):

* `/ack/<id>.pdf`: **20,573** requests, **100 % `200`**, from **20,571 distinct IPs**
  (top: `117.224.234.244` ×2, `103.61.71.248` ×2). It equals the number of successful
  `POST /declaration` (20,573) one-for-one, so the log is consistent with citizens fetching their
  own slip. **No single IP, user-agent or crawl pattern is identifiable**: only **6 user-agents**
  exist in the entire log (3 Android Chrome, iPhone Safari, 2 Windows Chrome), uniformly ~16.7 %
  each (e.g. iPhone Safari 3,475 `/ack` hits, Windows NT 10.0 Chrome 3,460), and there are
  0 consecutive-id pairs in time order.
* There are **0** `/admin/*` requests in the log; no access-log evidence of a scraper.
* The ids requested do not line up with the seed's `submitted_at` (4,434 of 20,573 `/ack`
  requests are for applications whose seed `submitted_at` is *later* than the request), so the log
  ids cannot be tied to specific seed citizens.

**Conclusion:** the log cannot name a scraper; the leak is proven from the code and live behaviour
(any unauthenticated client can download any applicant's PII by id), which matches the DC's
rumour of calls/visits after applying. Do not claim an identified attacker in the Loom.

Other security facts found in the code (all unfixed): hard-coded admin `admin` / `sewasetu@123`
(`app.py:36-37`); `secret_key = ssp-prod-key-2023-donotshare` in `config/app.ini`;
`/admin/approve|reject/<id>` check `session["logged_in"]` (any citizen) instead of
`session["admin"]` and leave `decided_by` NULL; SQL injection via `%` formatting in
`/status/reset` (`app.py:446,449`); upload path built from the raw client filename.

## 4. Month-end slowdown (28th–31st)

Pattern in the logs (all 19 spike days, none other):

| | Normal days (162) | Day ≥ 28 (19 days) |
|---|---|---|
| Requests | 509,570 (≈3,145/day) | 1,006,417 (≈52,969/day, **~17×**) |
| rt p50 / p95 / p99 (s) | 0.056 / 0.290 / 2.654 | 0.055 / **4.253** / **20.962** |
| 5xx | **0** | **13,759** |

* **Only one endpoint fails: `POST /declaration`** — 6,886 × `500` (rt p50 20.4 s) and
  6,873 × `504` (rt exactly 30.0 s = gunicorn `--timeout 30`). Of 22,977 declaration attempts on
  spike days **13,759 (59.9 %) failed**; success p50 rt 17.9 s vs 2.6 s on normal days.
* `sewasetu-app.log`: **13,759** `FATAL: sorry, too many clients already` (= 6,886 + 6,873 exactly)
  and **13,759** `ERROR generating acknowledgment pdf` lines; **100 % occur on days ≥ 28**.
  By day: Jan 28: 867 · 29: 888 · 30: 1,076 · 31: 303 · Feb 28: 344 · Mar 28: 361 · 29: 364 · 30: 1,114 ·
  31: 930 · Apr 28: 864 · 29: 1,078 · 30: 927 · May 28: 1,069 · 29: 774 · 30: 322 · 31: 360 ·
  Jun 28: 365 · 29: 759 · 30: 994.
* Per day 5xx (500 / 504): worst days Jan 30 (521/555), Mar 30 (552/562), May 28 (496/573), Apr 29 (531/547).
* `GET /application/<id>` slows from p50 0.126 s to **4.763 s** (p95 7.678 s) on spike days
  (47,118 requests), no 5xx. `GET /ack/<id>.pdf` stays ≈0.55 s on all days (no failure).
* Spike dates correction: the log shows a spike only on **Feb 28** (24,317 requests); Feb 26–27 are
  normal (3,629 / 3,783). Mar 28–29 are lighter (30k / 27k) than Mar 30–31 (79k / 71k).
* Root causes confirmed in code: `get_db()` opens a new `psycopg2.connect()` per call (no pool, no
  `finally`); `handle_submission()` holds the transaction open while `generate_acknowledgment()`
  runs and draws **4,000** line segments (`app.py:355`); any exception leaks the connection
  until Postgres hits `max_connections` (100). `schema.sql` has **no secondary indexes** (only the
  three primary keys). `smsgw` runs `python app.py` (Flask dev server); **62** `sms gateway error:
  Read timed out` lines in the app log, spread across Jan–Jun (≈ 1 per affected day).
* Not present in the logs: no `WARNING` lines and no `otp expired` messages at all.

## 5. Records: portal logins, truncation, other integrity checks

| Check | Result |
|---|---|
| `applications` rows without a `portal_users` login | **0** |
| `portal_users` rows without an application | **0** |
| `portal_users.password_hash` ≠ sha256(`DDMMYYYY` of stored DOB) | **0** (all 100,002 consistent) |
| Duplicate mobiles / application_no | 0 / 0 (100,002 distinct each) |
| Mobiles not exactly 10 digits | 0 |
| Names containing `?` | **0** |
| Truncated or mojibake names | **300** (240 + 60, see §2) |
| `doc_path` populated | **0** — no seeded application has an uploaded document recorded |
| Widowed with blank `husband_employer` | 40,641 of 40,641 widows (all blank in the dump) |
| Applicants under 60 on `submitted_at` | 3,382 (2,957 within 1 year of 60; only 236 would be ≥ 60 if day/month were swapped, so this is **not** explained by the DD/MM parse bug) — informational, not touched |

`portal_users` is therefore healthy for pre-existing citizens; the DOB/password desync risk is in
**new** submissions only (`handle_submission` parses `%m/%d/%Y` before `%d/%m/%Y`, so e.g. `05/10/1960`
is stored as 10 May 1960 and the password becomes `10051960`).

## 6. Citizen funnel (nginx, whole period)

`POST /apply` 118,564 → `GET /form/1` 97,374 → `/form/2` 89,556 → `/form/3` 82,366 →
`/upload` 75,815 → `GET /declaration` **34,332** → success (`302`) **20,573**.
The biggest step-drop is at the upload step: only **45.3 %** of those who reached it get to the
declaration page (`upload()` accepts only `.pdf` ≤ 102,400 bytes and shows `ERR_VAL_47`, while the
site says "JPG or PDF, maximum 5 MB"). (Access logs show `302` for both failure and success, so this
is an inference from the funnel, not a direct count of rejections.) Also: `/contact`, `/rti`,
`/grievance`, `/downloads/oap-guidelines-2023.pdf`, `/downloads/beneficiary-list.pdf` are requested
~4,800 times each and all return **404** (24,229 total).

## 7. Test harness (this commit)

* `requirements.txt` (root): `-r app/requirements.txt`, `pytest`, `Werkzeug==2.2.3`.
* `pytest.ini`, `tests/conftest.py`, `tests/test_harness.py` (6 passing smoke tests against the
  seeded DB; none of them asserts a bug).
* Run: `SEWASETU_DB_HOST=127.0.0.1 SEWASETU_DB_PORT=5432 pytest`
  (unset vars fall back to `app/config/app.ini`, host `db`).
* Safety: seeded citizens are never modified; tests create citizens with mobiles starting `5`
  (seed mobiles all start 6–9) and delete only those rows on teardown; SMS is captured, not sent;
  uploads go to a temp dir.
* Fixtures: `client`, `db`, `make_citizen`, `submit_application` (full `/apply → /verify →
  /form/1-3 → /upload → /declaration` journey), `otp_for`, `sms_outbox`, `mobile_factory`.
