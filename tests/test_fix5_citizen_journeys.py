# -*- coding: utf-8 -*-
"""Fix #5 - citizen form traps, DOB/password corruption, self-service corrections.

Problems reproduced here (all happened to real citizens):

* The form says DD/MM/YYYY but the code tried MM/DD/YYYY first, so 05/10/1960
  was stored as 10 May 1960 and the status-portal password (DOB as DDMMYYYY)
  became 10051960 - the citizen could never log in.
* Widows were rejected for a blank "husband's current employer".
* The upload page promises "JPG or PDF, max 5 MB" but the code accepted only
  PDFs up to 100 KB and answered every failure with the code "ERR_VAL_47".
* Every other mistake produced "Something went wrong. Please try again.", and
  was only reported on the last step.
* No way to correct a typo, or to withdraw a mistaken application and re-apply.
"""

import io
import os
from datetime import date, datetime, timedelta

import pytest

GENERIC_ERROR = "Something went wrong"
CRYPTIC_UPLOAD_ERROR = "ERR_VAL_47"

NAME_MSG = "Please enter your full name."
DOB_MSG = "Please enter a valid Date of Birth in DD/MM/YYYY format."
AGE_MSG = "Applicant must be at least 60 years of age on the date of application"
CHOICE_MSG = "Please select your gender and marital status."
ADDRESS_MSG = "Please enter your village/town and select your block."
BANK_MSG = "Please enter a valid Bank Account Number and IFSC Code."
MOBILE_MSG = "Please enter a valid 10-digit mobile number."
CAPTCHA_MSG = "Incorrect security check (CAPTCHA) answer. Please try again."
NO_FILE_MSG = "Please select an age proof document (PDF or JPG) to upload."
BAD_TYPE_MSG = "Invalid file format. Please upload a PDF or JPG/JPEG image file."
TOO_BIG_MSG = "File size exceeds the 5 MB limit. Please upload a file smaller than 5 MB."

MB = 1024 * 1024


# --------------------------------------------------------------------------- helpers

def text(resp):
    return resp.get_data(as_text=True)


def start_session(client, mobile, otp_for):
    """/apply -> /verify, leaving the client with a verified session."""
    client.get("/apply")
    with client.session_transaction() as s:
        captcha = str(s.get("captcha_answer", ""))
    client.post("/apply", data={"mobile": mobile, "captcha": captcha})
    client.post("/verify", data={"otp": otp_for(mobile)})


def post_steps(client, form, upto=3):
    """POST /form/1 .. /form/<upto> with the matching fields of ``form``."""
    fields = {
        1: ("applicant_name", "dob", "gender", "marital_status",
            "husband_name", "husband_employer"),
        2: ("village", "block"),
        3: ("bank_account", "ifsc"),
    }
    resp = None
    for step in range(1, upto + 1):
        resp = client.post("/form/%d" % step,
                           data={k: form[k] for k in fields[step]},
                           follow_redirects=True)
    return resp


def upload(client, filename, content, follow=False):
    return client.post("/upload",
                       data={"document": (io.BytesIO(content), filename)},
                       content_type="multipart/form-data", follow_redirects=follow)


def form_values(**overrides):
    from conftest import default_form
    return default_form(**overrides)


def stored(app_module, mobile):
    conn = app_module.get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, applicant_name, dob, gender, marital_status, husband_name, "
                "husband_employer, village, block, bank_account, ifsc, status, doc_path "
                "FROM applications WHERE mobile = %s ORDER BY id", (mobile,))
    rows = cur.fetchall()
    conn.close()
    return rows


def login(client, mobile, password):
    return client.post("/status", data={"mobile": mobile, "password": password})


@pytest.fixture
def applied(app_module, client, submit_application):
    """A citizen who has just submitted an application (PENDING). Returns a dict."""
    def _apply(**form_overrides):
        form = form_values(**form_overrides)
        resp, mobile = submit_application(client, form=form)
        assert resp.status_code == 200, text(resp)[:500]
        row = stored(app_module, mobile)[-1]
        return {"mobile": mobile, "id": row[0], "form": form}
    return _apply


