# -*- coding: utf-8 -*-
"""Shared pytest harness for Sewa Setu.

Tests run the real Flask app (``app/app.py``) in-process against a real Postgres
that already holds ``schema.sql`` + ``seed/seed.sql`` (the ``db`` service from
docker-compose, or any local Postgres restored from the same files).

Point the tests at the database with environment variables; anything unset falls
back to ``app/config/app.ini`` (host ``db``):

    SEWASETU_DB_HOST=127.0.0.1 SEWASETU_DB_PORT=5432 pytest

Safety rules for tests:
* Seeded citizens are never modified. Tests create their own citizens with
  ``make_citizen`` / ``submit_application``; every such mobile starts with ``5``
  (the seed only has 6-9) and all rows for those mobiles are deleted on teardown.
* The SMS gateway is never called; messages are captured in ``sms_outbox``.
* Uploads / acknowledgment PDFs go to a per-session temp dir, not /var/sewasetu.
"""

import io
import os
import random
import sys
from datetime import datetime, timedelta

import psycopg2
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

TEST_MOBILE_PREFIX = "5"

# app.py heals overdue applications when imported; keep tests from rewriting the
# seeded database as a side effect (tests exercise the function explicitly).
os.environ.setdefault("SEWASETU_SKIP_STARTUP_TASKS", "1")

_DB_ENV = {
    "host": "SEWASETU_DB_HOST",
    "port": "SEWASETU_DB_PORT",
    "dbname": "SEWASETU_DB_NAME",
    "user": "SEWASETU_DB_USER",
    "password": "SEWASETU_DB_PASSWORD",
}

_real_connect = psycopg2.connect


def _connect_with_env(*args, **kwargs):
    """psycopg2.connect that lets SEWASETU_DB_* override the config-file values.

    The app calls ``psycopg2.connect`` at request time, so patching it here is
    enough to redirect both the app and the test fixtures.
    """
    for key, env in _DB_ENV.items():
        if os.environ.get(env):
            kwargs[key] = os.environ[env]
    return _real_connect(*args, **kwargs)


psycopg2.connect = _connect_with_env


@pytest.fixture(scope="session")
def app_module(tmp_path_factory):
    import app as app_module  # app/app.py

    app_module.UPLOAD_DIR = str(tmp_path_factory.mktemp("uploads"))
    app_module.app.config["TESTING"] = True
    return app_module


@pytest.fixture(scope="session", autouse=True)
def _database_available(app_module):
    try:
        conn = app_module.get_db()
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM applications")
        count = cur.fetchone()[0]
        conn.close()
    except Exception as exc:  # pragma: no cover - environment problem
        pytest.exit(
            "Cannot reach the Sewa Setu database (%s). Start it with "
            "`docker compose up -d db` and set SEWASETU_DB_HOST / SEWASETU_DB_PORT, "
            "or restore app/schema.sql + seed/seed.sql into a local Postgres."
            % exc, returncode=2)
    if count < 100000:
        pytest.exit("Database is not the seeded production dump (%d applications "
                    "found, expected ~100,000)." % count, returncode=2)


@pytest.fixture
def db(app_module):
    conn = app_module.get_db()
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture
def sms_outbox(app_module, monkeypatch):
    """Capture SMS instead of calling the gateway. Entries: {"to", "text"}."""
    outbox = []

    def fake_send_sms(mobile, text):
        outbox.append({"to": mobile, "text": text})

    monkeypatch.setattr(app_module, "send_sms", fake_send_sms)
    return outbox


@pytest.fixture
def client(app_module, sms_outbox):
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def test_mobiles(app_module):
    """Registry of mobiles created by a test; all their rows are purged after."""
    created = []
    yield created
    if not created:
        return
    conn = app_module.get_db()
    cur = conn.cursor()
    for table in ("applications", "portal_users", "otps"):
        cur.execute("DELETE FROM %s WHERE mobile = ANY(%%s) AND mobile LIKE %%s"
                    % table, (created, TEST_MOBILE_PREFIX + "%"))
    conn.commit()
    conn.close()


