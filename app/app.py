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
import random
import hashlib
import hmac
import unicodedata
import configparser
import time
from datetime import datetime, date, timedelta, timezone

import requests
import psycopg2
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
    raise RuntimeError("SECRET_KEY must be set (environment or config/app.ini) "
                       "and at least 32 characters long")
app.config["PERMANENT_SESSION_LIFETIME"] = 300
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"     # blocks cross-site POSTs (CSRF)

ADMIN_USERNAME = _setting("ADMIN_USERNAME", "admin", "username")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")             # optional plaintext override
ADMIN_PASSWORD_HASH = config.get("admin", "password_hash", fallback="")

SMS_GATEWAY_URL = config.get("app", "sms_gateway_url")
OTP_VALIDITY_SECONDS = config.getint("app", "otp_validity_seconds")
UPLOAD_DIR = config.get("app", "upload_dir")
SCHEME_DEADLINE = datetime.strptime(config.get("pension", "scheme_deadline"),
                                    "%Y-%m-%d %H:%M")
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


def get_db():
    return psycopg2.connect(
        host=config.get("database", "host"),
        port=config.get("database", "port"),
        dbname=config.get("database", "name"),
        user=config.get("database", "user"),
        password=config.get("database", "password"),
    )


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


def send_sms(mobile, text):
    try:
        requests.post(SMS_GATEWAY_URL + "/api/send",
                      json={"to": mobile, "text": text}, timeout=5)
    except Exception as e:
        app.logger.error("sms gateway error: %s" % e)


def deadline_remaining():
    delta = SCHEME_DEADLINE - datetime.now()
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
            flash("Something went wrong. Please try again.")
            return render_template("apply.html", captcha_q=make_captcha())
        code = str(random.randint(100000, 999999))
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
                    (mobile, code, datetime.now()))
        conn.commit()
        cur.close(); conn.close()
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
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT code, created_at FROM otps WHERE mobile = %s "
                    "ORDER BY id DESC LIMIT 1", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
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
        data = session.get("form_data", {})
        for k, v in request.form.items():
            data[k] = v
        session["form_data"] = data
        if step < 3:
            return redirect(url_for("form_step", step=step + 1))
        return redirect(url_for("upload"))
    return render_template("form_step%d.html" % step,
                           data=session.get("form_data", {}), blocks=BLOCKS)


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        f = request.files.get("document")
        if f is None or f.filename == "":
            flash("ERR_VAL_47")
            return render_template("upload.html")
        original = f.filename.lower()
        content = f.read()
        if not original.endswith(".pdf"):
            flash("ERR_VAL_47")
            return render_template("upload.html")
        if len(content) > 102400:
            flash("ERR_VAL_47")
            return render_template("upload.html")
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        # never trust the client's filename (path traversal); a name with no
        # ASCII letters (e.g. Assamese) falls back to "document"
        safe_name = secure_filename(original[:-4]) or "document"
        path = os.path.join(UPLOAD_DIR, "%s_%s.pdf" % (session["verified_mobile"], safe_name))
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
    return render_template("declaration.html")