# --------------------------------------------------------------------------- DOB parsing

def test_dob_is_read_as_day_month_year_and_sets_the_login_password(
        app_module, client, submit_application):
    resp, mobile = submit_application(client, form=form_values(dob="05/10/1960"))
    assert resp.status_code == 200
    rows = stored(app_module, mobile)
    assert len(rows) == 1
    assert rows[0][2] == date(1960, 10, 5), "05/10/1960 is 5 October, not 10 May"

    fresh = app_module.app.test_client()
    ok = login(fresh, mobile, "05101960")
    assert ok.status_code == 302 and "/application/" in ok.headers["Location"]
    wrong = login(app_module.app.test_client(), mobile, "10051960")
    assert wrong.status_code == 200
    assert "/application/" not in wrong.headers.get("Location", "")


def test_dob_12_january_1966_is_eligible_and_not_read_as_1_december(
        app_module, client, submit_application):
    resp, mobile = submit_application(client, form=form_values(dob="12/01/1966"))
    assert resp.status_code == 200
    rows = stored(app_module, mobile)
    assert rows, "a citizen born on 12 Jan 1966 is 60 and must be accepted"
    assert rows[0][2] == date(1966, 1, 12)
    assert login(app_module.app.test_client(), mobile, "12011966").status_code == 302


@pytest.mark.parametrize("raw", ["1960-10-05", "05-10-1960", "05.10.1960", "5/10/1960"])
def test_other_common_date_spellings_are_read_as_day_first(
        app_module, client, submit_application, raw):
    resp, mobile = submit_application(client, form=form_values(dob=raw))
    assert resp.status_code == 200
    rows = stored(app_module, mobile)
    assert rows and rows[0][2] == date(1960, 10, 5)


def test_month_first_date_is_still_understood_when_unambiguous(
        app_module, client, submit_application):
    resp, mobile = submit_application(client, form=form_values(dob="10/25/1960"))
    rows = stored(app_module, mobile)
    assert rows and rows[0][2] == date(1960, 10, 25)


# --------------------------------------------------------------------------- widows

@pytest.mark.parametrize("marital", ["Widowed", "Unmarried"])
def test_widow_or_unmarried_needs_no_husband_details(
        app_module, client, submit_application, marital):
    form = form_values(marital_status=marital, gender="Female",
                       husband_name="", husband_employer="")
    resp, mobile = submit_application(client, form=form)
    assert resp.status_code == 200
    assert GENERIC_ERROR not in text(resp)
    rows = stored(app_module, mobile)
    assert len(rows) == 1 and rows[0][4] == marital


# --------------------------------------------------------------------------- uploads

@pytest.fixture
def verified(client, mobile_factory, otp_for):
    mobile = mobile_factory()
    start_session(client, mobile, otp_for)
    post_steps(client, form_values())
    return mobile


@pytest.mark.parametrize("filename,size", [
    ("proof.jpg", 500 * 1024),
    ("proof.JPEG", 200 * 1024),
    ("proof.png", 300 * 1024),
    ("proof.pdf", 500 * 1024),
    ("big.pdf", 5 * MB),
])
def test_valid_uploads_up_to_5mb_are_accepted(client, verified, filename, size):
    resp = upload(client, filename, b"x" * size)
    assert resp.status_code == 302, text(resp)[:400]
    assert resp.headers["Location"].endswith("/declaration")
    assert CRYPTIC_UPLOAD_ERROR not in text(resp)


def test_upload_just_over_5mb_gets_a_clear_size_message(client, verified):
    resp = upload(client, "huge.pdf", b"x" * (5 * MB + 1))
    body = text(resp)
    assert TOO_BIG_MSG in body
    assert CRYPTIC_UPLOAD_ERROR not in body


def test_upload_wrong_type_gets_a_clear_format_message(client, verified):
    resp = upload(client, "virus.exe", b"MZ" + b"\0" * 100)
    body = text(resp)
    assert BAD_TYPE_MSG in body
    assert CRYPTIC_UPLOAD_ERROR not in body


