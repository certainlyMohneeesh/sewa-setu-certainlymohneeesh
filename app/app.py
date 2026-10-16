# -*- coding: utf-8 -*-
"""
Sewa Setu - Old Age Pension Portal
Government of Purvanchal, Department of Social Welfare

Developed by: Netlink Infosolutions Pvt Ltd (2023)
Maintenance contract ended 31/01/2026.
"""

import os
import io
import csv
import uuid
import threading
from contextlib import contextmanager
import random
import hashlib
import hmac
import re
import unicodedata
import configparser
import time
from datetime import datetime, date, timedelta, timezone

import requests
import psycopg2
import psycopg2.pool
from flask import (Flask, request, session, redirect, url_for, render_template,
                   flash, send_file, abort, jsonify)
from fpdf import FPDF
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, "config", "app.ini"))



def _setting(env_name, section, option):
    # environment first, so secrets need not live in the repository
    return os.environ.get(env_name) or config.get(section, option, fallback="")


app = Flask(__name__)
app.secret_key = _setting("SECRET_KEY", "app", "secret_key")
if len(app.secret_key) < 32:
    raise RuntimeError("SECRET_KEY environment variable must be set to a random "
                       "string of at least 32 characters (see scripts/gen-env.sh)")
app.config["PERMANENT_SESSION_LIFETIME"] = 3600   # a 6-step form takes a senior citizen a while
# Uploads are limited to 5 MB in upload(); this only stops absurd request bodies
# before they are read into memory.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = 4 * MAX_UPLOAD_BYTES
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"     # blocks cross-site POSTs (CSRF)

ADMIN_USERNAME = _setting("ADMIN_USERNAME", "admin", "username")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")             # plaintext, or use the hash below
ADMIN_PASSWORD_HASH = _setting("ADMIN_PASSWORD_HASH", "admin", "password_hash")

SMS_GATEWAY_URL = config.get("app", "sms_gateway_url")
OTP_VALIDITY_SECONDS = config.getint("app", "otp_validity_seconds")
UPLOAD_DIR = config.get("app", "upload_dir")
# "2026-10-31 23:59" means the window closes at midnight India Standard Time, so
# the last minute is included in full (23:59:59). Compared against now_ist().
SCHEME_DEADLINE = datetime.strptime(config.get("pension", "scheme_deadline"),
                                    "%Y-%m-%d %H:%M").replace(second=59)
MIN_AGE = config.getint("pension", "min_age")
SLA_DAYS = config.getint("pension", "sla_days")

BLOCKS = ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]

import logging
_logdir = "/var/log/sewasetu"
try:
    os.makedirs(_logdir, exist_ok=True)
    _fh = logging.FileHandler(os.path.join(_logdir, "sewasetu-app.log"))
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s in app: %(message)s"))
    app.logger.addHandler(_fh)
    app.logger.setLevel(logging.INFO)
except Exception:
    pass


def db_connection_args():
    """Connection settings. Host/port/name/user have defaults in config/app.ini;
    the password is a secret and only ever comes from the environment
    (SEWASETU_DB_PASSWORD). Read per call so tests and tools can redirect it."""
    def pick(env_name, option):
        return os.environ.get(env_name) or config.get("database", option, fallback="")

    return dict(host=pick("SEWASETU_DB_HOST", "host"),
                port=pick("SEWASETU_DB_PORT", "port"),
                dbname=pick("SEWASETU_DB_NAME", "name"),
                user=pick("SEWASETU_DB_USER", "user"),
                password=pick("SEWASETU_DB_PASSWORD", "password"))


def get_db():
    """A direct, unpooled connection. The caller must close it. Used by one-off
    startup migrations and tools; request handlers use db_cursor() instead."""
    return psycopg2.connect(**db_connection_args())


# ---------------------------------------------------------------------------
# Connection pool
#
# Every call used to open a new Postgres connection and the error paths never
# closed it, so at month-end (~17x traffic) connections leaked until Postgres
# answered "sorry, too many clients already". Each worker process now owns one
# small pool; a request borrows a connection for the shortest time possible and
# always gives it back, whatever happens.
# ---------------------------------------------------------------------------

POOL_MIN = int(os.environ.get("SEWASETU_DB_POOL_MIN", 2))
POOL_MAX = int(os.environ.get("SEWASETU_DB_POOL_MAX", 20))
POOL_WAIT_SECONDS = float(os.environ.get("SEWASETU_DB_POOL_TIMEOUT", 10))


class PoolBusy(Exception):
    """No database connection became free in time; shed load instead of piling on."""


class _BlockingPool(object):
    # ThreadedConnectionPool raises immediately when exhausted; a semaphore makes
    # callers wait their turn for a connection instead.
    def __init__(self):
        self.pid = os.getpid()
        self._slots = threading.BoundedSemaphore(POOL_MAX)
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            POOL_MIN, POOL_MAX, **db_connection_args())

    def getconn(self):
        if not self._slots.acquire(timeout=POOL_WAIT_SECONDS):
            raise PoolBusy()
        try:
            return self._pool.getconn()
        except BaseException:
            self._slots.release()
            raise

    def putconn(self, conn, close=False):
        try:
            self._pool.putconn(conn, close=close)    # rolls back an open transaction
        finally:
            self._slots.release()


_pool_lock = threading.Lock()
_pool_holder = {"pool": None}


