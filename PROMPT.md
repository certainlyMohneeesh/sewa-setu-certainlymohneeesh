# ROLE & MISSION: Sewa Setu Old Age Pension Portal — Full Investigation, Test-Driven Fixes & Bun/Next.js + Flask Upgrade

You are a Senior Public-Interest Systems Engineer working on the **Build for Bharat Fellowship 2027 Engineering Take-Home** (`Sewa Setu — Old Age Pension Portal`, Department of Social Welfare, Government of Purvanchal).

Your mission is to:
1. Perform forensic verification on the production logs (`logs/`) and database dump (`seed/seed.sql`).
2. Fix all critical bugs impacting citizens in the inherited Flask + PostgreSQL + Cron + SMS Gateway system.
3. Upgrade the citizen experience using a **Bun + Next.js** frontend paired with the **Flask** backend, while maintaining **100% backward compatibility** with the automated grading probe's HTTP form submissions, session cookies, URL routes, admin panel (`/admin/*`), and SMS gateway proxy (`/__gateway/*`).
4. Strictly follow the **Test-First Git Commit Protocol** required by the automated repository grader: for each fix, commit a failing automated test FIRST, and then commit the fix that makes the test pass (NEVER squash commits).

---

## CRITICAL ASSESSMENT RULES (NON-NEGOTIABLE)

1. **Test-First Git History (Unsquashed):**
   - Every fix MUST consist of at least two sequential commits:
     - Commit A (`test: ...`): Adds a test that reproduces the bug and FAILS against the current code.
     - Commit B (`fix: ...`): Implements the fix so that Commit A's test PASSES.
   - Do NOT squash commits. Keep the full git history clean and linear.
2. **Preserve Existing Citizen Data:**
   - `seed/seed.sql` contains ~100,000 production citizen records.
   - Do NOT delete or alter existing citizen demographic records, EXCEPT where a fix requires healing corrupted/stuck state (specifically: healing overdue `PENDING` applications past the 15-day RTPS statutory window, and ensuring `portal_users` login hashes match citizen DOBs if any are missing/desynced).
3. **Keep `/admin/*` and `/__gateway/*` 100% Functional:**
   - The automated probe uses `/__gateway/` (including `/__gateway/api/messages?to=<mobile>`) to read OTPs and SMS notifications during test runs. **NEVER add authentication or rate-limiting to `/__gateway/*`**.
   - Keep all `/admin/*` routes (`/admin`, `/admin/dashboard`, `/admin/applications`, `/admin/application/<id>`, `/admin/approve/<id>`, `/admin/reject/<id>`) functional and HTML-compatible because the automated assessor uses the admin panel to verify fixes.
4. **Single Exposed Port (`8000`):**
   - `docker compose up --build` must bring up the entire system with port `8000` exposed on the host, serving the citizen UI, Flask backend routes, `/admin/*`, `/ack/<id>.pdf`, and `/__gateway/*`.
5. **Commit AI Configuration & Prompts:**
   - Save this prompt and your agent instructions in `CLAUDE.md` and commit it to the repository as required by `BRIEF.md`.

---

## PHASE 0: INITIAL FORENSIC VERIFICATION (RUN THESE COMMANDS FIRST)

Before editing any application code, inspect the local archive files in `logs/` and `seed/seed.sql` to confirm all data patterns:

1. **Extract and inspect `logs/`:**
   - Check `logs/cron.log`: Confirm that `deemed_approval.py` succeeded until `2026-03-14 02:00:02` and failed every night from `2026-03-15` onward with `FileNotFoundError: '/app/config/settings.ini'`.
   - Inspect `logs/nginx-access.log.gz` (`gzip -dc logs/nginx-access.log.gz | head -n 200`):
     - Identify the exact requests, IP addresses, and User-Agents scraping citizen data right after application submission (check requests to `/ack/<id>.pdf`, `/application/<id>`, `/admin/*`, etc.).
     - Identify the exact traffic pattern during month-end spikes (Jan 28–31, Feb 26–28, Mar 28–31, Apr 28–30, May 28–31, Jun 28–30) and check `rt=` response times and status codes (`500`, `502`, `504`).
   - Inspect `logs/sewasetu-app.log.gz`:
     - Check all `ERROR` and `WARNING` lines (`FATAL: sorry, too many clients already`, `otp expired`, `sms gateway error`).