def test_upload_without_a_file_gets_a_clear_message(client, verified):
    resp = client.post("/upload", data={}, content_type="multipart/form-data")
    body = text(resp)
    assert NO_FILE_MSG in body
    assert CRYPTIC_UPLOAD_ERROR not in body


def test_citizen_can_continue_without_reuploading(client, verified):
    assert upload(client, "proof.jpg", b"x" * 1000).status_code == 302
    resp = client.post("/upload", data={}, content_type="multipart/form-data")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/declaration")


def test_uploaded_jpg_keeps_its_extension_inside_upload_dir(
        app_module, client, submit_application):
    resp, mobile = submit_application(
        client, upload=("Photo of Certificate.JPG", b"x" * 2048, "image/jpeg"))
    assert resp.status_code == 200
    doc_path = stored(app_module, mobile)[0][12]
    assert doc_path.lower().endswith(".jpg")
    assert os.path.dirname(os.path.realpath(doc_path)) == os.path.realpath(app_module.UPLOAD_DIR)
    assert os.path.exists(doc_path)


# --------------------------------------------------------------------------- clear errors, step by step

def test_apply_rejects_a_bad_mobile_with_a_clear_message(client):
    client.get("/apply")
    with client.session_transaction() as s:
        captcha = str(s["captcha_answer"])
    body = text(client.post("/apply", data={"mobile": "123", "captcha": captcha}))
    assert MOBILE_MSG in body and GENERIC_ERROR not in body


def test_apply_rejects_a_wrong_captcha(client, mobile_factory, test_mobiles):
    mobile = mobile_factory()
    client.get("/apply")
    with client.session_transaction() as s:
        wrong = str(s["captcha_answer"] + 1)
    resp = client.post("/apply", data={"mobile": mobile, "captcha": wrong})
    assert CAPTCHA_MSG in text(resp)
    with client.session_transaction() as s:
        assert "apply_mobile" not in s, "no OTP may be sent after a failed security check"


def test_apply_without_a_captcha_field_still_works(client, mobile_factory, sms_outbox):
    # a script that never saw the form posts no captcha at all
    resp = client.post("/apply", data={"mobile": mobile_factory()})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/verify")


@pytest.mark.parametrize("overrides,expected", [
    ({"applicant_name": "  "}, NAME_MSG),
    ({"dob": "not-a-date"}, DOB_MSG),
    ({"dob": "31/02/1960"}, DOB_MSG),
    ({"dob": ""}, DOB_MSG),
    ({"dob": "01/01/2000"}, AGE_MSG),
    ({"gender": ""}, CHOICE_MSG),
    ({"marital_status": ""}, CHOICE_MSG),
])
def test_step1_reports_the_exact_problem_and_keeps_the_entries(
        client, mobile_factory, otp_for, overrides, expected):
    start_session(client, mobile_factory(), otp_for)
    form = form_values(**dict({"applicant_name": "Sita Devi"}, **overrides))
    resp = client.post("/form/1", data={k: form[k] for k in (
        "applicant_name", "dob", "gender", "marital_status",
        "husband_name", "husband_employer")}, follow_redirects=True)
    body = text(resp)
    assert expected in body
    assert GENERIC_ERROR not in body
    assert "Personal Details" in body, "the citizen must stay on step 1"
    if "applicant_name" not in overrides:
        assert "Sita Devi" in body, "what the citizen typed must not be lost"


def test_step1_does_not_advance_on_an_error(client, mobile_factory, otp_for):
    start_session(client, mobile_factory(), otp_for)
    resp = client.post("/form/1", data={"applicant_name": "", "dob": "x"})
    assert resp.status_code == 200 or resp.headers["Location"].endswith("/form/1")


@pytest.mark.parametrize("data", [
    {"village": "", "block": "Sonari"},
    {"village": "Testgaon", "block": ""},
])
def test_step2_requires_village_and_block(client, mobile_factory, otp_for, data):
    start_session(client, mobile_factory(), otp_for)
    resp = client.post("/form/2", data=data, follow_redirects=True)
    body = text(resp)
    assert ADDRESS_MSG in body and GENERIC_ERROR not in body
    assert "Address Details" in body