def _get_pool():
    pool = _pool_holder["pool"]
    if pool is None or pool.pid != os.getpid():      # never share a pool across fork()
        with _pool_lock:
            pool = _pool_holder["pool"]
            if pool is None or pool.pid != os.getpid():
                pool = _pool_holder["pool"] = _BlockingPool()
    return pool


@contextmanager
def db_conn():
    pool = _get_pool()
    conn = pool.getconn()
    discard = False
    try:
        yield conn
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        discard = True                                # connection is probably dead
        raise
    except BaseException:
        if not conn.closed:
            conn.rollback()
        raise
    finally:
        pool.putconn(conn, close=discard or conn.closed != 0)


@contextmanager
def db_cursor():
    """Cursor on a pooled connection. Commit explicitly with cur.connection.commit();
    anything uncommitted is rolled back, and the connection always goes back."""
    with db_conn() as conn:
        cur = conn.cursor()
        try:
            yield cur
        finally:
            cur.close()


@app.errorhandler(PoolBusy)
def _pool_busy(error):
    app.logger.error("database pool exhausted: no connection free after %.0fs"
                     % POOL_WAIT_SECONDS)
    return ("The service is very busy right now. Please try again in a minute.", 503,
            {"Retry-After": "10", "Content-Type": "text/plain; charset=utf-8"})


# Indexes: the 100,000-row tables had only primary keys, so every login, duplicate
# check, admin queue and dashboard count scanned the whole table. Also in schema.sql
# for new databases; applied here (idempotently) to the existing production one.
INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_applications_mobile ON applications (mobile)",
    "CREATE INDEX IF NOT EXISTS idx_applications_app_no ON applications (application_no)",
    "CREATE INDEX IF NOT EXISTS idx_applications_status_submitted "
    "ON applications (status, submitted_at ASC)",
    "CREATE INDEX IF NOT EXISTS idx_applications_status_block ON applications (status, block)",
    "CREATE INDEX IF NOT EXISTS idx_otps_mobile_id ON otps (mobile, id DESC)",
]


def ensure_indexes():
    conn = get_db()
    try:
        cur = conn.cursor()
        # Every gunicorn worker runs this at start-up; a blocking advisory lock makes
        # the others wait, then find the indexes already there.
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (0x52545031,))
        for statement in INDEX_STATEMENTS:
            cur.execute(statement)
        conn.commit()
    finally:
        conn.close()


# Statutory time is India Standard Time (UTC+05:30, no DST). Timestamps in the
# database are naive IST.
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    return datetime.now(IST).replace(tzinfo=None)


def display_status(status):
    # Under the RTPS Act a deemed approval is an approval; show it as one.
    if status == "DEEMED_APPROVED":
        return "APPROVED (Deemed Approved under RTPS Act)"
    return status


_DEEM_SQL = """UPDATE applications
               SET status = 'DEEMED_APPROVED',
                   decided_at = submitted_at + %s * interval '1 day',
                   decided_by = 'RTPS-AUTO'
               WHERE status = 'PENDING' AND submitted_at < %s"""


def deem_if_overdue(cur, app_id):
    """Approve this application now if it is PENDING past the RTPS SLA, so an
    untouched file is never shown as PENDING between nightly runs."""
    cur.execute(_DEEM_SQL + " AND id = %s",
                (SLA_DAYS, now_ist() - timedelta(days=SLA_DAYS), app_id))
    return cur.rowcount