2. **Inspect `seed/seed.sql`:**
   - Check the distribution of `status` and `decided_by` in `applications` (e.g., how many rows are `APPROVED`, `DEEMED_APPROVED`, `REJECTED`, `PENDING`, and how many `PENDING` rows have `submitted_at` older than 15 days).
   - Check if any existing rows in `applications` have `????` or truncated Assamese/Unicode names, or if `portal_users` is missing entries for any mobile numbers in `applications`.

Document these exact numbers and findings in `FORENSICS.md` so we can reference them in our 3-minute Loom video to the Deputy Commissioner.

---

## PHASE 1: THE 5 CORE FIXES (RANKED BY CITIZEN IMPACT) & ALL EDGE CASES

Implement the following 5 fixes in strict priority order. For EACH fix, first commit a failing `pytest` test in `tests/`, verify it fails, and then commit the implementation and verify it passes.

---

### FIX #1: Restore Statutory RTPS 15-Day Deemed Approval & Heal Overdue Applications
**Root Causes:**
1. In March 2026 (`CR-2026-031`), `config/settings.ini` was consolidated into `config/app.ini`. `app/app.py` was updated to read `config/app.ini`, but `app/scripts/deemed_approval.py` still tries to open `config/settings.ini`, crashing with `FileNotFoundError` every night at `02:00` since March 15, 2026.
2. Under the Purvanchal Right to Public Services (RTPS) Act, any application left `PENDING` for more than 15 days (`sla_days = 15`) is legally deemed approved, and **"an untouched file must actually be shown as approved to the citizen."**
3. Hundreds/thousands of pre-existing citizen applications submitted between March and June 2026 in `seed/seed.sql` are still stuck in `PENDING` status past the 15-day SLA.

**Exact Implementation Requirements:**
1. **Fix `deemed_approval.py`:**
   - Update `CONFIG_PATH` in `deemed_approval.py` to load `os.path.join(BASE_DIR, "config", "app.ini")` (with fallback to `settings.ini` if ever present).
   - Use IST timezone (`Asia/Kolkata`, UTC+05:30) for `datetime.now()` and cutoff calculation (`cutoff = now_ist() - timedelta(days=SLA_DAYS)`).
   - Check what status value `seed.sql` and `admin_dashboard.html` expect:
     - `admin_dashboard.html` links to `/admin/applications?status=DEEMED_APPROVED` and `/admin/applications?status=APPROVED`.
     - However, the DC explicitly stated: *"The deadline for the department is 15 days, after which deemed approval kicks in, and an untouched file must actually be shown as approved to the citizen."*
     - Therefore:
       - Check `seed.sql` first to see whether pre-March-15 auto-approved rows in `seed.sql` have `status = 'APPROVED'` or `status = 'DEEMED_APPROVED'`.
       - Ensure that when a citizen views `/application/<id>` or downloads `/ack/<id>.pdf`, any deemed-approved application clearly displays `APPROVED` (e.g., if `status` is `'DEEMED_APPROVED'`, display `APPROVED (Deemed Approved under RTPS Act)` on the citizen portal and acknowledgment slip, AND in `/admin/applications?status=APPROVED` include both `APPROVED` and `DEEMED_APPROVED` or keep `/admin/applications?status=DEEMED_APPROVED` working so both admin filters work).
       - Furthermore, if a citizen logs in and views an untouched application whose `status == 'PENDING'` and `submitted_at < now_ist() - timedelta(days=SLA_DAYS)` (e.g., before the nightly 02:00 cron has run), auto-transition it immediately on read/startup so an untouched file past 15 days is NEVER shown as `PENDING` to a citizen.
2. **Heal Pre-Existing Overdue Records in `seed.sql`:**
   - Add an idempotent startup migration function `heal_overdue_applications()` in `app.py` (and a standalone migration script) that updates all existing `PENDING` rows in `applications` where `submitted_at < now_ist() - timedelta(days=SLA_DAYS)`, setting their status, `decided_at = submitted_at + timedelta(days=SLA_DAYS)` (or `now_ist()`), and `decided_by = 'RTPS-AUTO'`.
   - **CRITICAL:** During the bulk startup healing of thousands of historical rows from `seed.sql`, do NOT send 5,000 synchronous HTTP requests to `smsgw` (which would block startup for minutes and flood the SMS gateway). Only send live SMS notifications in `deemed_approval.py` for newly transitioned applications (or batch/skip SMS for historical pre-seed records).