@pytest.mark.parametrize("data", [
    {"bank_account": "", "ifsc": "SBIN0001234"},
    {"bank_account": "12ab", "ifsc": "SBIN0001234"},
    {"bank_account": "123456789012", "ifsc": ""},
    {"bank_account": "123456789012", "ifsc": "BAD"},
])
def test_step3_requires_a_valid_bank_account_and_ifsc(client, mobile_factory, otp_for, data):
    start_session(client, mobile_factory(), otp_for)
    resp = client.post("/form/3", data=data, follow_redirects=True)
    body = text(resp)
    assert BANK_MSG in body and GENERIC_ERROR not in body
    assert "Bank Details" in body


def test_valid_steps_advance_normally(client, mobile_factory, otp_for):
    start_session(client, mobile_factory(), otp_for)
    form = form_values()
    r1 = client.post("/form/1", data={k: form[k] for k in (
        "applicant_name", "dob", "gender", "marital_status")})
    assert r1.status_code == 302 and r1.headers["Location"].endswith("/form/2")
    r2 = client.post("/form/2", data={"village": "Testgaon", "block": "Sonari"})
    assert r2.status_code == 302 and r2.headers["Location"].endswith("/form/3")
    r3 = client.post("/form/3", data={"bank_account": "123456789012", "ifsc": "sbin0001234"})
    assert r3.status_code == 302 and r3.headers["Location"].endswith("/upload")


@pytest.mark.parametrize("data,expected", [
    ({"applicant_name": "", "dob": "20/05/1955", "gender": "Male", "marital_status": "Married",
      "village": "V", "block": "Sonari", "bank_account": "123456789012", "ifsc": "SBIN0001234"},
     NAME_MSG),
    ({"applicant_name": "A", "dob": "20/05/2001", "gender": "Male", "marital_status": "Married",
      "village": "V", "block": "Sonari", "bank_account": "123456789012", "ifsc": "SBIN0001234"},
     AGE_MSG),
    ({"applicant_name": "A", "dob": "garbage", "gender": "Male", "marital_status": "Married",
      "village": "V", "block": "Sonari", "bank_account": "123456789012", "ifsc": "SBIN0001234"},
     DOB_MSG),
    ({"applicant_name": "A", "dob": "20/05/1955", "gender": "Male", "marital_status": "Married",
      "village": "", "block": "Sonari", "bank_account": "123456789012", "ifsc": "SBIN0001234"},
     ADDRESS_MSG),
    ({"applicant_name": "A", "dob": "20/05/1955", "gender": "Male", "marital_status": "Married",
      "village": "V", "block": "Sonari", "bank_account": "", "ifsc": "SBIN0001234"},
     BANK_MSG),
])
def test_declaration_still_names_the_problem_when_posted_directly(
        app_module, client, mobile_factory, data, expected):
    mobile = mobile_factory()
    with client.session_transaction() as s:
        s["verified_mobile"] = mobile
        s["form_data"] = data
    resp = client.post("/declaration", follow_redirects=True)
    body = text(resp)
    assert expected in body
    assert GENERIC_ERROR not in body
    assert not stored(app_module, mobile)


# --------------------------------------------------------------------------- session & deadline

def test_session_lasts_long_enough_to_fill_in_the_form():
    import app as app_module
    assert app_module.app.config["PERMANENT_SESSION_LIFETIME"] >= 3600


def test_deadline_is_midnight_ist(app_module, monkeypatch):
    deadline = app_module.SCHEME_DEADLINE
    assert (deadline.hour, deadline.minute, deadline.second) == (23, 59, 59)
    monkeypatch.setattr(app_module, "now_ist", lambda: deadline - timedelta(seconds=20))
    assert app_module.deadline_remaining() is not None
    monkeypatch.setattr(app_module, "now_ist", lambda: deadline + timedelta(seconds=2))
    assert app_module.deadline_remaining() is None


# --------------------------------------------------------------------------- review before submit

