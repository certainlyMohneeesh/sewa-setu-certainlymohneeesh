# -*- coding: utf-8 -*-
"""Fix #3 - citizen PII leaks and security holes.

DC briefing: "the moment you apply on the portal, you start getting phone
calls". /ack/<id>.pdf needs no login and ids are sequential, so every
applicant's name, mobile, DOB, village, bank account and IFSC can be harvested.
The same review found: any logged-in citizen can read other people's
applications (IDOR) and approve/reject applications (they check ``logged_in``
instead of ``admin``); SQL injection in /status/reset; the admin password and
the session-signing key are hard-coded and leaked in the repo; the upload path
is built from the raw client filename.

The tests log in with the credentials documented in ASSESSOR_CREDENTIALS.md, so
they also prove the credentials we hand to the assessor really work.
"""

import gc
import os
import re

import pytest
from flask import Flask

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEAKED_ADMIN_PASSWORD = "sewasetu@123"
LEAKED_SECRET_KEY = "ssp-prod-key-2023-donotshare"
BLOCKED = (301, 302, 303, 307, 308, 401, 403)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def admin_credentials():
    path = os.path.join(ROOT, "ASSESSOR_CREDENTIALS.md")
    if not os.path.exists(path):
        pytest.fail("ASSESSOR_CREDENTIALS.md is missing")
    text = open(path, encoding="utf-8").read()
    user = re.search(r"Username[^`\n]*`([^`]+)`", text)
    password = re.search(r"Password[^`\n]*`([^`]+)`", text)
    assert user and password, "ASSESSOR_CREDENTIALS.md must list Username and Password in backticks"
    return user.group(1), password.group(1)


def admin_client(app_module):
    client = app_module.app.test_client()
    user, password = admin_credentials()
    resp = client.post("/admin", data={"username": user, "password": password})
    assert resp.status_code == 302 and "/admin/dashboard" in resp.headers["Location"]
    return client


def citizen_client(app_module, citizen):
    client = app_module.app.test_client()
    resp = client.post("/status", data={"mobile": citizen["mobile"],
                                        "password": citizen["password"]})
    assert resp.status_code == 302
    return client


def status_of(app_module, app_id):
    conn = app_module.get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, decided_by FROM applications WHERE id = %s", (app_id,))
    row = cur.fetchone()
    conn.close()
    return row


# ---------------------------------------------------------------------------
# unauthenticated scraping of /ack/<id>.pdf
# ---------------------------------------------------------------------------

def test_unauthenticated_ack_pdf_of_a_seeded_citizen_is_blocked(client):
    resp = client.get("/ack/1.pdf")
    assert resp.status_code in BLOCKED
    assert not resp.data.startswith(b"%PDF")


def test_unauthenticated_ack_pdf_of_a_new_citizen_is_blocked(client, make_citizen):
    citizen = make_citizen(name="Private Person")
    resp = client.get("/ack/%d.pdf" % citizen["id"])
    assert resp.status_code in BLOCKED
    assert not resp.data.startswith(b"%PDF")


def test_ack_pdf_ids_cannot_be_enumerated_when_logged_out(client):
    """A missing id must look the same as a forbidden one."""
    assert client.get("/ack/99999999.pdf").status_code == client.get("/ack/1.pdf").status_code


# ---------------------------------------------------------------------------
# IDOR between citizens
# ---------------------------------------------------------------------------

def test_citizen_cannot_view_another_citizens_application(app_module, make_citizen):
    a = make_citizen(name="Citizen Alpha")
    b = make_citizen(name="Citizen Bravo Secret")
    client = citizen_client(app_module, a)
    resp = client.get("/application/%d" % b["id"])
    assert resp.status_code == 403
    assert "Bravo" not in resp.get_data(as_text=True)


def test_citizen_can_still_view_their_own_application(app_module, make_citizen):
    a = make_citizen(name="Citizen Alpha")
    client = citizen_client(app_module, a)
    resp = client.get("/application/%d" % a["id"])
    assert resp.status_code == 200
    assert "Citizen Alpha" in resp.get_data(as_text=True)


def test_logged_out_visitor_is_sent_to_login_for_application_page(client, make_citizen):
    citizen = make_citizen()
    resp = client.get("/application/%d" % citizen["id"])
    assert resp.status_code == 302 and "/status" in resp.headers["Location"]


def test_citizen_cannot_download_another_citizens_acknowledgment(app_module, make_citizen):
    a = make_citizen(name="Citizen Alpha")
    b = make_citizen(name="Citizen Bravo Secret")
    client = citizen_client(app_module, a)
    resp = client.get("/ack/%d.pdf" % b["id"])
    assert resp.status_code == 403
    assert not resp.data.startswith(b"%PDF")