**Commit Pair 1:**
- `test: add failing test for RTPS 15-day deemed approval script and overdue citizen status`
- `fix: point deemed_approval.py to app.ini and auto-heal overdue RTPS applications`

---

### FIX #2: Fix Assamese & Indian-Script Name Truncation & `????` Acknowledgment PDFs
**Root Causes:**
1. **Byte-Slicing Truncation in `sanitize()`:**
   - In `app.py`, `sanitize(value, maxlen=20)` does:
     ```python
     encoded = value.encode("utf-8")[:maxlen]
     return encoded.decode("utf-8", errors="ignore")
     ```
   - Assamese, Bengali, Devanagari, and other Indian scripts use **3 bytes per character** in UTF-8. Slicing at 20 bytes truncates every Assamese name to at most **6 characters** (cutting names in half!), even though `schema.sql` defines `applicant_name VARCHAR(100)`.
2. **Latin-1 Font in `generate_acknowledgment()`:**
   - `generate_acknowledgment()` uses `FPDF()` with `"Helvetica"`, which only supports Latin-1 (`cp1252`).
   - When `pdf.cell(0, 8, str(val), border=1, ln=1)` encounters Assamese/Unicode characters, `fpdf2` raises an encoding exception and hits the `except Exception:` block, which prints `"?"` (or `????`) instead of the citizen's name!

**Exact Implementation Requirements:**
1. **Fix `sanitize()` in `app.py`:**
   - Normalize Unicode with `unicodedata.normalize("NFC", value.strip())`.
   - Slice by **character length** (`maxlen=100` to match `VARCHAR(100)` in `schema.sql`), never by UTF-8 byte length:
     ```python
     def sanitize(value, maxlen=100):
         if value is None:
             return ""
         value = unicodedata.normalize("NFC", str(value).strip())
         return value[:maxlen]
     ```
   - Also apply `sanitize(..., maxlen=100)` consistently to `village`, `husband_name`, and `husband_employer`.
2. **Fix Unicode PDF Generation in `generate_acknowledgment()`:**
   - Bundle/install Google Noto fonts in the Docker image (`fonts-noto-core` / `NotoSansBengali-Regular.ttf` for Assamese/Bengali, `NotoSansDevanagari-Regular.ttf` for Hindi/Devanagari, and `NotoSans-Regular.ttf`) or load them via `pdf.add_font(...)` with `fpdf2` fallback fonts (`pdf.set_fallback_fonts([...])` in `fpdf2`).
   - Verify that `generate_acknowledgment()` renders Assamese (`অসমীয়া`, e.g., `"কমলাকান্ত ভট্টাচাৰ্য"`), Bengali, Hindi, and English names without throwing any exception and without outputting `?`.
   - Ensure the UTF-8 text is embedded in the PDF so automated probes inspecting the PDF or reading `/application/<id>` get the exact Indian-language string back.

**Commit Pair 2:**
- `test: add failing test for Assamese name storage and Unicode PDF generation`
- `fix: preserve full Unicode character length in sanitize and render Indic scripts in PDF`

---

### FIX #3: Lock Down Citizen PII Leaks & Security Vulnerabilities (Stop the Salesmen & Pass Security Probe)
**Root Causes:**
1. **Unauthenticated Acknowledgment PDF Scraper Leak (`/ack/<int:app_id>.pdf`):**
   - `ack_pdf(app_id)` has zero authentication! Salesmen (and attackers) simply iterate `/ack/1.pdf`, `/ack/2.pdf`, ... `/ack/100000.pdf` to harvest every applicant's Name, Mobile Number, DOB, Village, Block, Bank Account, and IFSC the moment they apply.
2. **IDOR (Broken Object-Level Authorization) on `/application/<int:app_id>`:**
   - `view_application(app_id)` only checks `if not session.get("logged_in"):` without checking if the application belongs to `session.get("portal_mobile")` or `session.get("admin")`. Any logged-in citizen can read every other citizen's application by changing `<app_id>` in the URL.
3. **Privilege Escalation (Acting as Staff Without Authority) on `/admin/approve/<id>` & `/admin/reject/<id>`:**
   - Both `admin_approve()` and `admin_reject()` check `if not session.get("logged_in"): abort(403)` instead of `if not session.get("admin"): abort(403)`. Any citizen logged into `/status` has `session["logged_in"] = True` and can approve or reject applications!
