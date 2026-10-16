# -*- coding: utf-8 -*-
"""Fix #4 - month-end slowdown and "sorry, too many clients already".

Evidence (FORENSICS.md): on days >= 28 traffic is ~17x normal and every one of
13,759 failures is POST /declaration: Postgres runs out of connections
(`FATAL: sorry, too many clients already`) or gunicorn times out at 30 s.
Causes found in the code:

* every call opens a brand-new connection (no pool) and the error paths never
  close it, so connections leak until Postgres hits max_connections;
* the acknowledgment PDF is rendered while the request still holds its
  database connection and transaction, and its "decorative border" draws 4,000
  line segments;
* generated PDFs are never written to disk when served on demand, so each
  download re-renders;
* applications / otps have no indexes (100,000 rows scanned for every lookup);
* smsgw runs `python app.py`.
"""

import io
import os
import re
import statistics
import threading
import time

import pytest
from conftest import TEST_ADMIN_PASSWORD, TEST_ADMIN_USER

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSAMESE = "কমলাকান্ত ভট্টাচাৰ্য"
PROBE_NAME = ASSAMESE + " \u2014 PROBE_TOKEN_123"
CONCURRENCY = 50


def read(relative):
    with open(os.path.join(ROOT, relative), encoding="utf-8") as fh:
        return fh.read()


def pdf_text(data):
    from pypdf import PdfReader
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)


def citizen_client(app_module, citizen):
    client = app_module.app.test_client()
    resp = client.post("/status", data={"mobile": citizen["mobile"],
                                        "password": citizen["password"]})
    assert resp.status_code == 302
    return client


def admin_client(app_module):
    client = app_module.app.test_client()
    resp = client.post("/admin", data={"username": TEST_ADMIN_USER,
                                       "password": TEST_ADMIN_PASSWORD})
    assert resp.status_code == 302
    return client


def index_columns(conn, table):
    cur = conn.cursor()
    cur.execute("""
        SELECT array_agg(a.attname ORDER BY k.ord)
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indrelid
        CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
        WHERE c.relname = %s GROUP BY i.indexrelid""", (table,))
    columns = [tuple(r[0]) for r in cur.fetchall()]
    conn.rollback()
    return columns


def has_index(columns, *wanted):
    return any(c[:len(wanted)] == wanted for c in columns)


# ---------------------------------------------------------------------------
# indexes
# ---------------------------------------------------------------------------

def test_schema_sql_creates_the_lookup_indexes(scratch_db):
    apps = index_columns(scratch_db, "applications")
    assert has_index(apps, "mobile"), apps                    # status login, duplicate check
    assert has_index(apps, "application_no"), apps
    assert has_index(apps, "status", "submitted_at"), apps    # admin queue, SLA sweep
    assert has_index(apps, "status", "block"), apps           # dashboard by block
    assert has_index(index_columns(scratch_db, "otps"), "mobile", "id"), "otps"


def test_ensure_indexes_upgrades_an_existing_database_idempotently(app_module, scratch_db):
    """Production already has the tables: indexes must be added on startup."""
    cur = scratch_db.cursor()
    cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                "AND indexname LIKE 'idx\\_%'")
    for (name,) in cur.fetchall():
        cur.execute('DROP INDEX "%s"' % name)
    scratch_db.commit()
    assert not has_index(index_columns(scratch_db, "applications"), "mobile")

    assert hasattr(app_module, "ensure_indexes"), "app.py must expose ensure_indexes()"
    app_module.ensure_indexes()
    app_module.ensure_indexes()                               # idempotent
    apps = index_columns(scratch_db, "applications")
    assert has_index(apps, "mobile") and has_index(apps, "status", "submitted_at")
    assert has_index(index_columns(scratch_db, "otps"), "mobile", "id")


def test_ensure_indexes_is_safe_when_every_worker_runs_it_at_once(app_module, scratch_db):
    cur = scratch_db.cursor()
    cur.execute("DROP INDEX IF EXISTS idx_applications_mobile")
    scratch_db.commit()
    errors = []

    def run():
        try:
            app_module.ensure_indexes()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]    # gunicorn --workers 4
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert has_index(index_columns(scratch_db, "applications"), "mobile")