def test_declaration_shows_everything_entered_with_edit_links(
        client, mobile_factory, otp_for):
    start_session(client, mobile_factory(), otp_for)
    post_steps(client, form_values(applicant_name="Kamala Devi", village="Rampur",
                                   bank_account="987654321098"))
    upload(client, "age-proof.jpg", b"x" * 100)
    body = text(client.get("/declaration"))
    for expected in ("Kamala Devi", "20/05/1955", "Male", "Married", "Rampur",
                     "Sonari", "987654321098", "SBIN0001234", "age-proof"):
        assert expected in body, expected
    for link in ('href="/form/1"', 'href="/form/2"', 'href="/form/3"', 'href="/upload"'):
        assert link in body, link


# --------------------------------------------------------------------------- edit a pending application

def test_pending_application_offers_edit_and_withdraw(applied, client, app_module):
    c = applied(applicant_name="Ramesh Kumr")
    assert login(client, c["mobile"], "20051955").status_code == 302
    body = text(client.get("/application/%d" % c["id"]))
    assert "/application/%d/edit" % c["id"] in body
    assert "/application/%d/cancel" % c["id"] in body


def test_citizen_can_fix_a_typo_and_dob_and_login_follows(applied, client, app_module):
    c = applied(applicant_name="Ramesh Kumr", dob="20/05/1955")
    login(client, c["mobile"], "20051955")

    page = client.get("/application/%d/edit" % c["id"])
    assert page.status_code == 200 and "Ramesh Kumr" in text(page)

    resp = client.post("/application/%d/edit" % c["id"], data={
        "applicant_name": "Ramesh Kumar", "dob": "21/05/1955", "gender": "Male",
        "marital_status": "Married", "husband_name": "", "husband_employer": "",
        "village": "Rampur", "block": "Namti", "bank_account": "111122223333",
        "ifsc": "hdfc0001234"})
    assert resp.status_code == 302
    row = stored(app_module, c["mobile"])[0]
    assert row[1] == "Ramesh Kumar" and row[2] == date(1955, 5, 21)
    assert row[7:11] == ("Rampur", "Namti", "111122223333", "HDFC0001234")
    assert row[11] == "PENDING"

    assert "Ramesh Kumar" in text(client.get("/application/%d" % c["id"]))
    # the status password is the DOB, so it must follow the correction
    assert login(app_module.app.test_client(), c["mobile"], "21051955").status_code == 302
    old = login(app_module.app.test_client(), c["mobile"], "20051955")
    assert "/application/" not in old.headers.get("Location", "")


def test_edit_refreshes_the_acknowledgment_pdf(applied, client, app_module):
    from pypdf import PdfReader
    c = applied(applicant_name="Ramesh Kumr")
    login(client, c["mobile"], "20051955")
    assert client.get("/ack/%d.pdf" % c["id"]).status_code == 200   # caches the slip
    client.post("/application/%d/edit" % c["id"], data={
        "applicant_name": "Ramesh Kumar", "dob": "20/05/1955", "gender": "Male",
        "marital_status": "Married", "village": "Testgaon", "block": "Sonari",
        "bank_account": "123456789012", "ifsc": "SBIN0001234"})
    pdf = client.get("/ack/%d.pdf" % c["id"]).data
    content = "".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "Ramesh Kumar" in content and "Kumr" not in content


def test_edit_with_bad_input_explains_and_changes_nothing(applied, client, app_module):
    c = applied(applicant_name="Ramesh Kumar")
    login(client, c["mobile"], "20051955")
    resp = client.post("/application/%d/edit" % c["id"], data={
        "applicant_name": "Ramesh Kumar", "dob": "01/01/2000", "gender": "Male",
        "marital_status": "Married", "village": "Rampur", "block": "Namti",
        "bank_account": "111122223333", "ifsc": "HDFC0001234"}, follow_redirects=True)
    assert AGE_MSG in text(resp)
    row = stored(app_module, c["mobile"])[0]
    assert row[2] == date(1955, 5, 20) and row[7] == "Testgaon"