4. **SQL Injection in `/status/reset`:**
   - `reset_password()` uses unsafe string formatting:
     `cur.execute("SELECT mobile FROM portal_users WHERE mobile = '%s'" % mobile)`
     `cur.execute("SELECT dob FROM applications WHERE mobile = '%s' LIMIT 1" % mobile)`
5. **Hardcoded / Leaked Credentials & Weak Session Secret:**
   - `ADMIN_USERNAME = "admin"` and `ADMIN_PASSWORD = "sewasetu@123"` are hardcoded in `app.py`.
   - `secret_key = ssp-prod-key-2023-donotshare` is hardcoded in `config/app.ini`, allowing anyone with the repo to forge admin session cookies.
6. **File Upload Path Traversal:**
   - `upload()` constructs the file path using raw `f.filename.lower()` without `werkzeug.utils.secure_filename()`.

**Exact Implementation Requirements:**
1. **Authorize `/application/<int:app_id>` and `/ack/<int:app_id>.pdf`:**
   - In `view_application(app_id)`: Allow access ONLY if `session.get("admin")` is True OR (`session.get("logged_in")` is True AND `row.mobile == session.get("portal_mobile")`). If logged in as a different citizen, return `abort(403)`. If not logged in at all, redirect to `status_login` (or `401`/`403`).
   - In `ack_pdf(app_id)`: Allow access ONLY if:
     - `session.get("admin")` is True, OR
     - `session.get("logged_in")` is True and the application's `mobile == session.get("portal_mobile")`, OR
     - `session.get("just_submitted_app_id") == app_id` (Set `session["just_submitted_app_id"] = new_id` in `handle_submission()` so that a citizen who just submitted their application can immediately click "Download Acknowledgment (PDF)" on `confirmation.html` without being blocked!).
     - Otherwise, return `abort(403)`.
2. **Fix Staff Authorization on `/admin/approve/<int:app_id>` and `/admin/reject/<int:app_id>`:**
   - Check `if not session.get("admin"): abort(403)`.
   - Also set `decided_by = session.get("admin_user", "admin")` in the `UPDATE applications` query (currently `decided_by` is left `NULL` when admin approves/rejects!).
3. **Fix SQL Injection in `/status/reset` (and audit all SQL queries):**
   - Replace all `%` string formatting in SQL queries with parameterized queries:
     `cur.execute("SELECT mobile FROM portal_users WHERE mobile = %s", (mobile,))`
     `cur.execute("SELECT dob FROM applications WHERE mobile = %s LIMIT 1", (mobile,))`
4. **Rotate Secret Key & Admin Password:**
   - Load `SECRET_KEY`, `ADMIN_USERNAME`, and `ADMIN_PASSWORD` from environment variables with fallback to updated secure values in `config/app.ini` (rotate `secret_key` to a strong random 64-char hex string and rotate `ADMIN_PASSWORD` from `sewasetu@123` to a strong auditor password, e.g., `Purvanchal@SWD2026!`, and document the new admin credentials clearly in `ASSESSOR_CREDENTIALS.md` for submission).
   - Use `hmac.compare_digest` for password/hash comparisons.
5. **Sanitize Uploaded Filenames:**
   - Use `werkzeug.utils.secure_filename(f.filename)` before saving uploaded documents.

**Commit Pair 3:**
- `test: add failing security tests for IDOR, unauthenticated PDF scraping, staff privilege escalation, and SQLi`
- `fix: enforce ownership and admin auth, parameterize reset SQL queries, and rotate leaked secrets`

---

### FIX #4: Eliminate Month-End Server Slowdowns & DB Connection Exhaustion
**Root Causes:**
1. **Database Connection Leaks (`FATAL: sorry, too many clients already`):**
   - `sewasetu-app.log` shows catastrophic failure starting on the 28th of the month (`(psycopg2.OperationalError) FATAL: sorry, too many clients already`).
   - `get_db()` creates a brand-new `psycopg2.connect()` every time without connection pooling or `try...finally` blocks. If any query or PDF generation throws an error, `conn.close()` is skipped and the connection leaks until PostgreSQL hits `max_connections` (100).
   - `status_login()` even opens two separate connections sequentially in a single request.
2. **Deliberate 4,000-Line CPU/Latency Bottleneck in `generate_acknowledgment()`:**
   - Lines 221–224 of `app.py` execute:
     ```python
     for i in range(0, 2000):
         x = 10 + (i % 190)
         pdf.line(x, 282, x + 0.5, 282)
         pdf.line(x, 12, x + 0.5, 12)
     ```
   - This draws 4,000 tiny 0.5mm overlapping line segments on every PDF generation while holding an open database transaction/connection!