def handle_submission():
    # TODO: split this up some day. It grew. - RK, 09/2024
    mobile = session.get("verified_mobile")
    data = session.get("form_data", {})
    doc_path = session.get("doc_path", "")

    name = sanitize(data.get("applicant_name", ""))
    village = sanitize(data.get("village", ""))
    block = data.get("block", "").strip()
    gender = data.get("gender", "")
    marital = data.get("marital_status", "")
    husband_name = sanitize(data.get("husband_name", ""))
    husband_employer = sanitize(data.get("husband_employer", ""))
    bank_account = data.get("bank_account", "").strip()
    ifsc = data.get("ifsc", "").strip().upper()
    dob_raw = data.get("dob", "").strip()

    if not name or not village or not block or not bank_account or not dob_raw:
        flash("Something went wrong. Please try again.")
        return redirect(url_for("form_step", step=1))

    if marital == "Widowed":
        if not husband_name or not husband_employer:
            flash("Something went wrong. Please try again.")
            return redirect(url_for("form_step", step=1))

    # lenient date parsing to reduce rejections (CR-2024-117)
    dob = None
    for fmt in ("%m/%d/%Y", "%d/%m/%Y"):
        try:
            dob = datetime.strptime(dob_raw, fmt).date()
            break
        except ValueError:
            continue
    if dob is None:
        flash("Something went wrong. Please try again.")
        return redirect(url_for("form_step", step=1))

    today = date.today()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    if age < MIN_AGE:
        flash("Applicant must be above %d years of age to be eligible." % MIN_AGE)
        return redirect(url_for("form_step", step=1))

    if datetime.now() > SCHEME_DEADLINE:
        flash("The application deadline has passed.")
        return redirect(url_for("index"))

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT count(*) FROM applications WHERE mobile = %s", (mobile,))
    if cur.fetchone()[0] > 0:
        cur.close(); conn.close()
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
         datetime.now()))
    new_id = cur.fetchone()[0]

    # status portal account; password is DOB as DDMMYYYY per dept. circular
    portal_pass = dob.strftime("%d%m%Y")
    cur.execute("SELECT count(*) FROM portal_users WHERE mobile = %s", (mobile,))
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO portal_users (mobile, password_hash) VALUES (%s,%s)",
                    (mobile, hash_password(portal_pass)))
    conn.commit()

    app.logger.info("generating acknowledgment pdf application=%d" % new_id)
    pdf_bytes = generate_acknowledgment(cur, new_id)
    ack_dir = os.path.join(UPLOAD_DIR, "ack")
    os.makedirs(ack_dir, exist_ok=True)
    with open(os.path.join(ack_dir, "%d.pdf" % new_id), "wb") as out:
        out.write(pdf_bytes)

    cur.close(); conn.close()
    session.pop("form_data", None)
    session.pop("doc_path", None)
    session.pop("verified_mobile", None)
    session["just_submitted_app_id"] = new_id    # lets them fetch their own slip
    send_sms(mobile, "Sewa Setu: application %s received. Track at the status portal "
                     "with mobile no. and password (DOB as DDMMYYYY)." % app_no)
    return render_template("confirmation.html", app_no=app_no, app_id=new_id)


# Fonts for the acknowledgment slip. Helvetica (a PDF core font) is Latin-1 only,
# so Assamese/Bengali/Hindi names used to print as "?". Bundled Noto fonts cover
# Latin, Bengali script (Assamese, Bengali) and Devanagari (Hindi, Marathi, Nepali).
FONT_DIR = os.path.join(BASE_DIR, "fonts")
LATIN_FONT, BENGALI_FONT, DEVANAGARI_FONT = "NotoSans", "NotoSansBengali", "NotoSansDevanagari"
_FONT_FILES = [
    (LATIN_FONT, "", "NotoSans-Regular.ttf"),
    (LATIN_FONT, "B", "NotoSans-Bold.ttf"),
    (BENGALI_FONT, "", "NotoSansBengali-Regular.ttf"),
    (DEVANAGARI_FONT, "", "NotoSansDevanagari-Regular.ttf"),
]
_JOINERS = ("\u200c", "\u200d")       # ZWNJ / ZWJ: part of the preceding cluster
_VIRAMAS = ("\u09cd", "\u094d")


def _font_for(ch, previous):
    cp = ord(ch)
    if 0x0980 <= cp <= 0x09FF:
        return BENGALI_FONT
    if 0x0900 <= cp <= 0x097F:
        return DEVANAGARI_FONT
    if ch in _JOINERS:
        return previous
    return LATIN_FONT


def _script_runs(text):
    """Split text into (font, substring) runs, one font per run. Per-run font
    choice is used instead of fpdf2 fallback fonts, which misrender when
    combined with text shaping."""
    runs = []
    font = LATIN_FONT
    for ch in text:
        font = _font_for(ch, font)
        if runs and runs[-1][0] == font:
            runs[-1][1] += ch
        else:
            runs.append([font, ch])
    return [(f, t) for f, t in runs]