def test_the_seeded_database_answers_lookups_from_the_indexes(app_module, db):
    app_module.ensure_indexes()
    cur = db.cursor()

    def plan(sql, *args):
        cur.execute("EXPLAIN " + sql, args)
        return "\n".join(r[0] for r in cur.fetchall())

    assert "Seq Scan" not in plan("SELECT id FROM applications WHERE mobile = %s", "9861908220")
    assert "Seq Scan" not in plan("SELECT 1 FROM applications WHERE application_no = %s", "SSP25825708")
    assert "Seq Scan" not in plan(
        "SELECT id FROM applications WHERE status = %s ORDER BY submitted_at LIMIT 50", "PENDING")
    assert "Seq Scan" not in plan(
        "SELECT code FROM otps WHERE mobile = %s ORDER BY id DESC LIMIT 1", "9861908220")


# ---------------------------------------------------------------------------
# acknowledgment PDF: speed, no 4,000-line border, no DB connection while drawing
# ---------------------------------------------------------------------------

def median_ms(fn, runs=7):
    fn()                                                       # warm up
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


@pytest.mark.parametrize("name", ["Padmeswari Choudhury", ASSAMESE, PROBE_NAME])
def test_generate_acknowledgment_takes_under_50_ms(app_module, make_citizen, name):
    citizen = make_citizen(name=name)
    conn = app_module.get_db()
    try:
        cur = conn.cursor()
        elapsed = median_ms(lambda: app_module.generate_acknowledgment(cur, citizen["id"]))
    finally:
        conn.close()
    assert elapsed < 50, "median %.1f ms" % elapsed


def test_the_letterhead_border_is_two_lines_not_four_thousand(app_module, make_citizen, monkeypatch):
    import fpdf
    calls = []
    original = fpdf.FPDF.line
    monkeypatch.setattr(fpdf.FPDF, "line",
                        lambda self, *a, **k: calls.append(a) or original(self, *a, **k))
    citizen = make_citizen()
    conn = app_module.get_db()
    try:
        data = app_module.generate_acknowledgment(conn.cursor(), citizen["id"])
    finally:
        conn.close()
    assert data.startswith(b"%PDF")
    assert 1 <= len(calls) <= 4, "%d line() calls" % len(calls)


def test_pdf_is_drawn_without_holding_a_database_connection(app_module, make_citizen, monkeypatch):
    """Drawing used to happen inside the request's open transaction."""
    import fpdf
    import psycopg2
    citizen = make_citizen(name="Holds Nothing")
    client = citizen_client(app_module, citizen)
    watcher = app_module.get_db()
    watcher.autocommit = True
    cur = watcher.cursor()

    def busy_backends():
        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'")
        return cur.fetchone()[0]

    baseline = busy_backends()
    seen = []
    original = fpdf.FPDF.output
    monkeypatch.setattr(fpdf.FPDF, "output",
                        lambda self, *a, **k: seen.append(busy_backends()) or original(self, *a, **k))
    resp = client.get("/ack/%d.pdf" % citizen["id"])
    watcher.close()
    assert resp.status_code == 200 and seen, "PDF was not rendered"
    assert max(seen) <= baseline, "a transaction was open while the PDF was drawn"


# ---------------------------------------------------------------------------
# acknowledgment PDF caching
# ---------------------------------------------------------------------------

def test_ack_pdf_is_written_to_disk_and_served_from_there_next_time(app_module, make_citizen, monkeypatch):
    import fpdf
    citizen = make_citizen(name="Cache Me")
    client = citizen_client(app_module, citizen)
    path = os.path.join(app_module.UPLOAD_DIR, "ack", "%d.pdf" % citizen["id"])
    assert not os.path.exists(path)

    renders = []
    original = fpdf.FPDF.output
    monkeypatch.setattr(fpdf.FPDF, "output",
                        lambda self, *a, **k: renders.append(1) or original(self, *a, **k))
    first = client.get("/ack/%d.pdf" % citizen["id"])
    assert first.status_code == 200 and os.path.exists(path), "PDF was not cached to disk"
    second = client.get("/ack/%d.pdf" % citizen["id"])
    assert second.status_code == 200 and second.data == first.data
    assert len(renders) == 1, "the PDF was re-rendered on the second download"
    with open(path, "rb") as fh:
        assert fh.read() == first.data