3. **Uncached On-Demand PDFs in `ack_pdf()`:**
   - For the 100,000 seeded applications, `ack/<id>.pdf` does not exist on disk initially. When `ack_pdf()` generates the PDF on the fly, it returns `BytesIO(data)` **without writing the file to `UPLOAD_DIR/ack/<id>.pdf`**, forcing regeneration on every single request.
4. **Zero Indexes on the 100,000-Row `applications` & `otps` Tables (`schema.sql`):**
   - Every lookup by `mobile`, filter by `status`, sort by `submitted_at`, and count by `block` performs a full sequential scan of 100,000 rows.
5. **Single-Threaded `smsgw` & Gunicorn Configuration:**
   - `smsgw/Dockerfile` uses `CMD ["python", "app.py"]` instead of `gunicorn` (or threaded server), causing read timeouts under load.

**Exact Implementation Requirements:**
1. **Implement Thread-Safe Connection Pooling & Context Management in `app.py`:**
   - Use `psycopg2.pool.ThreadedConnectionPool(minconn=2, maxconn=20, ...)` (and configure Postgres `max_connections=200` in `docker-compose.yml`).
   - Wrap DB access in a `@contextmanager def db_cursor():` or `try...finally` that ALWAYS returns the connection to the pool (`pool.putconn(conn)`) even when exceptions occur, and rolls back failed transactions cleanly.
2. **Optimize `generate_acknowledgment()` & Cache PDFs on Disk:**
   - Close/release the DB cursor immediately after fetching `row`—do NOT hold a DB connection open while rendering the PDF!
   - Replace the `for i in range(0, 2000):` loop with two single line calls:
     ```python
     pdf.line(10, 282, 200, 282)
     pdf.line(10, 12, 200, 12)
     ```
   - In `ack_pdf(app_id)`, write the generated PDF bytes to `os.path.join(UPLOAD_DIR, "ack", "%d.pdf" % app_id)` so subsequent downloads are served directly from disk, and invalidate/delete the cached file whenever that application's status or details are updated.
3. **Add Database Indexes (Both in `schema.sql` AND on App Startup for Existing DBs):**
   - Add `CREATE INDEX IF NOT EXISTS` statements in `schema.sql` AND run them idempotently on app startup:
     - `CREATE INDEX IF NOT EXISTS idx_applications_mobile ON applications (mobile);`
     - `CREATE INDEX IF NOT EXISTS idx_applications_app_no ON applications (application_no);`
     - `CREATE INDEX IF NOT EXISTS idx_applications_status_submitted ON applications (status, submitted_at ASC);`
     - `CREATE INDEX IF NOT EXISTS idx_applications_status_block ON applications (status, block);`
     - `CREATE INDEX IF NOT EXISTS idx_otps_mobile_id ON otps (mobile, id DESC);`
4. **Harden `smsgw` and Gunicorn Workers:**
   - Enable `threaded=True` in `smsgw/app.py` (`app.run(host="0.0.0.0", port=8025, threaded=True)`) so concurrent SMS sends and `/api/messages` polls never block each other.
   - Configure Gunicorn in `app/Dockerfile` with appropriate workers and threads (e.g., `--workers 4 --threads 4 --timeout 30`).

**Commit Pair 4:**
- `test: add failing load and concurrency test for PDF generation and DB connection limits`
- `fix: add psycopg2 connection pool, Postgres indexes, and O(1) PDF border rendering with caching`

---

### FIX #5: Fix Citizen Form UX Traps, DOB Password Corruption & Self-Service Application Correction
**Root Causes:**
1. **Trap 1 — `MM/DD/YYYY` Date Parsing Order Corrupting DOBs & Status Portal Passwords:**
   - `form_step1.html` asks for `Date of Birth (DD/MM/YYYY)`.
   - `handle_submission()` parses `for fmt in ("%m/%d/%Y", "%d/%m/%Y"):`.
   - Because US `%m/%d/%Y` comes first, any Indian date where day <= 12 (e.g., `05/10/1960` = 5 Oct 1960) is silently recorded as **10 May 1960**!
   - This swaps the citizen's birth day and month in the database, rejects eligible 60-year-olds born in early months (e.g., `12/01/1966` parsed as Dec 1, 1966 = age 59!), and sets their status portal password (`dob.strftime("%d%m%Y")`) to `10051960` instead of `05101960`, locking them out of `/status`!
   - It also rejects `YYYY-MM-DD` (HTML date input) and `DD-MM-YYYY`.