def _text_width(pdf, text, size):
    total = 0
    for font, run in _script_runs(text):
        pdf.set_font(font, "", size)
        total += pdf.get_string_width(run)
    return total


def _wrap(pdf, text, size, max_width):
    """Greedy line wrap that prefers spaces and never splits a letter cluster."""
    lines, current = [], ""
    for ch in text:
        if (current and _text_width(pdf, current + ch, size) > max_width
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


def _draw_row(pdf, label, value, size=10, line_h=8, label_w=60):
    x0, y0 = pdf.l_margin, pdf.get_y()
    value_w = pdf.w - pdf.r_margin - pdf.l_margin - label_w
    lines = _wrap(pdf, value, size, value_w - 2)
    height = line_h * len(lines)
    pdf.rect(x0, y0, label_w, height)
    pdf.rect(x0 + label_w, y0, value_w, height)
    pdf.set_font(LATIN_FONT, "", size)
    pdf.set_xy(x0 + 1, y0)
    pdf.cell(label_w - 2, line_h, label)
    for n, line in enumerate(lines):
        pdf.set_xy(x0 + label_w + 1, y0 + n * line_h)
        for font, run in _script_runs(line):
            pdf.set_font(font, "", size)
            pdf.cell(pdf.get_string_width(run), line_h, run)
    pdf.set_xy(x0, y0 + height)


def generate_acknowledgment(cur, app_id):
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, submitted_at, status FROM applications WHERE id = %s",
                (app_id,))
    row = list(cur.fetchone())
    row[9] = display_status(row[9])
    pdf = FPDF()
    for family, style, filename in _FONT_FILES:
        pdf.add_font(family, style, os.path.join(FONT_DIR, filename))
    pdf.set_text_shaping(True)     # correct Bengali/Devanagari conjuncts and vowel signs
    pdf.set_margins(15, 15, 15)
    pdf.add_page()
    pdf.set_font(LATIN_FONT, "B", 14)
    pdf.cell(0, 10, "GOVERNMENT OF PURVANCHAL", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font(LATIN_FONT, "", 11)
    pdf.cell(0, 8, "Department of Social Welfare", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.cell(0, 8, "Old Age Pension Scheme - Acknowledgment", new_x="LMARGIN", new_y="NEXT",
             align="C")
    pdf.ln(4)
    # decorative border, as per approved letterhead design
    for i in range(0, 2000):
        x = 10 + (i % 190)
        pdf.line(x, 282, x + 0.5, 282)
        pdf.line(x, 12, x + 0.5, 12)
    labels = ["Application No", "Applicant Name", "Mobile", "Date of Birth",
              "Village", "Block", "Bank Account", "IFSC", "Submitted At", "Status"]
    for label, val in zip(labels, row):
        _draw_row(pdf, label, str(val))
    pdf.ln(6)
    pdf.set_font(LATIN_FONT, "", 9)
    pdf.multi_cell(0, 5, "This is a computer generated acknowledgment. Processing SLA "
                         "as per the Purvanchal Right to Public Services Act applies.")
    return bytes(pdf.output())


# ---------------------------------------------------------------------------
# Status portal (citizen login)
# ---------------------------------------------------------------------------

@app.route("/status", methods=["GET", "POST"])
def status_login():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and safe_equals(row[0], hash_password(password)):
            session.clear()
            session["logged_in"] = True
            session["portal_mobile"] = mobile
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id FROM applications WHERE mobile = %s "
                        "ORDER BY submitted_at DESC LIMIT 1", (mobile,))
            r = cur.fetchone()
            cur.close(); conn.close()
            if r:
                return redirect(url_for("view_application", app_id=r[0]))
            flash("No application found for this mobile number.")
            return redirect(url_for("status_login"))
        flash("Something went wrong. Please try again.")
    return render_template("status_login.html")


@app.route("/application/<int:app_id>")
def view_application(app_id):
    if not session.get("logged_in"):
        flash("Please login to view application status.")
        return redirect(url_for("status_login"))
    conn = get_db()
    cur = conn.cursor()
    try:
        require_application_access(cur, app_id)
    except Exception:
        cur.close(); conn.close()
        raise
    if deem_if_overdue(cur, app_id):
        conn.commit()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, status, submitted_at, decided_at "
                "FROM applications WHERE id = %s", (app_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        abort(404)
    return render_template("application.html", a=row, app_id=app_id,
                           status_label=display_status(row[8]),
                           deemed=(row[8] == "DEEMED_APPROVED"))


@app.route("/ack/<int:app_id>.pdf")
def ack_pdf(app_id):
    conn = get_db()
    cur = conn.cursor()
    try:
        require_application_access(cur, app_id, allow_just_submitted=True)
    except Exception:
        cur.close(); conn.close()
        raise
    if deem_if_overdue(cur, app_id):
        conn.commit()
    cur.execute("SELECT decided_at FROM applications WHERE id = %s", (app_id,))
    found = cur.fetchone()
    if not found:
        cur.close(); conn.close()
        abort(404)
    path = os.path.join(UPLOAD_DIR, "ack", "%d.pdf" % app_id)
    decided_at = found[0]
    if (os.path.exists(path) and decided_at is not None and
            os.path.getmtime(path) < decided_at.replace(tzinfo=IST).timestamp()):
        # slip written at submission predates the decision (e.g. says PENDING)
        os.remove(path)
    if not os.path.exists(path):
        data = generate_acknowledgment(cur, app_id)
        cur.close(); conn.close()
        return send_file(io.BytesIO(data), mimetype="application/pdf")
    cur.close(); conn.close()
    return send_file(path, mimetype="application/pdf")


@app.route("/status/reset", methods=["GET", "POST"])
def reset_password():
    if request.method == "POST":
        mobile = request.form.get("mobile", "")
        conn = get_db()
        cur = conn.cursor()
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
                conn.commit()
                send_sms(row[0], "Sewa Setu: your password has been reset to your "
                                 "date of birth (DDMMYYYY).")
        cur.close(); conn.close()
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
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, count(*) FROM applications GROUP BY status")
    by_status = cur.fetchall()
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' "
                "AND submitted_at < %s", (datetime.now() - timedelta(days=SLA_DAYS),))
    overdue = cur.fetchone()[0]
    cur.execute("SELECT block, count(*) FROM applications WHERE status = 'PENDING' "
                "GROUP BY block ORDER BY count(*) DESC")
    by_block = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_dashboard.html", by_status=by_status,
                           overdue=overdue, by_block=by_block, sla=SLA_DAYS)