def test_cached_pdf_is_never_served_to_someone_else(app_module, make_citizen):
    owner = make_citizen(name="Owner One")
    other = make_citizen(name="Other Two")
    assert citizen_client(app_module, owner).get("/ack/%d.pdf" % owner["id"]).status_code == 200
    assert os.path.exists(os.path.join(app_module.UPLOAD_DIR, "ack", "%d.pdf" % owner["id"]))
    assert citizen_client(app_module, other).get("/ack/%d.pdf" % owner["id"]).status_code == 403
    assert app_module.app.test_client().get("/ack/%d.pdf" % owner["id"]).status_code in (302, 401, 403)


def test_cached_pdf_is_refreshed_when_the_decision_changes(app_module, make_citizen):
    citizen = make_citizen(name="Awaiting Decision", status="PENDING", submitted_days_ago=1)
    client = citizen_client(app_module, citizen)
    path = os.path.join(app_module.UPLOAD_DIR, "ack", "%d.pdf" % citizen["id"])
    before = client.get("/ack/%d.pdf" % citizen["id"])
    assert "PENDING" in pdf_text(before.data) and os.path.exists(path)

    admin = admin_client(app_module)
    assert admin.post("/admin/approve/%d" % citizen["id"]).status_code == 302
    assert not os.path.exists(path), "stale cached PDF was left behind"
    after = client.get("/ack/%d.pdf" % citizen["id"])
    text = pdf_text(after.data)
    assert "APPROVED" in text and "PENDING" not in text


def test_a_slip_cached_at_submission_is_what_the_citizen_downloads(
        app_module, client, submit_application, db, monkeypatch):
    import fpdf
    resp, mobile = submit_application(client)
    cur = db.cursor()
    cur.execute("SELECT id FROM applications WHERE mobile = %s", (mobile,))
    app_id = cur.fetchone()[0]
    assert os.path.exists(os.path.join(app_module.UPLOAD_DIR, "ack", "%d.pdf" % app_id))
    renders = []
    original = fpdf.FPDF.output
    monkeypatch.setattr(fpdf.FPDF, "output",
                        lambda self, *a, **k: renders.append(1) or original(self, *a, **k))
    assert client.get("/ack/%d.pdf" % app_id).status_code == 200
    assert renders == []


# ---------------------------------------------------------------------------
# concurrency: a month-end burst must not exhaust database connections
# ---------------------------------------------------------------------------

def run_burst(app_module, make_citizen, total=CONCURRENCY):
    """total simultaneous requests: GET /status, citizen GET /ack/<id>.pdf (every
    one a cache miss) and admin GET /admin/dashboard. Returns (outcomes, peak new
    backends)."""
    per_kind = total // 3
    plan = []
    for _ in range(total - 2 * per_kind):
        plan.append((app_module.app.test_client(), "/status"))
    for i in range(per_kind):
        citizen = make_citizen(name="Burst Citizen %d" % i)
        plan.append((citizen_client(app_module, citizen), "/ack/%d.pdf" % citizen["id"]))
    for _ in range(per_kind):
        plan.append((admin_client(app_module), "/admin/dashboard"))
    assert len(plan) == total

    watcher = app_module.get_db()
    watcher.autocommit = True
    wcur = watcher.cursor()

    def backends():
        wcur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                     "AND pid <> pg_backend_pid()")
        return wcur.fetchone()[0]

    baseline, peak, stop = backends(), [0], threading.Event()

    def sample():
        while not stop.is_set():
            peak[0] = max(peak[0], backends())
            time.sleep(0.003)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()

    barrier = threading.Barrier(total)
    outcomes = []
    lock = threading.Lock()

    def fire(client, url):
        barrier.wait()
        start = time.perf_counter()
        try:
            resp = client.get(url)
            result = (url, resp.status_code, None)
        except Exception as exc:                  # a 500 surfaces as an exception here
            result = (url, 500, "%s: %s" % (type(exc).__name__, exc))
        with lock:
            outcomes.append(result + (time.perf_counter() - start,))

    threads = [threading.Thread(target=fire, args=job) for job in plan]
    [t.start() for t in threads]
    [t.join(120) for t in threads]
    stop.set()
    sampler.join()
    watcher.close()
    return outcomes, peak[0] - baseline