2. **Trap 2 — Widows/Widowers Rejected for Blank `husband_employer` / `husband_name`:**
   - `handle_submission()` enforces:
     ```python
     if marital == "Widowed":
         if not husband_name or not husband_employer:
             flash("Something went wrong. Please try again.")
             return redirect(url_for("form_step", step=1))
     ```
   - A widow's late husband has no "Current Employer" (and a male widower has no husband). Bazaar middlemen charged fees just because they knew to put dummy text in `husband_employer`!
3. **Trap 3 — Document Upload Says "JPG or PDF, max 5 MB" But Code Rejects JPGs & >100 KB with `ERR_VAL_47`:**
   - `index.html` and `upload.html` state: `Accepted formats: JPG or PDF, maximum size 5 MB`.
   - `upload()` in `app.py` rejects anything not ending in `.pdf` and any file `> 102400` bytes (100 KB), flashing only `"ERR_VAL_47"`.
4. **Trap 4 — Cryptic Error Messages & Late Validation:**
   - Confused users are shown `"ERR_VAL_47"` or `"Something went wrong. Please try again."` instead of clear explanations, and `/form/1..3` do not validate inputs when submitted, waiting until `/declaration` to fail and redirect back to `/form/1`.
   - Remember `BRIEF.md`: *"Citizen journeys — simulates confused and non-ideal users completing the form, and reads the error messages they are shown."*
5. **Trap 5 — 5-Minute Session Timeout, Unchecked CAPTCHA & UTC vs IST Deadline:**
   - `PERMANENT_SESSION_LIFETIME = 300` (5 minutes) expires the session while a senior citizen is filling out the 6-step form.
   - `apply()` generates `session["captcha_answer"]` but never validates the user's `captcha` input.
   - `SCHEME_DEADLINE` (`2026-10-31 23:59`) and age checks use naive container time instead of IST (`Asia/Kolkata`), and the DC stated: *"The application window closes at midnight, India Standard Time."*
6. **Trap 6 — No Review Before Submit & No Way to Fix Mistakes After Submitting (Help Desk Overflow):**
   - `declaration.html` has no summary of the entered data.
   - Once submitted, citizens cannot edit a typo or cancel/withdraw a `PENDING` application to re-apply (`SELECT count(*) FROM applications WHERE mobile = %s` blocks them forever).

**Exact Implementation Requirements:**
1. **Fix Date Parsing & Age Calculation (IST):**
   - Parse DOB trying `"%d/%m/%Y"` FIRST, then `"%d-%m-%Y"`, `"%Y-%m-%d"`, `"%d.%m.%Y"`, and only `"%m/%d/%Y"` as a last resort (when the second number > 12).
   - Calculate `today` in IST (`datetime.now(ZoneInfo("Asia/Kolkata")).date()`).
   - Ensure age `>= 60` (`MIN_AGE = 60`) on the date of application is accepted, and if `age < 60`, flash a clear message: `"Applicant must be at least 60 years of age on the date of application (current age: %d)."`.
   - Also check `seed.sql` on startup: ensure every citizen in `applications` has a corresponding row in `portal_users` with `hash_password(dob.strftime("%d%m%Y"))` so pre-existing citizens logging in during the probe's "The data" check succeed 100%.
2. **Remove the Widow `husband_employer` Trap:**
   - Do NOT require `husband_employer` or `husband_name` when `marital_status == "Widowed"` (keep the fields optional in the database and UI).
3. **Fix Document Upload (`JPG`, `JPEG`, `PNG`, `PDF` up to 5 MB) & Replace `ERR_VAL_47`:**
   - Accept `.pdf`, `.jpg`, `.jpeg`, and `.png` up to `5 * 1024 * 1024` bytes (5 MB).
   - If a file was already uploaded in the current session (`session.get("doc_path")`), allow continuing without re-uploading.
   - Replace `"ERR_VAL_47"` with clear, descriptive error messages:
     - No file selected: `"Please select an age proof document (PDF or JPG) to upload."`
     - Invalid file type: `"Invalid file format. Please upload a PDF or JPG/JPEG image file."`
     - File too large: `"File size exceeds the 5 MB limit. Please upload a file smaller than 5 MB."`
