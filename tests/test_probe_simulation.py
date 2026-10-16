# -*- coding: utf-8 -*-
"""End-to-end simulation of the assessor's probe (BRIEF.md), over HTTP only.

Runs against a *deployed* stack - by default the docker-compose one on
http://localhost:8000 - and needs nothing but that URL:

    docker compose up -d --build
    PROBE_BASE_URL=http://localhost:8000 pytest tests/test_probe_simulation.py

Admin credentials are read from PROBE_ADMIN_USER / PROBE_ADMIN_PASSWORD, else from
the untracked .env file at the repository root (ADMIN_USERNAME / ADMIN_PASSWORD).
Tests that need them are skipped when neither exists. The suite never touches the
database directly; pre-existing citizens are sampled through the admin panel.

Probe citizens use mobile numbers starting with 5 (the seed only has 6-9) and are
left in place; remove them with:  DELETE FROM applications WHERE mobile LIKE '5%'.
"""

import concurrent.futures
import io
import os
import random
import re
import subprocess
import time
from datetime import datetime, timedelta

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("PROBE_BASE_URL", "http://localhost:8000").rstrip("/")
MB = 1024 * 1024
TOKEN = "PROBE_TOKEN_%d" % random.randint(100, 999)
ASSAMESE_NAME = "কমলাকান্ত ভট্টাচাৰ্য — " + TOKEN

# The vendor's login and signing key leaked in the original repository; both must
# be dead on the live site.
LEAKED_LOGIN = ("admin", "sewasetu@123")
LEAKED_SIGNING_KEY = "ssp-prod-key-2023-donotshare"

GENERIC_ERROR = "Something went wrong"
CRYPTIC_UPLOAD_ERROR = "ERR_VAL_47"