def test_edit_is_only_for_the_owner_and_only_while_pending(
        applied, client, app_module, make_citizen):
    c = applied()
    other = make_citizen(name="Someone Else", status="PENDING", submitted_days_ago=1)
    login(client, c["mobile"], "20051955")
    payload = {"applicant_name": "Hijacked", "dob": "20/05/1955", "gender": "Male",
               "marital_status": "Married", "village": "X", "block": "Sonari",
               "bank_account": "123456789012", "ifsc": "SBIN0001234"}
    assert client.get("/application/%d/edit" % other["id"]).status_code == 403
    assert client.post("/application/%d/edit" % other["id"], data=payload).status_code == 403
    assert client.post("/application/%d/cancel" % other["id"]).status_code == 403
    assert stored(app_module, other["mobile"])[0][1] == "Someone Else"

    approved = make_citizen(name="Approved Person", status="APPROVED", submitted_days_ago=1)
    c2 = app_module.app.test_client()
    login(c2, approved["mobile"], approved["password"])
    c2.post("/application/%d/edit" % approved["id"], data=payload, follow_redirects=True)
    c2.post("/application/%d/cancel" % approved["id"], follow_redirects=True)
    row = stored(app_module, approved["mobile"])[0]
    assert row[1] == "Approved Person" and row[11] == "APPROVED"


def test_anonymous_visitor_cannot_edit_or_cancel(applied, app_module):
    c = applied()
    anon = app_module.app.test_client()
    assert anon.get("/application/%d/edit" % c["id"]).status_code in (302, 401, 403)
    assert anon.post("/application/%d/cancel" % c["id"]).status_code in (302, 401, 403)
    assert stored(app_module, c["mobile"])[0][11] == "PENDING"


# --------------------------------------------------------------------------- withdraw and re-apply

def test_citizen_can_withdraw_and_then_apply_again(
        applied, client, app_module, submit_application):
    c = applied(applicant_name="Wrong Person", dob="20/05/1955")
    login(client, c["mobile"], "20051955")
    resp = client.post("/application/%d/cancel" % c["id"], follow_redirects=True)
    assert "WITHDRAWN" in text(resp)
    assert stored(app_module, c["mobile"])[0][11] == "WITHDRAWN"

    again = app_module.app.test_client()
    resp, _ = submit_application(
        again, mobile=c["mobile"],
        form=form_values(applicant_name="Right Person", dob="07/03/1958"))
    assert resp.status_code == 200, "a withdrawn application must not block re-applying"
    rows = stored(app_module, c["mobile"])
    assert [r[11] for r in rows] == ["WITHDRAWN", "PENDING"]
    assert rows[1][1] == "Right Person" and rows[1][2] == date(1958, 3, 7)

    # the portal password now follows the new application's DOB
    fresh = app_module.app.test_client()
    ok = login(fresh, c["mobile"], "07031958")
    assert ok.status_code == 302
    assert ok.headers["Location"].endswith("/application/%d" % rows[1][0])


def test_one_active_application_per_person_is_still_enforced(
        applied, app_module, submit_application):
    c = applied()
    again = app_module.app.test_client()
    submit_application(again, mobile=c["mobile"], form=form_values())
    assert "already exists" in text(again.get("/"))
    assert len(stored(app_module, c["mobile"])) == 1


def test_withdrawn_application_cannot_be_edited_or_withdrawn_again(
        applied, client, app_module):
    c = applied()
    login(client, c["mobile"], "20051955")
    client.post("/application/%d/cancel" % c["id"])
    page = text(client.get("/application/%d" % c["id"]))
    assert "/edit" not in page and "/cancel" not in page
    client.post("/application/%d/edit" % c["id"], data={
        "applicant_name": "Changed", "dob": "20/05/1955", "gender": "Male",
        "marital_status": "Married", "village": "X", "block": "Sonari",
        "bank_account": "123456789012", "ifsc": "SBIN0001234"})
    assert stored(app_module, c["mobile"])[0][1] != "Changed"


# --------------------------------------------------------------------------- status login hint

def test_wrong_status_password_explains_what_the_password_is(client, make_citizen):
    c = make_citizen()
    body = text(client.post("/status", data={"mobile": c["mobile"], "password": "nope"}))
    assert GENERIC_ERROR not in body
    assert "DDMMYYYY" in body