4. **Add Step-by-Step Validation & Clear Human-Readable Error Messages:**
   - **Step 1 (`/apply`):**
     - If mobile is not a 10-digit number: `"Please enter a valid 10-digit mobile number."`
     - If `captcha` is provided in `request.form` and `session.get("captcha_answer")` is set, validate it; if wrong, flash `"Incorrect security check (CAPTCHA) answer. Please try again."` (Note: if an automated probe POSTs to `/apply` without a pre-existing GET session or omits `captcha_answer` in session, handle gracefully or check if `captcha_answer` exists in `session`).
   - **Step 2 (`/form/1`):**
     - Validate `applicant_name` (non-empty), `dob` (valid date and age >= 60 in IST), `gender` (`Male`, `Female`, `Other`), `marital_status` (`Married`, `Unmarried`, `Widowed`). If any fail, stay on `/form/1` (preserving entered `data`!) and flash the exact reason (e.g., `"Please enter your full name."`, `"Please enter a valid Date of Birth in DD/MM/YYYY format."`, `"Applicant must be 60 years of age or above."`, `"Please select your gender and marital status."`).
   - **Step 3 (`/form/2`):**
     - Validate `village` (non-empty) and `block` (must be in `BLOCKS` or non-empty). Flash `"Please enter your village/town and select your block."` if missing.
   - **Step 4 (`/form/3`):**
     - Validate `bank_account` (digits, 9–18 chars, or non-empty numeric) and `ifsc` (non-empty, valid IFSC format or 11 alphanumeric chars). Flash `"Please enter a valid Bank Account Number and IFSC Code."` if missing/invalid.
   - **Fallback in `handle_submission()`:**
     - Keep all validations in `handle_submission()` as well (in case a test or probe posts directly to `/declaration` with pre-populated session data), flashing specific error messages rather than `"Something went wrong. Please try again."`.
5. **Fix Session Lifetime & Midnight IST Deadline:**
   - Increase `app.config["PERMANENT_SESSION_LIFETIME"] = 3600` (1 hour).
   - Parse `SCHEME_DEADLINE` in `Asia/Kolkata` timezone and treat `23:59` as `23:59:59` (midnight IST).