def new_test_mobile(app_module, registry):
    for _ in range(100):
        mobile = TEST_MOBILE_PREFIX + "".join(random.choice("0123456789") for _ in range(9))
        conn = app_module.get_db()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM applications WHERE mobile = %s", (mobile,))
        taken = cur.fetchone()
        conn.close()
        if not taken and mobile not in registry:
            registry.append(mobile)
            return mobile
    raise RuntimeError("could not allocate a unique test mobile")


@pytest.fixture
def mobile_factory(app_module, test_mobiles):
    return lambda: new_test_mobile(app_module, test_mobiles)


@pytest.fixture
def make_citizen(app_module, test_mobiles):
    """Insert a citizen (application + portal login) directly into the database.

    Returns a dict with id, mobile, password (DDMMYYYY) and the other fields.
    """
    def _make(name="Test Citizen", dob=datetime(1955, 5, 20).date(),
              status="PENDING", submitted_days_ago=1, decided_by=None,
              village="Testgaon", block="Sonari", marital_status="Married",
              gender="Male", bank_account="123456789012", ifsc="SBIN0001234"):
        mobile = new_test_mobile(app_module, test_mobiles)
        submitted_at = datetime.now().replace(microsecond=0) - timedelta(days=submitted_days_ago)
        app_no = "SSP26%06d" % random.randint(0, 999999)
        password = dob.strftime("%d%m%Y")
        conn = app_module.get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO applications (application_no, applicant_name, mobile, dob, "
            "gender, marital_status, husband_name, husband_employer, village, block, "
            "bank_account, ifsc, doc_path, status, submitted_at, decided_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,'','',%s,%s,%s,%s,'',%s,%s,%s) RETURNING id",
            (app_no, name, mobile, dob, gender, marital_status, village, block,
             bank_account, ifsc, status, submitted_at, decided_by))
        app_id = cur.fetchone()[0]
        cur.execute("INSERT INTO portal_users (mobile, password_hash) VALUES (%s,%s)",
                    (mobile, app_module.hash_password(password)))
        conn.commit()
        conn.close()
        return {"id": app_id, "mobile": mobile, "password": password,
                "application_no": app_no, "name": name, "dob": dob,
                "submitted_at": submitted_at}
    return _make


def latest_otp(app_module, mobile):
    conn = app_module.get_db()
    cur = conn.cursor()
    cur.execute("SELECT code FROM otps WHERE mobile = %s ORDER BY id DESC LIMIT 1",
                (mobile,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


@pytest.fixture
def otp_for(app_module):
    return lambda mobile: latest_otp(app_module, mobile)


def default_form(**overrides):
    form = {
        "applicant_name": "Ramesh Kumar",
        "dob": "20/05/1955",
        "gender": "Male",
        "marital_status": "Married",
        "husband_name": "",
        "husband_employer": "",
        "village": "Testgaon",
        "block": "Sonari",
        "bank_account": "123456789012",
        "ifsc": "SBIN0001234",
    }
    form.update(overrides)
    return form


@pytest.fixture
def submit_application(app_module, mobile_factory, otp_for):
    """Drive the whole citizen journey over HTTP, exactly as a browser/probe would.

    /apply -> /verify (OTP read from the otps table) -> /form/1..3 -> /upload ->
    /declaration. Returns (response_of_declaration, mobile). Pass ``upload=None``
    to skip the upload step, ``mobile`` to reuse a number.
    """
    def _submit(client, mobile=None, form=None,
                upload=("age-proof.pdf", b"%PDF-1.4 test", "application/pdf")):
        mobile = mobile or mobile_factory()
        form = form or default_form()
        client.get("/apply")
        with client.session_transaction() as s:
            captcha = str(s.get("captcha_answer", ""))
        client.post("/apply", data={"mobile": mobile, "captcha": captcha})
        client.post("/verify", data={"otp": otp_for(mobile)})
        client.post("/form/1", data={k: form[k] for k in
                    ("applicant_name", "dob", "gender", "marital_status",
                     "husband_name", "husband_employer")})
        client.post("/form/2", data={k: form[k] for k in ("village", "block")})
        client.post("/form/3", data={k: form[k] for k in ("bank_account", "ifsc")})
        if upload is not None:
            fname, content, ctype = upload
            client.post("/upload", data={"document": (io.BytesIO(content), fname, ctype)},
                        content_type="multipart/form-data")
        resp = client.post("/declaration")
        return resp, mobile
    return _submit
