# -*- coding: utf-8 -*-
"""Fix #1 - RTPS 15-day deemed approval.

Law (DC briefing): an application untouched for more than 15 days is deemed
approved, and "an untouched file must actually be shown as approved to the
citizen". The nightly job stopped on 2026-03-15 because it still opens
config/settings.ini (merged into app.ini by CR-2026-031).

Tests that run the job or the bulk heal use a throw-away database created from
app/schema.sql, so the seeded production data is never modified.
"""

import http.server
import io
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "app", "scripts", "deemed_approval.py")
SLA_DAYS = 15


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def pdf_text(data):
    """Text of a PDF as a reader would see it (fonts are embedded subsets)."""
    from pypdf import PdfReader
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)


def login(client, citizen):
    resp = client.post("/status", data={"mobile": citizen["mobile"],
                                        "password": citizen["password"]})
    assert resp.status_code == 302, "test citizen could not log in"


def row(app_module, app_id):
    conn = app_module.get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, decided_by, decided_at FROM applications WHERE id = %s",
                (app_id,))
    result = cur.fetchone()
    conn.close()
    return result


def insert(conn, mobile, days_old, status="PENDING", decided_by=None):
    submitted = (datetime.now() - timedelta(days=days_old)).replace(microsecond=0)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO applications (application_no, applicant_name, mobile, dob, "
        "status, submitted_at, decided_by) VALUES (%s,%s,%s,'1950-01-01',%s,%s,%s) "
        "RETURNING id", ("SSP26%s" % mobile[-6:], "Citizen " + mobile, mobile, status,
                         submitted, decided_by))
    app_id = cur.fetchone()[0]
    conn.commit()
    return app_id, submitted


class SmsSink(http.server.BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        SmsSink.received.append(json.loads(body))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "queued"}')

    def log_message(self, *args):
        pass


@pytest.fixture
def sms_sink():
    SmsSink.received = []
    server = http.server.HTTPServer(("127.0.0.1", 0), SmsSink)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % server.server_port, SmsSink.received
    server.shutdown()


# ---------------------------------------------------------------------------
# 1. the nightly job
# ---------------------------------------------------------------------------

def run_job(sms_url):
    env = dict(os.environ, SEWASETU_SMS_GATEWAY_URL=sms_url)
    return subprocess.run([sys.executable, SCRIPT], env=env, cwd=ROOT,
                          capture_output=True, text=True, timeout=60)


def test_job_runs_without_missing_settings_ini(scratch_db, sms_sink):
    result = run_job(sms_sink[0])
    assert "FileNotFoundError" not in result.stderr, result.stderr
    assert "settings.ini" not in result.stderr
    assert result.returncode == 0, result.stderr
    assert "deemed approval: 0 applications approved" in result.stdout


def test_job_approves_applications_older_than_15_days(scratch_db, sms_sink):
    overdue, _ = insert(scratch_db, "5000000001", SLA_DAYS + 1)
    long_overdue, _ = insert(scratch_db, "5000000002", 120)
    inside_sla, _ = insert(scratch_db, "5000000003", SLA_DAYS - 2)
    already_rejected, _ = insert(scratch_db, "5000000004", 60, "REJECTED", "admin")

    result = run_job(sms_sink[0])
    assert result.returncode == 0, result.stderr
    assert "2 applications approved" in result.stdout

    cur = scratch_db.cursor()
    cur.execute("SELECT id, status, decided_by, decided_at FROM applications ORDER BY id")
    states = {r[0]: r[1:] for r in cur.fetchall()}
    scratch_db.rollback()
    for app_id in (overdue, long_overdue):
        status, decided_by, decided_at = states[app_id]
        assert status == "DEEMED_APPROVED"
        assert decided_by == "RTPS-AUTO"
        assert decided_at is not None
    assert states[inside_sla][0] == "PENDING"
    assert states[already_rejected][:2] == ("REJECTED", "admin")


def test_job_notifies_newly_deemed_citizens_but_not_the_historic_backlog(scratch_db, sms_sink):
    """A night's worth of new approvals gets an SMS; a months-old backlog must
    not trigger thousands of gateway calls."""
    url, received = sms_sink
    insert(scratch_db, "5000000011", SLA_DAYS + 1)      # crossed the line recently
    insert(scratch_db, "5000000012", 200)               # stuck since the outage

    result = run_job(url)
    assert result.returncode == 0, result.stderr
    assert [m["to"] for m in received] == ["5000000011"]
    assert "approved" in received[0]["text"].lower()


def test_job_is_idempotent(scratch_db, sms_sink):
    url, received = sms_sink
    app_id, _ = insert(scratch_db, "5000000021", SLA_DAYS + 1)
    assert run_job(url).returncode == 0
    sent = len(received)
    again = run_job(url)
    assert again.returncode == 0, again.stderr
    assert "0 applications approved" in again.stdout
    assert len(received) == sent == 1