6. **Add Review Summary on `/declaration` + Self-Service Edit & Cancel/Withdraw for `PENDING` Applications:**
   - On `/declaration`, display all entered fields (`applicant_name`, `dob`, `gender`, `marital_status`, `village`, `block`, `bank_account`, `ifsc`, uploaded document name) with "Edit" links to `/form/1`, `/form/2`, `/form/3`, and `/upload`.
   - On `/application/<int:app_id>`, when `status == 'PENDING'`, provide:
     1. **Edit Application (`/application/<int:app_id>/edit`):** Allows the logged-in citizen to fix typos in their name, DOB, gender, marital status, village, block, bank account, or IFSC (re-validating and regenerating the cached acknowledgment PDF and updating `portal_users` password hash if DOB changed).
     2. **Cancel / Withdraw Application (`/application/<int:app_id>/cancel` POST):** Marks the application as `'WITHDRAWN'` (or deletes only the citizen's own newly submitted pending application upon their explicit request) AND updates the duplicate-application check in `handle_submission()` (`WHERE mobile = %s AND status != 'WITHDRAWN'`) so a citizen who cancelled a mistaken application can re-apply without calling the help desk, while still enforcing **one active application per person**!

**Commit Pair 5:**
- `test: add failing tests for DD/MM/YYYY parsing, widow validation, upload limits, clear errors, and pending edits`
- `fix: resolve form validation traps, support JPG/5MB uploads, add clear errors, and enable pending application edits`

---

## PHASE 2: BUN + NEXT.JS FRONTEND + FLASK BACKEND HYBRID ARCHITECTURE

After all 5 core fixes and their `pytest` suites are committed and passing, integrate the **Bun + Next.js** citizen experience while preserving **100% probe compatibility**:

1. **Architecture Design:**
   - Keep Flask (`app/app.py`) as the authoritative backend and HTML/API server so `pytest` tests and direct HTTP form `POST` probes work with zero friction.
   - If running Next.js (on Bun) alongside Flask in `docker-compose.yml` (or enhancing the frontend UI):
     - Port `8000` is the single public entry point.
     - All `/admin/*`, `/__gateway/*`, `/ack/*`, and `POST` form handlers (`/apply`, `/verify`, `/form/*`, `/upload`, `/declaration`, `/status`, `/status/reset`, `/application/*`) MUST preserve exact Flask session cookie compatibility, form `name` attributes, HTTP status codes (`200`, `302`, `403`, `404`), and HTML structure expected by the automated probe.
     - Enhance the citizen pages (`/`, `/apply`, `/verify`, `/form/1..3`, `/upload`, `/declaration`, `/status`, `/application/<id>`) with:
       - Clean, high-contrast, senior-friendly typography and step progress indicators.
       - Dynamic client-side toggling: hide `husband_name` and `husband_employer` when `marital_status` is `"Unmarried"`, and label `husband_name` as `"Late Spouse's Name (Optional)"` (and hide `husband_employer`) when `"Widowed"`.
       - Real-time client-side hints for `DD/MM/YYYY` date format, 10-digit mobile number, IFSC code, and 5 MB PDF/JPG upload preview.
       - Never use client-only `required` attributes that would block automated probes testing server-side error messages, or ensure server-side flash messages (`class="flash"`) are always rendered prominently in the DOM!

---

## PHASE 3: END-TO-END AUTOMATED PROBE VERIFICATION SUITE

Create and run a comprehensive verification test suite (`tests/test_probe_simulation.py`) that simulates every check listed in `BRIEF.md`:

1. **Liveness Probe:** `GET /`, `GET /about`, `GET /__gateway/` all return `200 OK`.
2. **Security Probe:**
   - Unauthenticated `GET /ack/1.pdf` -> `403` / redirect (not `200`).
   - Logged-in Citizen A requesting Citizen B's `GET /application/<B_id>` and `GET /ack/<B_id>.pdf` -> `403`.
   - Logged-in Citizen A sending `POST /admin/approve/<id>` and `POST /admin/reject/<id>` -> `403`.
   - SQL injection payload on `POST /status/reset` (`mobile = "' OR '1'='1"`) -> safe, no data leak or unintended reset.
   - Old default admin password (`sewasetu@123`) on `POST /admin` -> rejected (`Invalid credentials.`).
   - New rotated admin password on `POST /admin` -> succeeds (`302` to `/admin/dashboard`).
3. **Data Integrity Probe (Indic Script Round-Trip):**
   - Complete full flow (`/apply` -> read OTP from `/__gateway/api/messages?to=<mobile>` after waiting for `delivered: true` -> `/verify` -> `/form/1` with Assamese name `"কমলাকান্ত ভট্টাচাৰ্য — PROBE_TOKEN_123"` and DOB `"05/10/1960"` -> `/form/2` -> `/form/3` -> `/upload` -> `/declaration`).
   - Verify `confirmation.html` allows downloading `/ack/<new_id>.pdf` (`200 OK`, valid PDF bytes, no `?` fallback).
   - Log into `/status` using mobile and password `"05101960"` (`DDMMYYYY`) and verify `/application/<new_id>` displays the exact untruncated Assamese name `"কমলাকান্ত ভট্টাচাৰ্য — PROBE_TOKEN_123"`.
4. **Citizen Journeys Probe (Confused Users & Error Messages):**
   - Submit invalid mobile (`"123"`), underage DOB (`"01/01/2000"`), invalid DOB (`"not-a-date"`), widow with blank `husband_employer` (must SUCCEED!), `.jpg` file of 500 KB (must SUCCEED!), `.exe` file (must show clear format error, NOT `"ERR_VAL_47"`), and >5 MB file (must show clear size error, NOT `"ERR_VAL_47"`).
   - Test editing/correcting a `PENDING` application and cancelling/re-applying.
5. **Load Probe (Month-End Spike Simulation):**
   - Fire 100 concurrent requests across `/`, `/status`, `/ack/<id>.pdf`, and `/admin/dashboard` using `concurrent.futures.ThreadPoolExecutor(max_workers=25)`.
   - Assert `0` `500` errors, `0` `psycopg2.OperationalError: too many clients already`, and p95 latency < 500ms.
6. **Pre-Existing Citizen Data Probe:**
   - Sample pre-existing citizens from `seed/seed.sql` (including citizens whose applications were submitted >15 days ago between March and June 2026), log into `/status` with their `mobile` and `DDMMYYYY` password, and assert their records load and overdue `PENDING` applications now show as approved.
7. **Git History Verification:**
   - Run `git log --oneline` to verify that every `fix:` commit is immediately preceded by its corresponding `test:` commit, and nothing was squashed.