def _dotenv():
    values = {}
    try:
        with open(os.path.join(ROOT, ".env"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    values[key.strip()] = value.strip().strip("\"'")
    except OSError:
        pass
    return values


def admin_login_details():
    env = _dotenv()
    user = os.environ.get("PROBE_ADMIN_USER") or env.get("ADMIN_USERNAME")
    secret = os.environ.get("PROBE_ADMIN_PASSWORD") or env.get("ADMIN_PASSWORD")
    return (user, secret) if user and secret else None


@pytest.fixture(scope="module", autouse=True)
def _stack_is_up():
    try:
        requests.get(BASE + "/", timeout=5)
    except requests.RequestException as exc:
        pytest.skip("no deployment reachable at %s (%s)" % (BASE, exc))


# --------------------------------------------------------------------------- helpers

class Browser(object):
    """A cookie-keeping HTTP client, like one person's browser."""

    def __init__(self):
        self.s = requests.Session()

    def get(self, path, follow=True, **kw):
        return self.s.get(BASE + path, allow_redirects=follow, timeout=30, **kw)

    def post(self, path, follow=True, **kw):
        return self.s.post(BASE + path, allow_redirects=follow, timeout=30, **kw)


def new_mobile():
    return "5" + "".join(random.choice("0123456789") for _ in range(9))


def inbox(mobile):
    return requests.get(BASE + "/__gateway/api/messages", params={"to": mobile},
                        timeout=10).json()["messages"]


def read_otp(mobile, ignore_ids=(), timeout=20):
    """What the citizen would do: watch the SMS inbox until the new OTP is delivered
    (a number that asked before also has older OTPs in its inbox)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for m in reversed(inbox(mobile)):
            if m["id"] in ignore_ids or not (m["delivered"] and m["text"]):
                continue
            found = re.search(r"OTP is (\d{6})", m["text"])
            if found:
                return found.group(1)
        time.sleep(0.5)
    raise AssertionError("OTP for %s was not delivered within %ds" % (mobile, timeout))


def captcha_answer(html):
    found = re.search(r"What is (\d+) \+ (\d+)\?", html)
    return str(int(found.group(1)) + int(found.group(2))) if found else ""


def start_application(b, mobile):
    """/apply -> OTP -> /verify. Returns the response of /verify."""
    earlier = {m["id"] for m in inbox(mobile)}
    page = b.get("/apply")
    sent = b.post("/apply", data={"mobile": mobile, "captcha": captcha_answer(page.text)},
                  follow=False)
    assert sent.status_code == 302, sent.text[:300]
    return b.post("/verify", data={"otp": read_otp(mobile, earlier)})


def form(**overrides):
    values = {"applicant_name": "Ramesh Kumar", "dob": "05/10/1960", "gender": "Male",
              "marital_status": "Married", "husband_name": "", "husband_employer": "",
              "village": "Probegaon", "block": "Sonari", "bank_account": "123456789012",
              "ifsc": "SBIN0001234"}
    values.update(overrides)
    return values


STEP_FIELDS = {
    1: ("applicant_name", "dob", "gender", "marital_status", "husband_name", "husband_employer"),
    2: ("village", "block"),
    3: ("bank_account", "ifsc"),
}


def post_step(b, step, values, follow=True):
    return b.post("/form/%d" % step, data={k: values[k] for k in STEP_FIELDS[step]},
                  follow=follow)


def upload(b, filename, content, follow=True):
    return b.post("/upload", files={"document": (filename, io.BytesIO(content))},
                  follow=follow)


def submit_application(mobile=None, values=None, doc=("age-proof.pdf", b"%PDF-1.4 probe")):
    """The whole citizen journey. Returns (browser, mobile, application id, values)."""
    mobile, values = mobile or new_mobile(), values or form()
    b = Browser()
    start_application(b, mobile)
    for step in (1, 2, 3):
        post_step(b, step, values)
    if doc:
        upload(b, *doc)
    done = b.post("/declaration")
    found = re.search(r"/ack/(\d+)\.pdf", done.text)
    assert found, "application was not accepted: %s" % re.sub(r"\s+", " ", done.text)[:600]
    return b, mobile, int(found.group(1)), values


def citizen_login(mobile, secret):
    b = Browser()
    r = b.post("/status", data={"mobile": mobile, "password": secret}, follow=False)
    return b, r


def admin_browser():
    creds = admin_login_details()
    if not creds:
        pytest.skip("admin credentials not available (PROBE_ADMIN_* or .env)")
    b = Browser()
    r = b.post("/admin", data={"username": creds[0], "password": creds[1]}, follow=False)
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin/dashboard"), \
        "admin login failed (%s)" % r.status_code
    return b


def visible_text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


ROW = re.compile(r"<tr>\s*<td>(\d+)</td><td>(\w+)</td><td>(.*?)</td><td>(\d+)</td>\s*"
                 r"<td>(.*?)</td><td>(\w+)</td><td>(.*?)</td>", re.S)


def admin_rows(admin, status, page=1):
    html = admin.get("/admin/applications?status=%s&page=%d" % (status, page)).text
    return [{"id": int(m[0]), "no": m[1], "name": m[2], "mobile": m[3], "block": m[4],
             "status": m[5], "submitted": m[6]} for m in ROW.findall(html)]


def admin_dob(admin, app_id):
    html = admin.get("/admin/application/%d" % app_id).text
    found = re.search(r"Date of Birth</th><td>(\d{4})-(\d{2})-(\d{2})</td>", html)
    return "%s%s%s" % (found.group(3), found.group(2), found.group(1)) if found else None


def admin_status(admin, app_id):
    html = admin.get("/admin/application/%d" % app_id).text
    return re.search(r"Status</th><td><b>(\w+)</b>", html).group(1)


# --------------------------------------------------------------------------- 1. liveness

@pytest.mark.parametrize("path", ["/", "/about", "/apply", "/status", "/__gateway/",
                                  "/__gateway/api/messages?to=9999999999", "/admin"])
def test_liveness(path):
    assert requests.get(BASE + path, timeout=15).status_code == 200


def test_citizen_pages_ship_their_stylesheet_and_script():
    page = requests.get(BASE + "/apply", timeout=15).text
    assert "/static/portal.css" in page and "/static/portal.js" in page
    assert requests.get(BASE + "/static/portal.css", timeout=15).status_code == 200
    assert requests.get(BASE + "/static/portal.js", timeout=15).status_code == 200


def test_gateway_stays_open_without_login_or_throttling():
    codes = {requests.get(BASE + "/__gateway/api/messages", timeout=10).status_code
             for _ in range(30)}
    assert codes == {200}


# --------------------------------------------------------------------------- 2. security

def test_anonymous_cannot_download_any_acknowledgment():
    for app_id in (1, 2, 50000, 100000):
        r = requests.get("%s/ack/%d.pdf" % (BASE, app_id), allow_redirects=False, timeout=15)
        assert r.status_code in (302, 401, 403), (app_id, r.status_code)
        assert b"%PDF" not in r.content[:8]


def test_anonymous_cannot_read_an_application():
    r = requests.get(BASE + "/application/1", allow_redirects=False, timeout=15)
    assert r.status_code in (302, 401, 403)


def test_citizen_cannot_read_another_citizens_application_or_slip():
    _, mobile_a, id_a, _ = submit_application(values=form(applicant_name="Citizen Alpha"))
    _, mobile_b, id_b, _ = submit_application(
        values=form(applicant_name="Citizen Bravo", dob="07/03/1958"))
    alpha, _ = citizen_login(mobile_a, "05101960")
    bravo, _ = citizen_login(mobile_b, "07031958")
    own_a = alpha.get("/application/%d" % id_a, follow=False)
    own_b = bravo.get("/application/%d" % id_b, follow=False)
    assert own_a.status_code == 200 and own_b.status_code == 200
    assert alpha.get("/ack/%d.pdf" % id_a, follow=False).status_code == 200
    for path in ("/application/%d" % id_b, "/ack/%d.pdf" % id_b):
        r = alpha.get(path, follow=False)
        assert r.status_code == 403, (path, r.status_code)
        assert "Citizen Bravo" not in r.text
    for path in ("/application/%d" % id_a, "/ack/%d.pdf" % id_a):
        assert bravo.get(path, follow=False).status_code == 403


def test_citizen_cannot_act_as_staff():
    _, victim_mobile, victim_id, _ = submit_application(
        values=form(applicant_name="Staff Test Victim"))
    victim_browser, _ = citizen_login(victim_mobile, "05101960")   # a logged-in citizen
    for action in ("approve", "reject"):
        r = victim_browser.post("/admin/%s/%d" % (action, victim_id), follow=False)
        assert r.status_code == 403, (action, r.status_code)
    assert requests.post("%s/admin/approve/%d" % (BASE, victim_id), allow_redirects=False,
                         timeout=15).status_code == 403
    assert victim_browser.get("/admin/dashboard", follow=False).status_code in (302, 403)
    if admin_login_details():
        assert admin_status(admin_browser(), victim_id) == "PENDING"


def test_sql_injection_on_reset_and_login_is_harmless():
    b, mobile, app_id, _ = submit_application(values=form(applicant_name="Injection Target"))
    other = Browser()
    for payload in ("' OR '1'='1", "x' OR mobile LIKE '5%' --", "'; DROP TABLE portal_users; --"):
        r = other.post("/status/reset", data={"mobile": payload})
        assert r.status_code == 200
        assert "Traceback" not in r.text and "psycopg2" not in r.text
        _, r2 = citizen_login(payload, payload)
        assert r2.status_code == 200 and "/application/" not in r2.headers.get("Location", "")
    # the victim's DOB password is untouched and the tables still exist
    _, ok = citizen_login(mobile, "05101960")
    assert ok.status_code == 302


def test_leaked_default_admin_login_is_rejected():
    r = requests.post(BASE + "/admin", data={"username": LEAKED_LOGIN[0],
                                             "password": LEAKED_LOGIN[1]},
                      allow_redirects=False, timeout=15)
    assert r.status_code == 200 and "Invalid credentials" in r.text


def test_rotated_admin_login_works_and_panel_is_reachable():
    admin = admin_browser()
    for path in ("/admin/dashboard", "/admin/applications", "/admin/applications?status=APPROVED",
                 "/admin/applications?status=DEEMED_APPROVED"):
        assert admin.get(path).status_code == 200, path


def test_cookie_forged_with_the_leaked_signing_key_is_worthless():
    from flask import Flask
    from flask.sessions import SecureCookieSessionInterface
    fake = Flask(__name__)
    fake.secret_key = LEAKED_SIGNING_KEY
    cookie = SecureCookieSessionInterface().get_signing_serializer(fake).dumps(
        {"logged_in": True, "admin": True, "admin_user": "admin"})
    r = requests.get(BASE + "/admin/dashboard", cookies={"session": cookie},
                     allow_redirects=False, timeout=15)
    assert r.status_code in (302, 403)


def test_path_traversal_in_upload_filename_is_contained():
    mobile = new_mobile()
    b = Browser()
    start_application(b, mobile)
    for step in (1, 2, 3):
        post_step(b, step, form())
    r = upload(b, "../../../../tmp/evil.pdf", b"%PDF-1.4", follow=False)
    assert r.status_code == 302 and r.headers["Location"].endswith("/declaration")


# --------------------------------------------------------------------------- 3. data integrity

def test_indic_name_round_trips_and_acknowledgment_is_a_real_pdf():
    values = form(applicant_name=ASSAMESE_NAME, dob="05/10/1960")
    b, mobile, app_id, _ = submit_application(values=values)

    # the confirmation page offered the slip, and the citizen can download it at once
    slip = b.get("/ack/%d.pdf" % app_id)
    assert slip.status_code == 200 and slip.content.startswith(b"%PDF")
    assert len(slip.content) > 2000

    # DOB 05/10/1960 means the status password is 05101960 (not 10051960)
    citizen, r = citizen_login(mobile, "05101960")
    assert r.status_code == 302 and "/application/%d" % app_id in r.headers["Location"]
    page = citizen.get("/application/%d" % app_id).text
    assert ASSAMESE_NAME in page, "the name must come back whole, not cut or '?'-ed"
    assert "1960-10-05" in page
    wrong, r = citizen_login(mobile, "10051960")
    assert "/application/" not in r.headers.get("Location", "")

    # the stored slip text holds the name and no question-mark fallback
    from pypdf import PdfReader
    text = "".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(
        citizen.get("/ack/%d.pdf" % app_id).content)).pages)
    assert TOKEN in text and "????" not in text


def test_otp_and_confirmation_sms_arrive_through_the_gateway():
    b, mobile, app_id, _ = submit_application()
    deadline, texts = time.time() + 15, []
    while time.time() < deadline:
        msgs = requests.get(BASE + "/__gateway/api/messages", params={"to": mobile},
                            timeout=10).json()["messages"]
        texts = [m["text"] for m in msgs if m["delivered"]]
        if any("received" in t for t in texts):
            break
        time.sleep(1)
    assert any("OTP" in t for t in texts) and any("received" in t for t in texts), texts


# --------------------------------------------------------------------------- 4. citizen journeys

def test_confused_citizen_is_told_exactly_what_to_fix():
    shown = []
    b = Browser()

    page = b.get("/apply")
    r = b.post("/apply", data={"mobile": "123", "captcha": captcha_answer(page.text)})
    shown.append(visible_text(r.text))
    assert "valid 10-digit mobile number" in r.text

    mobile = new_mobile()
    page = b.get("/apply")
    wrong = str(int(captcha_answer(page.text)) + 1)
    r = b.post("/apply", data={"mobile": mobile, "captcha": wrong})
    shown.append(visible_text(r.text))
    assert "Incorrect security check" in r.text

    start_application(b, mobile)

    r = post_step(b, 1, form(applicant_name=""))
    assert "Please enter your full name." in r.text
    r = post_step(b, 1, form(dob="not-a-date"))
    assert "valid Date of Birth in DD/MM/YYYY" in r.text
    r = post_step(b, 1, form(dob="01/01/2000"))
    assert "at least 60 years of age" in r.text
    shown.append(visible_text(r.text))
    r = post_step(b, 1, form(gender=""))
    assert "select your gender and marital status" in r.text
    r = post_step(b, 2, form(village=""))
    assert "village/town and select your block" in r.text
    r = post_step(b, 3, form(ifsc="X"))
    assert "valid Bank Account Number and IFSC" in r.text

    # a widow with nothing to put under "husband's employer" must get through
    values = form(applicant_name="Sita Devi", gender="Female", marital_status="Widowed",
                  husband_name="", husband_employer="", dob="12/01/1966")
    for step in (1, 2, 3):
        r = post_step(b, step, values, follow=False)
        assert r.status_code == 302, (step, visible_text(r.text)[:300])

    exe = upload(b, "setup.exe", b"MZ" + b"\0" * 500)
    big = upload(b, "scan.pdf", b"x" * (5 * MB + 1024))
    shown += [visible_text(exe.text), visible_text(big.text)]
    assert "Invalid file format" in exe.text
    assert "5 MB" in big.text and "File size exceeds" in big.text
    nothing = b.post("/upload", data={})
    assert "select an age proof document" in nothing.text
    shown.append(visible_text(nothing.text))

    jpg = upload(b, "photo.jpg", b"\xff\xd8\xff" + b"x" * (500 * 1024), follow=False)
    assert jpg.status_code == 302 and jpg.headers["Location"].endswith("/declaration")

    review = b.get("/declaration").text
    assert "Sita Devi" in review and 'href="/form/1"' in review

    done = b.post("/declaration")
    assert re.search(r"/ack/\d+\.pdf", done.text), visible_text(done.text)[:400]
    shown.append(visible_text(done.text))

    for text in shown:
        assert CRYPTIC_UPLOAD_ERROR not in text and GENERIC_ERROR not in text
    # 12/01/1966 was read as 12 January 1966, so the password is 12011966
    _, r = citizen_login(mobile, "12011966")
    assert r.status_code == 302


def test_fix_a_typo_then_withdraw_and_apply_again():
    b, mobile, app_id, _ = submit_application(values=form(applicant_name="Ramesh Kumr"))
    c, r = citizen_login(mobile, "05101960")
    assert r.status_code == 302

    page = c.get("/application/%d" % app_id).text
    assert "/application/%d/edit" % app_id in page and "/application/%d/cancel" % app_id in page

    r = c.post("/application/%d/edit" % app_id, data=form(
        applicant_name="Ramesh Kumar", dob="06/10/1960"))
    assert r.status_code == 200 and "Ramesh Kumar" in r.text and "Ramesh Kumr" not in r.text
    _, ok = citizen_login(mobile, "06101960")
    assert ok.status_code == 302

    r = c.post("/application/%d/cancel" % app_id)
    assert "WITHDRAWN" in r.text

    again, _, new_id, _ = submit_application(
        mobile=mobile, values=form(applicant_name="Ramesh Kumar", dob="07/08/1959"))
    assert new_id != app_id
    _, ok = citizen_login(mobile, "07081959")
    assert ok.status_code == 302 and ok.headers["Location"].endswith("/application/%d" % new_id)


def test_second_active_application_for_a_mobile_is_still_refused():
    b, mobile, app_id, _ = submit_application()
    second = Browser()
    start_application(second, mobile)
    for step in (1, 2, 3):
        post_step(second, step, form())
    upload(second, "age-proof.pdf", b"%PDF-1.4")
    r = second.post("/declaration")
    assert "already exists" in r.text


# --------------------------------------------------------------------------- 5. load

def test_month_end_spike_is_survived():
    b, mobile, app_id, _ = submit_application()
    citizen, _ = citizen_login(mobile, "05101960")
    admin = admin_browser()
    assert citizen.get("/ack/%d.pdf" % app_id).status_code == 200      # warm the slip cache

    jobs = (["/"] * 40) + (["/status"] * 20) + (["ack"] * 20) + (["dash"] * 20)
    random.shuffle(jobs)

    def hit(job):
        started = time.perf_counter()
        try:
            if job == "ack":
                r = citizen.get("/ack/%d.pdf" % app_id)
            elif job == "dash":
                r = admin.get("/admin/dashboard")
            else:
                r = requests.get(BASE + job, timeout=30)
            status, body = r.status_code, r.content[:4000]
        except requests.RequestException as exc:
            status, body = 599, str(exc).encode()
        return job, status, body, (time.perf_counter() - started) * 1000

    with concurrent.futures.ThreadPoolExecutor(max_workers=25) as pool:
        results = list(pool.map(hit, jobs))

    server_errors = [(j, s) for j, s, _, _ in results if s >= 500]
    assert server_errors == []
    assert not any(b"too many clients" in body or b"OperationalError" in body
                   for _, _, body, _ in results)
    latencies = sorted(ms for _, _, _, ms in results)
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    assert p95 < 500, "p95 latency %.0f ms (max %.0f ms)" % (p95, latencies[-1])


# --------------------------------------------------------------------------- 6. citizens who existed before

def _sample_deemed_citizens(admin, wanted=6):
    sample = []
    for page in (200, 220, 240, 255, 265, 270, 275, 150, 100):
        for row in admin_rows(admin, "DEEMED_APPROVED", page):
            if re.match(r"2026-0[3-6]-", row["submitted"]):
                sample.append(row)
        if len(sample) >= wanted:
            break
    return sample[:wanted]


def test_overdue_applications_from_march_to_june_show_as_approved_to_the_citizen():
    admin = admin_browser()
    sample = _sample_deemed_citizens(admin)
    assert len(sample) >= 3, "could not find healed March-June applications to sample"
    for row in sample:
        dob = admin_dob(admin, row["id"])
        assert dob, row
        citizen, r = citizen_login(row["mobile"], dob)
        assert r.status_code == 302, "pre-existing citizen %s cannot log in" % row["mobile"]
        page = citizen.get("/application/%d" % row["id"]).text
        assert "APPROVED" in page and ">PENDING<" not in page, row
        assert row["name"] in page
        assert citizen.get("/ack/%d.pdf" % row["id"]).content.startswith(b"%PDF")


def test_no_pending_application_is_older_than_the_15_day_deadline():
    admin = admin_browser()
    cutoff = datetime.utcnow() + timedelta(hours=5, minutes=30) - timedelta(days=15)
    page, stale = 1, []
    while page <= 20:
        rows = admin_rows(admin, "PENDING", page)
        if not rows:
            break
        stale += [r for r in rows
                  if datetime.strptime(r["submitted"][:19], "%Y-%m-%d %H:%M:%S") < cutoff]
        page += 1
    assert stale == [], "overdue PENDING rows still shown: %s" % stale[:3]


def test_pending_citizens_and_manually_approved_citizens_can_log_in():
    admin = admin_browser()
    sampled = 0
    for status in ("APPROVED", "REJECTED", "PENDING"):
        for row in admin_rows(admin, status, 3 if status != "PENDING" else 1)[:3]:
            dob = admin_dob(admin, row["id"])
            citizen, r = citizen_login(row["mobile"], dob)
            assert r.status_code == 302, (status, row["mobile"])
            assert row["name"] in citizen.get("/application/%d" % row["id"]).text
            sampled += 1
    assert sampled >= 6


# --------------------------------------------------------------------------- 7. repository

def _git(*args):
    return subprocess.run(["git"] + list(args), cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout.strip()


def test_history_is_linear_and_every_fix_follows_its_failing_test():
    assert _git("log", "--merges", "--oneline") == "", "history must be linear"
    subjects = _git("log", "--reverse", "--format=%s").splitlines()
    fixes = [i for i, s in enumerate(subjects) if s.startswith("fix:")]
    assert len(fixes) >= 5
    for i in fixes:
        assert i > 0 and subjects[i - 1].startswith("test:"), \
            "%r is not directly preceded by a test: commit (%r)" % (subjects[i], subjects[i - 1])


def test_ai_instructions_and_prompts_are_committed():
    tracked = _git("ls-files").splitlines()
    assert "CLAUDE.md" in tracked and "PROMPT.md" in tracked