# ---------------------------------------------------------------------------
# 2. bulk heal of already-overdue rows (startup migration)
# ---------------------------------------------------------------------------

def test_heal_overdue_applications_updates_backlog_without_sms(app_module, scratch_db, monkeypatch):
    assert hasattr(app_module, "heal_overdue_applications"), \
        "app.py must expose heal_overdue_applications()"
    sent = []
    monkeypatch.setattr(app_module, "send_sms", lambda *a: sent.append(a))
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: sent.append(a))

    ids = [insert(scratch_db, "50000001%02d" % i, 40 + i) for i in range(5)]
    fresh, _ = insert(scratch_db, "5000000199", 3)
    rejected, _ = insert(scratch_db, "5000000198", 90, "REJECTED", "admin")

    assert app_module.heal_overdue_applications() == 5
    assert sent == [], "healing a historic backlog must not call the SMS gateway"

    cur = scratch_db.cursor()
    for app_id, submitted in ids:
        cur.execute("SELECT status, decided_by, decided_at FROM applications WHERE id = %s",
                    (app_id,))
        status, decided_by, decided_at = cur.fetchone()
        assert (status, decided_by) == ("DEEMED_APPROVED", "RTPS-AUTO")
        assert decided_at == submitted + timedelta(days=SLA_DAYS)
    cur.execute("SELECT status FROM applications WHERE id IN (%s, %s) ORDER BY id",
                (fresh, rejected))
    assert [r[0] for r in cur.fetchall()] == ["PENDING", "REJECTED"]
    scratch_db.rollback()

    assert app_module.heal_overdue_applications() == 0   # idempotent


# ---------------------------------------------------------------------------
# 3. what the citizen sees
# ---------------------------------------------------------------------------

def test_overdue_pending_application_is_shown_as_approved_to_the_citizen(client, app_module, make_citizen):
    citizen = make_citizen(status="PENDING", submitted_days_ago=SLA_DAYS + 5)
    login(client, citizen)

    page = client.get("/application/%d" % citizen["id"])
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "APPROVED" in html
    assert "PENDING" not in html

    status, decided_by, decided_at = row(app_module, citizen["id"])
    assert (status, decided_by) == ("DEEMED_APPROVED", "RTPS-AUTO")
    assert decided_at is not None


def test_seeded_style_deemed_approved_row_reads_as_approved(client, make_citizen):
    citizen = make_citizen(status="DEEMED_APPROVED", submitted_days_ago=100,
                           decided_by="RTPS-AUTO")
    login(client, citizen)
    html = client.get("/application/%d" % citizen["id"]).get_data(as_text=True)
    assert "APPROVED" in html and "PENDING" not in html
    assert "RTPS" in html, "the citizen should be told why it is approved"


def test_application_inside_the_sla_stays_pending(client, app_module, make_citizen):
    citizen = make_citizen(status="PENDING", submitted_days_ago=SLA_DAYS - 3)
    login(client, citizen)
    html = client.get("/application/%d" % citizen["id"]).get_data(as_text=True)
    assert "PENDING" in html
    assert row(app_module, citizen["id"])[0] == "PENDING"


def test_acknowledgment_pdf_shows_overdue_application_as_approved(client, app_module, make_citizen):
    citizen = make_citizen(status="PENDING", submitted_days_ago=SLA_DAYS + 5)
    login(client, citizen)

    resp = client.get("/ack/%d.pdf" % citizen["id"])
    assert resp.status_code == 200
    text = pdf_text(resp.data)
    assert "APPROVED" in text, text
    assert "PENDING" not in text
    assert row(app_module, citizen["id"])[0] == "DEEMED_APPROVED"


def test_stale_cached_acknowledgment_is_not_served_after_deemed_approval(client, app_module, make_citizen):
    """The slip written at submission says PENDING; once the file is deemed
    approved the citizen must not keep getting the old cached PDF."""
    citizen = make_citizen(status="PENDING", submitted_days_ago=SLA_DAYS + 5)
    conn = app_module.get_db()
    stale = app_module.generate_acknowledgment(conn.cursor(), citizen["id"])
    conn.close()
    assert "PENDING" in pdf_text(stale)
    ack_dir = os.path.join(app_module.UPLOAD_DIR, "ack")
    os.makedirs(ack_dir, exist_ok=True)
    path = os.path.join(ack_dir, "%d.pdf" % citizen["id"])
    with open(path, "wb") as fh:
        fh.write(stale)
    old = (datetime.now() - timedelta(days=SLA_DAYS + 4)).timestamp()
    os.utime(path, (old, old))

    login(client, citizen)
    text = pdf_text(client.get("/ack/%d.pdf" % citizen["id"]).data)
    assert "APPROVED" in text and "PENDING" not in text, text