@app.route("/admin/applications")
def admin_list():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    status = request.args.get("status", "PENDING")
    page = min(max(request.args.get("page", 1, type=int), 1), 1000000)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                "submitted_at FROM applications WHERE status = %s "
                "ORDER BY submitted_at ASC LIMIT 50 OFFSET %s",
                (status, (page - 1) * 50))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_list.html", rows=rows, status=status, page=page)


@app.route("/admin/application/<int:app_id>")
def admin_view(app_id):
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, gender, "
                "marital_status, village, block, bank_account, ifsc, doc_path, "
                "status, submitted_at, decided_at, decided_by "
                "FROM applications WHERE id = %s", (app_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        abort(404)
    return render_template("admin_view.html", a=row, app_id=app_id)


@app.route("/admin/approve/<int:app_id>", methods=["POST"])
def admin_approve(app_id):
    if not session.get("admin"):
        abort(403)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = 'APPROVED', decided_at = %s, "
                "decided_by = %s WHERE id = %s",
                (now_ist(), session.get("admin_user", "admin"), app_id))
    conn.commit()
    cur.close(); conn.close()
    flash("Application approved.")
    return redirect(url_for("admin_list"))


@app.route("/admin/reject/<int:app_id>", methods=["POST"])
def admin_reject(app_id):
    if not session.get("admin"):
        abort(403)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = 'REJECTED', decided_at = %s, "
                "decided_by = %s WHERE id = %s",
                (now_ist(), session.get("admin_user", "admin"), app_id))
    conn.commit()
    cur.close(); conn.close()
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