def heal_overdue_applications():
    """Idempotent bulk migration: deem every PENDING application past the SLA.

    Used at startup to clear the backlog left by the nightly job failing since
    2026-03-15. Updates only; it deliberately sends no SMS (thousands of
    synchronous gateway calls would stall startup). Returns rows healed.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        # One worker does the job; the others skip instead of queueing behind it.
        cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (0x52545053,))
        if not cur.fetchone()[0]:
            conn.rollback()
            return 0
        cur.execute(_DEEM_SQL, (SLA_DAYS, now_ist() - timedelta(days=SLA_DAYS)))
        healed = cur.rowcount
        conn.commit()
        return healed
    finally:
        conn.close()


def sanitize(value, maxlen=100):
    # Trim to the column width (VARCHAR(100)) by *characters*. The old code cut
    # at 20 UTF-8 bytes, which left only 6 letters of any Bengali/Assamese name.
    if value is None:
        return ""
    value = unicodedata.normalize("NFC", str(value).strip())
    return value[:maxlen]


def hash_password(p):
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def safe_equals(a, b):
    return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


def admin_credentials_valid(username, password):
    user_ok = safe_equals(username, ADMIN_USERNAME)
    if ADMIN_PASSWORD:
        password_ok = safe_equals(password, ADMIN_PASSWORD)
    else:
        password_ok = bool(ADMIN_PASSWORD_HASH) and check_password_hash(
            ADMIN_PASSWORD_HASH, password)
    return user_ok and password_ok


def require_application_access(cur, app_id, allow_just_submitted=False):
    """Admins may open any application; a citizen only their own. Everyone else,
    including citizens asking for an id that does not exist, gets 403 so ids
    cannot be enumerated. Admins get 404 for a missing id."""
    if session.get("admin"):
        return
    if allow_just_submitted and session.get("just_submitted_app_id") == app_id:
        return
    mobile = session.get("portal_mobile")
    if session.get("logged_in") and mobile:
        cur.execute("SELECT mobile FROM applications WHERE id = %s", (app_id,))
        row = cur.fetchone()
        if row and safe_equals(row[0] or "", mobile):
            return
    abort(403)


# ---------------------------------------------------------------------------
# Form validation. One set of rules and messages, used by every form step, by the
# final submission and by the edit page, so a citizen is told the real reason
# straight away instead of "Something went wrong" at the very end.
# ---------------------------------------------------------------------------

GENDERS = ("Male", "Female", "Other")
MARITAL_STATUSES = ("Married", "Unmarried", "Widowed")
# The form says DD/MM/YYYY, so day-first is tried first. Month-first is only a
# last resort, which in practice means a second number above 12 (10/25/1960).
DOB_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y")
ALLOWED_UPLOADS = (".pdf", ".jpg", ".jpeg", ".png")

MSG_NAME = "Please enter your full name."
MSG_DOB = "Please enter a valid Date of Birth in DD/MM/YYYY format."
MSG_CHOICE = "Please select your gender and marital status."
MSG_ADDRESS = "Please enter your village/town and select your block."
MSG_BANK = "Please enter a valid Bank Account Number and IFSC Code."


def parse_dob(raw):
    raw = (raw or "").strip()
    for fmt in DOB_FORMATS:
        try:
            dob = datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
        if dob.year >= 1900:
            return dob
    return None


def age_on(dob, today):
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def validate_personal(data):
    """Returns an error message, or None when the personal details are fine."""
    if not sanitize(data.get("applicant_name")):
        return MSG_NAME
    today = now_ist().date()
    dob = parse_dob(data.get("dob"))
    if dob is None or dob > today:
        return MSG_DOB
    age = age_on(dob, today)
    if age < MIN_AGE:
        return ("Applicant must be at least %d years of age on the date of application "
                "(current age: %d)." % (MIN_AGE, age))
    if data.get("gender") not in GENDERS or data.get("marital_status") not in MARITAL_STATUSES:
        return MSG_CHOICE
    return None


def validate_address(data):
    if not sanitize(data.get("village")) or not sanitize(data.get("block"), 50):
        return MSG_ADDRESS
    return None


def clean_account(value):
    return re.sub(r"[\s-]", "", str(value or ""))


def validate_bank(data):
    account = clean_account(data.get("bank_account"))
    ifsc = str(data.get("ifsc") or "").strip()
    if not (account.isdigit() and 9 <= len(account) <= 18):
        return MSG_BANK
    if not re.fullmatch(r"[A-Za-z0-9]{11}", ifsc):
        return MSG_BANK
    return None


# step number -> (validator, form fields kept in the session for that step)
STEP_RULES = {
    1: (validate_personal, ("applicant_name", "dob", "gender", "marital_status",
                            "husband_name", "husband_employer")),
    2: (validate_address, ("village", "block")),
    3: (validate_bank, ("bank_account", "ifsc")),
}


def first_form_error(data):
    """(error, step to send the citizen back to) for the first invalid step."""
    for step in (1, 2, 3):
        error = STEP_RULES[step][0](data)
        if error:
            return error, step
    return None, None


def send_sms(mobile, text):
    try:
        requests.post(SMS_GATEWAY_URL + "/api/send",
                      json={"to": mobile, "text": text}, timeout=5)
    except Exception as e:
        app.logger.error("sms gateway error: %s" % e)


def deadline_remaining():
    delta = SCHEME_DEADLINE - now_ist()
    if delta.total_seconds() <= 0:
        return None
    return int(delta.total_seconds() // 3600)


def new_application_no():
    return "SSP" + datetime.now().strftime("%y") + str(random.randint(100000, 999999))


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", hours_left=deadline_remaining())


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/__gateway/", defaults={"subpath": ""})
@app.route("/__gateway/<path:subpath>")
def gateway_proxy(subpath):
    # Convenience proxy to the internal SMS gateway, so the OTP inbox is reachable
    # on the main site without opening a second port on the host. The gateway
    # itself runs only on the internal network.
    try:
        r = requests.get(SMS_GATEWAY_URL + "/" + subpath,
                         params=request.args, timeout=5)
        return (r.content, r.status_code,
                {"Content-Type": r.headers.get("Content-Type", "text/html")})
    except Exception as e:
        return ("SMS gateway unreachable: %s" % e, 502)


# ---------------------------------------------------------------------------
# Application flow: mobile -> OTP -> form steps -> upload -> declaration
# ---------------------------------------------------------------------------

@app.route("/apply", methods=["GET", "POST"])
def apply():
    if deadline_remaining() is None:
        flash("The application window for this scheme has closed.")
        return redirect(url_for("index"))
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        captcha = request.form.get("captcha", "")
        if len(mobile) != 10 or not mobile.isdigit():
            flash("Please enter a valid 10-digit mobile number.")
            return render_template("apply.html", captcha_q=make_captcha())
        # Checked when the form sent an answer and we issued a question; a client
        # that never loaded the form (no question in its session) is not blocked.
        expected = session.get("captcha_answer")
        if "captcha" in request.form and expected is not None and \
                captcha.strip() != str(expected):
            flash("Incorrect security check (CAPTCHA) answer. Please try again.")
            return render_template("apply.html", captcha_q=make_captcha())
        code = str(random.randint(100000, 999999))
        with db_cursor() as cur:
            cur.execute("INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
                        (mobile, code, datetime.now()))
            cur.connection.commit()
        send_sms(mobile, "Your Sewa Setu OTP is %s. Valid for 5 minutes." % code)
        session.permanent = True
        session["apply_mobile"] = mobile
        return redirect(url_for("verify"))
    return render_template("apply.html", captcha_q=make_captcha())


def make_captcha():
    a, b = random.randint(1, 9), random.randint(1, 9)
    session["captcha_answer"] = a + b
    return "%d + %d" % (a, b)


@app.route("/verify", methods=["GET", "POST"])
def verify():
    mobile = session.get("apply_mobile")
    if not mobile:
        return redirect(url_for("apply"))
    if request.method == "POST":
        code = request.form.get("otp", "").strip()
        with db_cursor() as cur:
            cur.execute("SELECT code, created_at FROM otps WHERE mobile = %s "
                        "ORDER BY id DESC LIMIT 1", (mobile,))
            row = cur.fetchone()
        if row and row[0] == code:
            age = (datetime.now() - row[1]).total_seconds()
            if age > OTP_VALIDITY_SECONDS:
                app.logger.warning("otp expired mobile=%s age=%ds" % (mobile, int(age)))
                flash("OTP expired. Please request a new OTP.")
                return redirect(url_for("apply"))
            session["verified_mobile"] = mobile
            return redirect(url_for("form_step", step=1))
        flash("Invalid OTP.")
    return render_template("verify.html", mobile=mobile)


@app.route("/form/<int:step>", methods=["GET", "POST"])
def form_step(step):
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if step not in (1, 2, 3):
        abort(404)
    if request.method == "POST":
        validator, fields = STEP_RULES[step]
        data = dict(session.get("form_data", {}))
        for k in fields:
            if k in request.form:
                data[k] = request.form[k]
        session["form_data"] = data
        error = validator(data)
        if error:
            flash(error)
            return render_template("form_step%d.html" % step, data=data, blocks=BLOCKS)
        if step < 3:
            return redirect(url_for("form_step", step=step + 1))
        return redirect(url_for("upload"))
    return render_template("form_step%d.html" % step,
                           data=session.get("form_data", {}), blocks=BLOCKS)


MSG_FILE_TOO_BIG = ("File size exceeds the 5 MB limit. Please upload a file "
                    "smaller than 5 MB.")


@app.errorhandler(413)
def _request_too_large(error):
    flash(MSG_FILE_TOO_BIG)
    return render_template("upload.html"), 413


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        f = request.files.get("document")
        if f is None or f.filename == "":
            if session.get("doc_path"):          # already uploaded: carry on
                return redirect(url_for("declaration"))
            flash("Please select an age proof document (PDF or JPG) to upload.")
            return render_template("upload.html")
        original = f.filename.lower()
        ext = os.path.splitext(original)[1]
        if ext not in ALLOWED_UPLOADS:
            flash("Invalid file format. Please upload a PDF or JPG/JPEG image file.")
            return render_template("upload.html")
        content = f.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            flash(MSG_FILE_TOO_BIG)
            return render_template("upload.html")
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        # never trust the client's filename (path traversal); a name with no
        # ASCII letters (e.g. Assamese) falls back to "document"
        safe_name = secure_filename(original[:-len(ext)]) or "document"
        path = os.path.join(UPLOAD_DIR, "%s_%s%s" % (session["verified_mobile"], safe_name, ext))
        with open(path, "wb") as out:
            out.write(content)
        session["doc_path"] = path
        return redirect(url_for("declaration"))
    return render_template("upload.html")


@app.route("/declaration", methods=["GET", "POST"])
def declaration():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        return handle_submission()
    doc_path = session.get("doc_path", "")
    return render_template("declaration.html", data=session.get("form_data", {}),
                           doc_name=os.path.basename(doc_path).split("_", 1)[-1] if doc_path else "")


def handle_submission():
    # TODO: split this up some day. It grew. - RK, 09/2024
    mobile = session.get("verified_mobile")
    data = session.get("form_data", {})
    doc_path = session.get("doc_path", "")

    error, back_to = first_form_error(data)
    if error:
        flash(error)
        return redirect(url_for("form_step", step=back_to))

    name = sanitize(data.get("applicant_name"))
    village = sanitize(data.get("village"))
    block = sanitize(data.get("block"), 50)
    gender = data.get("gender")
    marital = data.get("marital_status")
    # A widow(er) or unmarried applicant has no husband's employer to give, so
    # neither of these is ever required.
    husband_name = sanitize(data.get("husband_name"))
    husband_employer = sanitize(data.get("husband_employer"))
    bank_account = clean_account(data.get("bank_account"))
    ifsc = data.get("ifsc", "").strip().upper()
    dob = parse_dob(data.get("dob"))

    if now_ist() > SCHEME_DEADLINE:
        flash("The application deadline has passed.")
        return redirect(url_for("index"))

    with db_cursor() as cur:
        # one *active* application per person; a withdrawn one does not count
        cur.execute("SELECT count(*) FROM applications WHERE mobile = %s "
                    "AND status != 'WITHDRAWN'", (mobile,))
        if cur.fetchone()[0] > 0:
            flash("An application already exists for this mobile number. "
                  "Duplicate applications are not permitted.")
            return redirect(url_for("index"))

        app_no = new_application_no()
        cur.execute(
            """INSERT INTO applications
               (application_no, applicant_name, mobile, dob, gender, marital_status,
                husband_name, husband_employer, village, block, bank_account, ifsc,
                doc_path, status, submitted_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)
               RETURNING id""",
            (app_no, name, mobile, dob, gender, marital, husband_name,
             husband_employer, village, block, bank_account, ifsc, doc_path,
             now_ist()))
        new_id = cur.fetchone()[0]

        # status portal account; password is DOB as DDMMYYYY per dept. circular
        portal_pass = dob.strftime("%d%m%Y")
        # (a citizen re-applying after a withdrawal gets the new DOB as password)
        cur.execute("INSERT INTO portal_users (mobile, password_hash) VALUES (%s,%s) "
                    "ON CONFLICT (mobile) DO UPDATE SET password_hash = EXCLUDED.password_hash",
                    (mobile, hash_password(portal_pass)))
        cur.connection.commit()
        slip_row = fetch_acknowledgment_row(cur, new_id)

    # The application is saved. Draw and cache the slip after the database
    # connection has been returned; if drawing fails it is rebuilt on first download.
    try:
        app.logger.info("generating acknowledgment pdf application=%d" % new_id)
        save_acknowledgment(new_id, render_acknowledgment(slip_row))
    except Exception as e:
        app.logger.error("acknowledgment pdf for application=%d failed: %s" % (new_id, e))

    session.pop("form_data", None)
    session.pop("doc_path", None)
    session.pop("verified_mobile", None)
    session["just_submitted_app_id"] = new_id    # lets them fetch their own slip
    send_sms(mobile, "Sewa Setu: application %s received. Track at the status portal "
                     "with mobile no. and password (DOB as DDMMYYYY)." % app_no)
    return render_template("confirmation.html", app_no=app_no, app_id=new_id)


# Acknowledgment slip -------------------------------------------------------
#
# Latin text uses the PDF core font Helvetica (built in: no file to parse or
# subset). A bundled Noto font is loaded only when the text needs it: Bengali
# script (Assamese, Bengali) and Devanagari (Hindi, Marathi, Nepali) each have
# their own font, and Noto Sans covers any other character Helvetica lacks.
# Loading and subsetting fonts is what takes time, so a slip only pays for the
# scripts it actually contains. (The core fonts alone are Latin-1 only, which is
# why Assamese names used to print as "?".)
FONT_DIR = os.path.join(BASE_DIR, "fonts")
CORE_FONT, LATIN_FONT = "helvetica", "NotoSans"
BENGALI_FONT, DEVANAGARI_FONT = "NotoSansBengali", "NotoSansDevanagari"
_FONT_FILES = {
    LATIN_FONT: "NotoSans-Regular.ttf",
    BENGALI_FONT: "NotoSansBengali-Regular.ttf",
    DEVANAGARI_FONT: "NotoSansDevanagari-Regular.ttf",
}
_JOINERS = ("\u200c", "\u200d")       # ZWNJ / ZWJ: part of the preceding cluster
_VIRAMAS = ("\u09cd", "\u094d")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
SLIP_LABELS = ["Application No", "Applicant Name", "Mobile", "Date of Birth",
               "Village", "Block", "Bank Account", "IFSC", "Submitted At", "Status"]


def _font_for(ch, previous):
    cp = ord(ch)
    if 0x0980 <= cp <= 0x09FF:
        return BENGALI_FONT
    if 0x0900 <= cp <= 0x097F:
        return DEVANAGARI_FONT
    if ch in _JOINERS:
        return previous
    return CORE_FONT if cp < 256 else LATIN_FONT


def _script_runs(text):
    """Split text into (font, substring) runs, one font per run. Per-run font
    choice is used instead of fpdf2 fallback fonts, which misrender when
    combined with text shaping."""
    runs = []
    font = CORE_FONT
    for ch in text:
        font = _font_for(ch, font)
        if runs and runs[-1][0] == font:
            runs[-1][1] += ch
        else:
            runs.append([font, ch])
    return [(f, t) for f, t in runs]


class _Slip(object):
    """One acknowledgment PDF; fonts are registered the first time they are used."""

    def __init__(self, shaping):
        self.pdf = FPDF()
        if shaping:
            self.pdf.set_text_shaping(True)   # Bengali/Devanagari conjuncts, vowel signs
        self.pdf.set_margins(15, 15, 15)
        self.loaded = set()

    def font(self, family, style="", size=10):
        if family in _FONT_FILES and family not in self.loaded:
            self.pdf.add_font(family, "", os.path.join(FONT_DIR, _FONT_FILES[family]))
            self.loaded.add(family)
        self.pdf.set_font(family, "" if family in _FONT_FILES else style, size)

    def width(self, text, size):
        total = 0
        for family, run in _script_runs(text):
            self.font(family, "", size)
            total += self.pdf.get_string_width(run)
        return total

    def wrap(self, text, size, max_width):
        """Greedy line wrap that prefers spaces and never splits a letter cluster."""
        lines, current = [], ""
        for ch in text:
            if (current and self.width(current + ch, size) > max_width
                    and not unicodedata.category(ch).startswith("M")
                    and ch not in _JOINERS and current[-1] not in _VIRAMAS):
                space = current.rfind(" ")
                if ch != " " and space > 0:
                    lines.append(current[:space])
                    current = current[space + 1:] + ch
                else:
                    lines.append(current)
                    current = ch.lstrip(" ")
            else:
                current += ch
        lines.append(current)
        return lines

    def row(self, label, value, size=10, line_h=8, label_w=60):
        pdf = self.pdf
        x0, y0 = pdf.l_margin, pdf.get_y()
        value_w = pdf.w - pdf.r_margin - pdf.l_margin - label_w
        lines = self.wrap(value, size, value_w - 2)
        height = line_h * len(lines)
        pdf.rect(x0, y0, label_w, height)
        pdf.rect(x0 + label_w, y0, value_w, height)
        self.font(CORE_FONT, "", size)
        pdf.set_xy(x0 + 1, y0)
        pdf.cell(label_w - 2, line_h, label)
        for n, line in enumerate(lines):
            pdf.set_xy(x0 + label_w + 1, y0 + n * line_h)
            for family, run in _script_runs(line):
                self.font(family, "", size)
                pdf.cell(pdf.get_string_width(run), line_h, run)
        pdf.set_xy(x0, y0 + height)


def fetch_acknowledgment_row(cur, app_id):
    """Everything the slip prints, as display strings. Do this inside the database
    connection; draw afterwards (render_acknowledgment) once it is released."""
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, submitted_at, status FROM applications WHERE id = %s",
                (app_id,))
    row = list(cur.fetchone())
    row[9] = display_status(row[9])
    return [_CONTROL.sub(" ", "" if v is None else str(v)) for v in row]


def render_acknowledgment(row):
    """Draw the slip. Pure CPU, no database access."""
    needs_shaping = any(0x0900 <= ord(ch) <= 0x09FF for value in row for ch in value)
    slip = _Slip(needs_shaping)
    pdf = slip.pdf
    pdf.add_page()
    slip.font(CORE_FONT, "B", 14)
    pdf.cell(0, 10, "GOVERNMENT OF PURVANCHAL", new_x="LMARGIN", new_y="NEXT", align="C")
    slip.font(CORE_FONT, "", 11)
    pdf.cell(0, 8, "Department of Social Welfare", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.cell(0, 8, "Old Age Pension Scheme - Acknowledgment", new_x="LMARGIN", new_y="NEXT",
             align="C")
    pdf.ln(4)
    # decorative border, as per approved letterhead design: two lines (it used to
    # be 4,000 overlapping 0.5 mm segments that looked the same)
    pdf.line(10, 282, 200, 282)
    pdf.line(10, 12, 200, 12)
    for label, value in zip(SLIP_LABELS, row):
        slip.row(label, value)
    pdf.ln(6)
    slip.font(CORE_FONT, "", 9)
    pdf.multi_cell(0, 5, "This is a computer generated acknowledgment. Processing SLA "
                         "as per the Purvanchal Right to Public Services Act applies.")
    return bytes(pdf.output())


def generate_acknowledgment(cur, app_id):
    return render_acknowledgment(fetch_acknowledgment_row(cur, app_id))


# Slips are cached on disk (UPLOAD_DIR/ack/<id>.pdf) so a download after the first
# does no work; the cache entry is dropped whenever the application changes.
def acknowledgment_path(app_id):
    return os.path.join(UPLOAD_DIR, "ack", "%d.pdf" % app_id)


def save_acknowledgment(app_id, data):
    path = acknowledgment_path(app_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%s.tmp" % (path, uuid.uuid4().hex)     # write then rename: never half a file
    with open(tmp, "wb") as out:
        out.write(data)
    os.replace(tmp, path)


def invalidate_acknowledgment(app_id):
    try:
        os.remove(acknowledgment_path(app_id))
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Status portal (citizen login)
# ---------------------------------------------------------------------------

@app.route("/status", methods=["GET", "POST"])
def status_login():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        with db_cursor() as cur:
            cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (mobile,))
            row = cur.fetchone()
            r = None
            if row and safe_equals(row[0], hash_password(password)):
                cur.execute("SELECT id FROM applications WHERE mobile = %s "
                            "ORDER BY submitted_at DESC LIMIT 1", (mobile,))
                r = cur.fetchone()
        if row and safe_equals(row[0], hash_password(password)):
            session.clear()
            session["logged_in"] = True
            session["portal_mobile"] = mobile
            if r:
                return redirect(url_for("view_application", app_id=r[0]))
            flash("No application found for this mobile number.")
            return redirect(url_for("status_login"))
        flash("Incorrect mobile number or password. Your password is your date of birth "
              "as DDMMYYYY (for example 05101960 for 5 October 1960).")
    return render_template("status_login.html")


@app.route("/application/<int:app_id>")
def view_application(app_id):
    if not session.get("logged_in"):
        flash("Please login to view application status.")
        return redirect(url_for("status_login"))
    with db_cursor() as cur:
        require_application_access(cur, app_id)
        if deem_if_overdue(cur, app_id):
            cur.connection.commit()
        cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                    "bank_account, ifsc, status, submitted_at, decided_at "
                    "FROM applications WHERE id = %s", (app_id,))
        row = cur.fetchone()
    if not row:
        abort(404)
    return render_template("application.html", a=row, app_id=app_id,
                           status_label=display_status(row[8]),
                           deemed=(row[8] == "DEEMED_APPROVED"),
                           can_change=(row[8] == "PENDING" and
                                       not session.get("admin")))


EDIT_COLUMNS = ("applicant_name", "dob", "gender", "marital_status", "husband_name",
                "husband_employer", "village", "block", "bank_account", "ifsc")


def own_pending_application(cur, app_id):
    """The citizen's own application, still PENDING, or None (after a message).
    403 for anyone else: staff decide applications, they do not edit them."""
    mobile = session.get("portal_mobile")
    if not (session.get("logged_in") and mobile) or session.get("admin"):
        return None
    if deem_if_overdue(cur, app_id):
        cur.connection.commit()
    cur.execute("SELECT mobile, status, " + ", ".join(EDIT_COLUMNS) +
                " FROM applications WHERE id = %s", (app_id,))
    row = cur.fetchone()
    if not row or not safe_equals(row[0] or "", mobile):
        abort(403)
    if row[1] != "PENDING":
        flash("This application is already %s and can no longer be changed."
              % display_status(row[1]))
        return False
    return dict(zip(EDIT_COLUMNS, row[2:]))


def citizen_gate():
    if not session.get("logged_in") or session.get("admin"):
        if session.get("admin"):
            abort(403)
        flash("Please login to view application status.")
        return redirect(url_for("status_login"))
    return None


@app.route("/application/<int:app_id>/edit", methods=["GET", "POST"])
def edit_application(app_id):
    gate = citizen_gate()
    if gate:
        return gate
    with db_cursor() as cur:
        current = own_pending_application(cur, app_id)
    if current is None:
        abort(403)
    if current is False:
        return redirect(url_for("view_application", app_id=app_id))
    data = {k: ("" if v is None else str(v)) for k, v in current.items()}
    data["dob"] = current["dob"].strftime("%d/%m/%Y") if current["dob"] else ""
    if request.method == "POST":
        for k in EDIT_COLUMNS:
            if k in request.form:
                data[k] = request.form[k]
        error = (validate_personal(data) or validate_address(data) or validate_bank(data))
        if error:
            flash(error)
            return render_template("edit_application.html", data=data, blocks=BLOCKS,
                                   app_id=app_id)
        dob = parse_dob(data["dob"])
        with db_cursor() as cur:
            cur.execute("UPDATE applications SET applicant_name = %s, dob = %s, gender = %s, "
                        "marital_status = %s, husband_name = %s, husband_employer = %s, "
                        "village = %s, block = %s, bank_account = %s, ifsc = %s "
                        "WHERE id = %s AND status = 'PENDING' AND mobile = %s",
                        (sanitize(data["applicant_name"]), dob, data["gender"],
                         data["marital_status"], sanitize(data.get("husband_name")),
                         sanitize(data.get("husband_employer")), sanitize(data["village"]),
                         sanitize(data["block"], 50), clean_account(data["bank_account"]),
                         data["ifsc"].strip().upper(), app_id, session["portal_mobile"]))
            changed = cur.rowcount
            if changed:
                # the status-portal password is the DOB, so it follows a correction
                cur.execute("UPDATE portal_users SET password_hash = %s WHERE mobile = %s",
                            (hash_password(dob.strftime("%d%m%Y")), session["portal_mobile"]))
            cur.connection.commit()
            slip_row = fetch_acknowledgment_row(cur, app_id) if changed else None
        if not changed:
            flash("This application can no longer be changed.")
            return redirect(url_for("view_application", app_id=app_id))
        invalidate_acknowledgment(app_id)
        try:
            save_acknowledgment(app_id, render_acknowledgment(slip_row))
        except Exception as e:
            app.logger.error("acknowledgment pdf for application=%d failed: %s" % (app_id, e))
        flash("Your application has been updated. Your status-portal password is your "
              "date of birth as DDMMYYYY: %s." % dob.strftime("%d%m%Y"))
        return redirect(url_for("view_application", app_id=app_id))
    return render_template("edit_application.html", data=data, blocks=BLOCKS, app_id=app_id)


@app.route("/application/<int:app_id>/cancel", methods=["POST"])
def cancel_application(app_id):
    gate = citizen_gate()
    if gate:
        return gate
    with db_cursor() as cur:
        current = own_pending_application(cur, app_id)
        if current is None:
            abort(403)
        if current is not False:
            cur.execute("UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, "
                        "decided_by = 'CITIZEN' WHERE id = %s AND status = 'PENDING' "
                        "AND mobile = %s", (now_ist(), app_id, session["portal_mobile"]))
            cur.connection.commit()
            invalidate_acknowledgment(app_id)
            flash("Your application has been withdrawn. You can submit a new application "
                  "from the home page.")
    return redirect(url_for("view_application", app_id=app_id))


@app.route("/ack/<int:app_id>.pdf")
def ack_pdf(app_id):
    path = acknowledgment_path(app_id)
    with db_cursor() as cur:
        require_application_access(cur, app_id, allow_just_submitted=True)
        if deem_if_overdue(cur, app_id):
            cur.connection.commit()
        cur.execute("SELECT decided_at FROM applications WHERE id = %s", (app_id,))
        found = cur.fetchone()
        if not found:
            abort(404)
        decided_at = found[0]
        if (os.path.exists(path) and decided_at is not None and
                os.path.getmtime(path) < decided_at.replace(tzinfo=IST).timestamp()):
            invalidate_acknowledgment(app_id)    # slip predates the decision
        slip_row = None if os.path.exists(path) else fetch_acknowledgment_row(cur, app_id)

    # Nothing below needs the database, so the connection is already back in the pool.
    if slip_row is None:
        try:
            return send_file(path, mimetype="application/pdf")
        except FileNotFoundError:                 # removed between the check and now
            with db_cursor() as cur:
                slip_row = fetch_acknowledgment_row(cur, app_id)
    data = render_acknowledgment(slip_row)
    try:
        save_acknowledgment(app_id, data)
    except OSError as e:
        app.logger.error("could not cache acknowledgment pdf application=%d: %s" % (app_id, e))
    return send_file(io.BytesIO(data), mimetype="application/pdf")


@app.route("/status/reset", methods=["GET", "POST"])
def reset_password():
    if request.method == "POST":
        mobile = request.form.get("mobile", "")
        reset_mobile = None
        with db_cursor() as cur:
            # fetch account for reset
            cur.execute("SELECT mobile FROM portal_users WHERE mobile = %s", (mobile,))
            row = cur.fetchone()
            if row:
                cur.execute("SELECT dob FROM applications WHERE mobile = %s LIMIT 1", (mobile,))
                r2 = cur.fetchone()
                if r2:
                    newpass = r2[0].strftime("%d%m%Y")
                    cur.execute("UPDATE portal_users SET password_hash = %s WHERE mobile = %s",
                                (hash_password(newpass), row[0]))
                    cur.connection.commit()
                    reset_mobile = row[0]
        if reset_mobile:
            # outside the pooled connection: the gateway call can take seconds
            send_sms(reset_mobile, "Sewa Setu: your password has been reset to your "
                                   "date of birth (DDMMYYYY).")
        flash("If the mobile number exists, the password has been reset and sent by SMS.")
    return render_template("reset.html")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if admin_credentials_valid(request.form.get("username", ""),
                                   request.form.get("password", "")):
            session.clear()
            session["logged_in"] = True
            session["admin"] = True
            session["admin_user"] = ADMIN_USERNAME
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials.")
    return render_template("admin_login.html")


@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    with db_cursor() as cur:
        cur.execute("SELECT status, count(*) FROM applications GROUP BY status")
        by_status = cur.fetchall()
        cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' "
                    "AND submitted_at < %s", (datetime.now() - timedelta(days=SLA_DAYS),))
        overdue = cur.fetchone()[0]
        cur.execute("SELECT block, count(*) FROM applications WHERE status = 'PENDING' "
                    "GROUP BY block ORDER BY count(*) DESC")
        by_block = cur.fetchall()
    return render_template("admin_dashboard.html", by_status=by_status,
                           overdue=overdue, by_block=by_block, sla=SLA_DAYS)


@app.route("/admin/applications")
def admin_list():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    status = request.args.get("status", "PENDING")
    page = min(max(request.args.get("page", 1, type=int), 1), 1000000)
    with db_cursor() as cur:
        cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                    "submitted_at FROM applications WHERE status = %s "
                    "ORDER BY submitted_at ASC LIMIT 50 OFFSET %s",
                    (status, (page - 1) * 50))
        rows = cur.fetchall()
    return render_template("admin_list.html", rows=rows, status=status, page=page)


@app.route("/admin/application/<int:app_id>")
def admin_view(app_id):
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    with db_cursor() as cur:
        cur.execute("SELECT application_no, applicant_name, mobile, dob, gender, "
                    "marital_status, village, block, bank_account, ifsc, doc_path, "
                    "status, submitted_at, decided_at, decided_by "
                    "FROM applications WHERE id = %s", (app_id,))
        row = cur.fetchone()
    if not row:
        abort(404)
    return render_template("admin_view.html", a=row, app_id=app_id)


@app.route("/admin/approve/<int:app_id>", methods=["POST"])
def admin_approve(app_id):
    if not session.get("admin"):
        abort(403)
    with db_cursor() as cur:
        cur.execute("UPDATE applications SET status = 'APPROVED', decided_at = %s, "
                    "decided_by = %s WHERE id = %s",
                    (now_ist(), session.get("admin_user", "admin"), app_id))
        cur.connection.commit()
    invalidate_acknowledgment(app_id)      # the cached slip shows the old status
    flash("Application approved.")
    return redirect(url_for("admin_list"))


@app.route("/admin/reject/<int:app_id>", methods=["POST"])
def admin_reject(app_id):
    if not session.get("admin"):
        abort(403)
    with db_cursor() as cur:
        cur.execute("UPDATE applications SET status = 'REJECTED', decided_at = %s, "
                    "decided_by = %s WHERE id = %s",
                    (now_ist(), session.get("admin_user", "admin"), app_id))
        cur.connection.commit()
    invalidate_acknowledgment(app_id)      # the cached slip shows the old status
    flash("Application rejected.")
    return redirect(url_for("admin_list"))


# ---------------------------------------------------------------------------
# Unused / legacy
# ---------------------------------------------------------------------------

def export_to_excel(rows):
    # Was used for the monthly disbursement report before eKosh integration.
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["application_no", "name", "account", "ifsc", "amount"])
    for r in rows:
        writer.writerow(list(r) + ["250"])
    return out.getvalue()


def old_payment_gateway_callback(txn):
    # retained for reference; eChallan integration was descoped in Phase 2
    status = txn.get("STATUS")
    if status == "0300":
        return "SUCCESS"
    elif status == "0399":
        return "FAILED"
    return "PENDING"


def run_startup_migrations():
    # depends_on does not wait for Postgres to accept connections, so retry.
    for attempt in range(5):
        try:
            ensure_indexes()
            healed = heal_overdue_applications()
            if healed:
                app.logger.info("startup: deemed approved %d overdue applications "
                                "(RTPS %d-day SLA)" % (healed, SLA_DAYS))
            return
        except Exception as e:
            app.logger.error("startup migration attempt %d failed: %s" % (attempt + 1, e))
            time.sleep(2)


if not os.environ.get("SEWASETU_SKIP_STARTUP_TASKS"):
    run_startup_migrations()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