def test_citizen_can_still_download_their_own_acknowledgment(app_module, make_citizen):
    a = make_citizen(name="Citizen Alpha")
    client = citizen_client(app_module, a)
    resp = client.get("/ack/%d.pdf" % a["id"])
    assert resp.status_code == 200 and resp.data.startswith(b"%PDF")


def test_admin_can_open_any_application_and_acknowledgment(app_module, make_citizen):
    citizen = make_citizen(name="Citizen Alpha")
    admin = admin_client(app_module)
    assert admin.get("/application/%d" % citizen["id"]).status_code == 200
    resp = admin.get("/ack/%d.pdf" % citizen["id"])
    assert resp.status_code == 200 and resp.data.startswith(b"%PDF")


# ---------------------------------------------------------------------------
# the citizen who has just applied must still get their slip
# ---------------------------------------------------------------------------

def test_citizen_who_just_submitted_can_download_their_own_acknowledgment(
        client, submit_application, db):
    resp, mobile = submit_application(client)
    assert resp.status_code == 200
    cur = db.cursor()
    cur.execute("SELECT id FROM applications WHERE mobile = %s", (mobile,))
    app_id = cur.fetchone()[0]
    assert ("/ack/%d.pdf" % app_id) in resp.get_data(as_text=True)

    pdf = client.get("/ack/%d.pdf" % app_id)
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF")


def test_just_submitted_slip_is_not_available_to_other_browsers(
        app_module, client, submit_application, db):
    resp, mobile = submit_application(client)
    cur = db.cursor()
    cur.execute("SELECT id FROM applications WHERE mobile = %s", (mobile,))
    app_id = cur.fetchone()[0]

    stranger = app_module.app.test_client()
    assert stranger.get("/ack/%d.pdf" % app_id).status_code in BLOCKED


# ---------------------------------------------------------------------------
# acting as staff without authority
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", ["approve", "reject"])
def test_citizen_cannot_approve_or_reject_applications(app_module, make_citizen, action):
    a = make_citizen(name="Citizen Alpha")
    b = make_citizen(name="Citizen Bravo", status="PENDING", submitted_days_ago=1)
    client = citizen_client(app_module, a)
    resp = client.post("/admin/%s/%d" % (action, b["id"]))
    assert resp.status_code == 403
    assert status_of(app_module, b["id"]) == ("PENDING", None)


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_logged_out_visitor_cannot_approve_or_reject(client, app_module, make_citizen, action):
    b = make_citizen(status="PENDING", submitted_days_ago=1)
    assert client.post("/admin/%s/%d" % (action, b["id"])).status_code == 403
    assert status_of(app_module, b["id"]) == ("PENDING", None)


def test_citizen_cannot_open_admin_pages(app_module, make_citizen):
    a = make_citizen()
    client = citizen_client(app_module, a)
    for url in ("/admin/dashboard", "/admin/applications", "/admin/application/%d" % a["id"]):
        resp = client.get(url)
        assert resp.status_code in BLOCKED, url


def test_admin_can_approve_and_reject_and_the_decision_is_attributed(app_module, make_citizen):
    user, _ = admin_credentials()
    to_approve = make_citizen(status="PENDING", submitted_days_ago=1)
    to_reject = make_citizen(status="PENDING", submitted_days_ago=1)
    admin = admin_client(app_module)
    assert admin.post("/admin/approve/%d" % to_approve["id"]).status_code == 302
    assert admin.post("/admin/reject/%d" % to_reject["id"]).status_code == 302
    assert status_of(app_module, to_approve["id"]) == ("APPROVED", user)
    assert status_of(app_module, to_reject["id"]) == ("REJECTED", user)


def test_admin_list_survives_bad_page_numbers(app_module):
    admin = admin_client(app_module)
    for page in ("abc", "-3", "0", "1' OR '1'='1", "99999999999999999999"):
        resp = admin.get("/admin/applications", query_string={"page": page})
        assert resp.status_code == 200, page


# ---------------------------------------------------------------------------
# SQL injection
# ---------------------------------------------------------------------------

def test_reset_password_sql_injection_cannot_reset_another_account(
        app_module, make_citizen, client, sms_outbox, db):
    victim = make_citizen(name="Victim")
    cur = db.cursor()
    custom = app_module.hash_password("a-password-the-victim-chose")
    cur.execute("UPDATE portal_users SET password_hash = %s WHERE mobile = %s",
                (custom, victim["mobile"]))
    db.commit()

    payload = "zzz' OR mobile = '%s' --" % victim["mobile"]
    resp = client.post("/status/reset", data={"mobile": payload})
    assert resp.status_code == 200

    cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (victim["mobile"],))
    assert cur.fetchone()[0] == custom
    assert sms_outbox == []


@pytest.mark.parametrize("payload", ["' OR '1'='1", "'"])
def test_reset_password_rejects_injection_payloads_quietly(client, sms_outbox, payload):
    resp = client.post("/status/reset", data={"mobile": payload})
    assert resp.status_code == 200
    assert sms_outbox == []