def test_fifty_concurrent_requests_all_succeed(app_module, make_citizen):
    outcomes, _ = run_burst(app_module, make_citizen)
    assert len(outcomes) == CONCURRENCY, "some requests never finished"
    errors = [o for o in outcomes if o[1] != 200]
    assert errors == [], errors[:3]
    assert not any("too many clients" in (o[2] or "") for o in outcomes)
    assert not any("OperationalError" in (o[2] or "") for o in outcomes)


def test_a_burst_cannot_open_more_than_the_pool_allows(app_module, make_citizen):
    """Before: one new Postgres connection per in-flight request (50 -> 34+ at once)."""
    outcomes, new_backends = run_burst(app_module, make_citizen)
    assert all(o[1] == 200 for o in outcomes)
    assert new_backends <= 20, "burst opened %d new database connections" % new_backends


def test_requests_that_fail_still_return_their_connection(app_module, make_citizen, monkeypatch):
    """45 failing requests (more than the pool holds) must not exhaust it."""
    citizen = make_citizen(name="Fails Then Works")
    client = citizen_client(app_module, citizen)
    real = app_module.deem_if_overdue

    def boom(cur, app_id):
        raise RuntimeError("simulated failure inside a database transaction")

    monkeypatch.setattr(app_module, "deem_if_overdue", boom)
    failures = 0
    for _ in range(45):
        try:
            if client.get("/application/%d" % citizen["id"]).status_code >= 500:
                failures += 1
        except RuntimeError:
            failures += 1
    assert failures == 45

    monkeypatch.setattr(app_module, "deem_if_overdue", real)
    started = time.perf_counter()
    resp = client.get("/application/%d" % citizen["id"])
    assert resp.status_code == 200, "pool was exhausted by failed requests"
    assert time.perf_counter() - started < 5


# ---------------------------------------------------------------------------
# deployment configuration
# ---------------------------------------------------------------------------

def test_postgres_allows_more_connections_than_the_default_100():
    command = [str(part) for part in __import__("yaml").safe_load(
        read("docker-compose.yml"))["services"]["db"].get("command", [])]
    assert any(re.fullmatch(r"max_connections=(\d+)", p) and int(p.split("=")[1]) >= 200
               for p in command), command


def test_app_runs_under_gunicorn_with_threads():
    cmd = read("app/Dockerfile")
    assert "gunicorn" in cmd
    assert re.search(r'"--threads",\s*"([4-9]|\d{2,})"', cmd), "gunicorn needs --threads"
    assert re.search(r'"--workers",\s*"[2-9]"', cmd)


def test_sms_gateway_serves_requests_concurrently():
    assert re.search(r"threaded\s*=\s*True", read("smsgw/app.py"))
    dockerfile = read("smsgw/Dockerfile")
    assert "gunicorn" in dockerfile
    # messages live in process memory, so there must be exactly one worker process
    assert re.search(r'"--workers",\s*"1"', dockerfile)
    assert re.search(r'"--threads",\s*"([4-9]|\d{2,})"', dockerfile)


def test_sms_gateway_message_ids_stay_unique_under_concurrent_sends():
    import importlib.util
    spec = importlib.util.spec_from_file_location("smsgw_app", os.path.join(ROOT, "smsgw", "app.py"))
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
    gateway.MESSAGES.clear()
    barrier = threading.Barrier(40)

    def send(n):
        barrier.wait()
        gateway.app.test_client().post("/api/send", json={"to": "5000000000", "text": "m%d" % n})

    threads = [threading.Thread(target=send, args=(n,)) for n in range(40)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    ids = [m["id"] for m in gateway.MESSAGES]
    assert len(ids) == 40 and len(set(ids)) == 40