def test_reset_password_does_not_run_stacked_statements(
        app_module, make_citizen, client, db):
    victim = make_citizen(name="Victim")
    payload = ("x'; UPDATE portal_users SET password_hash = 'pwned' "
               "WHERE mobile = '%s'; --" % victim["mobile"])
    try:
        client.post("/status/reset", data={"mobile": payload})
    except Exception:
        pass        # the unfixed app crashes mid-transaction and leaks its connection
    gc.collect()    # release it so its uncommitted UPDATE cannot lock the row
    cur = db.cursor()
    cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (victim["mobile"],))
    assert cur.fetchone()[0] != "pwned"


def test_reset_password_still_works_for_a_real_citizen(
        app_module, client, make_citizen, sms_outbox, db):
    citizen = make_citizen(name="Forgetful")
    cur = db.cursor()
    cur.execute("UPDATE portal_users SET password_hash = %s WHERE mobile = %s",
                (app_module.hash_password("something-else"), citizen["mobile"]))
    db.commit()
    resp = client.post("/status/reset", data={"mobile": citizen["mobile"]})
    assert resp.status_code == 200
    cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (citizen["mobile"],))
    assert cur.fetchone()[0] == app_module.hash_password(citizen["password"])
    assert [m["to"] for m in sms_outbox] == [citizen["mobile"]]


def test_status_login_is_not_injectable(client, sms_outbox):
    resp = client.post("/status", data={"mobile": "' OR '1'='1", "password": "x"})
    assert resp.status_code == 200
    with client.session_transaction() as s:
        assert not s.get("logged_in")


# ---------------------------------------------------------------------------
# default / leaked credentials
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("username", ["admin", "administrator", "root"])
def test_leaked_default_admin_password_is_rejected(client, username):
    resp = client.post("/admin", data={"username": username,
                                       "password": LEAKED_ADMIN_PASSWORD})
    assert resp.status_code == 200
    assert "Invalid credentials" in resp.get_data(as_text=True)
    with client.session_transaction() as s:
        assert not s.get("admin") and not s.get("logged_in")
    assert client.get("/admin/dashboard").status_code == 302


def test_documented_assessor_credentials_log_in(app_module):
    admin = admin_client(app_module)
    assert admin.get("/admin/dashboard").status_code == 200
    assert admin.get("/admin/applications").status_code == 200


def test_wrong_password_for_the_documented_user_is_rejected(client):
    user, password = admin_credentials()
    resp = client.post("/admin", data={"username": user, "password": password + "x"})
    assert "Invalid credentials" in resp.get_data(as_text=True)


def test_session_forged_with_the_leaked_secret_key_is_not_accepted(client):
    forger = Flask("forger")
    forger.secret_key = LEAKED_SECRET_KEY
    cookie = forger.session_interface.get_signing_serializer(forger).dumps(
        {"logged_in": True, "admin": True})
    client.set_cookie("localhost", "session", cookie)
    resp = client.get("/admin/dashboard")
    assert resp.status_code == 302 and "/admin" in resp.headers["Location"]


def test_leaked_secrets_are_gone_from_the_repository():
    for relative in ("app/app.py", "app/config/app.ini"):
        text = open(os.path.join(ROOT, relative), encoding="utf-8").read()
        assert LEAKED_SECRET_KEY not in text, relative
        assert LEAKED_ADMIN_PASSWORD not in text, relative


def test_session_cookie_is_httponly_and_samesite(client, make_citizen):
    citizen = make_citizen()
    resp = client.post("/status", data={"mobile": citizen["mobile"],
                                        "password": citizen["password"]})
    cookie = resp.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


# ---------------------------------------------------------------------------
# uploads
# ---------------------------------------------------------------------------

def test_upload_filename_cannot_escape_the_upload_directory(
        app_module, client, submit_application, db):
    outside = os.path.join(os.path.dirname(app_module.UPLOAD_DIR), "evil.pdf")
    resp, mobile = submit_application(
        client, upload=("../../evil.pdf", b"%PDF-1.4 x", "application/pdf"))
    assert resp.status_code == 200
    cur = db.cursor()
    cur.execute("SELECT doc_path FROM applications WHERE mobile = %s", (mobile,))
    doc_path = cur.fetchone()[0]
    assert os.path.dirname(os.path.realpath(doc_path)) == os.path.realpath(app_module.UPLOAD_DIR)
    assert os.path.exists(doc_path)
    assert not os.path.exists(outside)


# ---------------------------------------------------------------------------
# the SMS gateway console must stay public (the assessor reads OTPs from it)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/__gateway/", "/__gateway/api/messages?to=9999999999"])
def test_gateway_proxy_requires_no_login(client, path):
    resp = client.get(path)
    assert resp.status_code not in BLOCKED     # 200, or 502 when no gateway is running
