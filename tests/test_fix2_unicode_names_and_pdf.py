# -*- coding: utf-8 -*-
"""Fix #2 - Assamese / Indian-script names.

DC briefing: an old man's Assamese name printed as four question marks on his
acknowledgment slip, and other names "come out cut in half".

Causes: sanitize() sliced 20 *bytes* (a Bengali-script letter is 3 bytes, so 6
characters survived) although the column is VARCHAR(100); and the slip used the
Latin-1 core font Helvetica, so any other script hit the bare ``except`` that
printed "?".
"""

import io
import os
import re
import unicodedata

import pytest
from conftest import default_form

ASSAMESE = "কমলাকান্ত ভট্টাচাৰ্য"          # 20 characters, 58 UTF-8 bytes
PROBE_NAME = ASSAMESE + " \u2014 PROBE_TOKEN_123"
BENGALI = "রবীন্দ্রনাথ ঠাকুর"
HINDI = "रामेश्वर प्रसाद शर्मा"
LONG_LATIN = "Padmeswari Choudhury Senapati"  # 29 chars; old limit was 20


def pdf_text(data):
    """Text of a PDF as a text extractor (what a probe or a reader) would see it."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        pytest.fail("pypdf is required to read PDF text: pip install -r requirements.txt")
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() for page in reader.pages)


def squash(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", text))


def login(client, citizen_mobile, password):
    resp = client.post("/status", data={"mobile": citizen_mobile, "password": password})
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# sanitize()
# ---------------------------------------------------------------------------

def test_sanitize_keeps_a_full_assamese_name(app_module):
    assert len(ASSAMESE.encode("utf-8")) > 20 and len(ASSAMESE) == 20
    assert app_module.sanitize(ASSAMESE) == ASSAMESE


@pytest.mark.parametrize("name", [ASSAMESE, BENGALI, HINDI, LONG_LATIN, PROBE_NAME])
def test_sanitize_does_not_truncate_names_under_the_column_limit(app_module, name):
    assert app_module.sanitize(name) == name


def test_sanitize_limits_by_characters_to_the_column_width(app_module):
    assert app_module.sanitize("ক" * 150) == "ক" * 100
    assert app_module.sanitize("a" * 150) == "a" * 100
    assert app_module.sanitize("ক" * 150, maxlen=30) == "ক" * 30


def test_sanitize_never_splits_a_character(app_module):
    out = app_module.sanitize("ক" * 150, maxlen=7)
    assert out == "ক" * 7 and "\ufffd" not in out


def test_sanitize_normalises_to_nfc_and_strips(app_module):
    decomposed = "\u09c7\u09be"       # BENGALI VOWEL SIGN E + AA
    assert app_module.sanitize("  %s  " % decomposed) == "\u09cb"
    assert app_module.sanitize("Jose\u0301") == "Jos\u00e9"
    assert app_module.sanitize(None) == ""


# ---------------------------------------------------------------------------
# acknowledgment PDF
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [ASSAMESE, BENGALI, HINDI, LONG_LATIN, PROBE_NAME])
def test_acknowledgment_pdf_contains_the_name_not_question_marks(app_module, make_citizen, name):
    citizen = make_citizen(name=name)
    conn = app_module.get_db()
    data = app_module.generate_acknowledgment(conn.cursor(), citizen["id"])
    conn.close()
    assert data.startswith(b"%PDF")
    text = pdf_text(data)
    assert squash(name) in squash(text), text
    row = re.search(r"Applicant Name\s*(.*)", text).group(1)
    assert row.strip() not in ("?", "????")


def test_acknowledgment_pdf_embeds_a_unicode_font(app_module, make_citizen):
    citizen = make_citizen(name=ASSAMESE)
    conn = app_module.get_db()
    data = app_module.generate_acknowledgment(conn.cursor(), citizen["id"])
    conn.close()
    assert b"/ToUnicode" in data and b"Noto" in data


# ---------------------------------------------------------------------------
# end to end: what the citizen and the probe do
# ---------------------------------------------------------------------------

def test_assamese_application_round_trips_through_db_page_and_pdf(
        client, app_module, submit_application, db):
    form = default_form(applicant_name=PROBE_NAME,
                                village="কামৰূপ মহানগৰ জিলা",
                                husband_name="", husband_employer="")
    resp, mobile = submit_application(client, form=form)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]

    cur = db.cursor()
    cur.execute("SELECT id, applicant_name, village FROM applications WHERE mobile = %s",
                (mobile,))
    app_id, stored_name, stored_village = cur.fetchone()
    assert stored_name == PROBE_NAME
    assert stored_village == "কামৰূপ মহানগৰ জিলা"

    # the slip written at submission time
    cached = os.path.join(app_module.UPLOAD_DIR, "ack", "%d.pdf" % app_id)
    with open(cached, "rb") as fh:
        assert squash(PROBE_NAME) in squash(pdf_text(fh.read()))

    login(client, mobile, "20051955")
    page = client.get("/application/%d" % app_id).get_data(as_text=True)
    assert PROBE_NAME in page

    served = client.get("/ack/%d.pdf" % app_id)
    assert served.status_code == 200 and served.data.startswith(b"%PDF")
    assert squash(PROBE_NAME) in squash(pdf_text(served.data))


def test_other_text_fields_are_limited_by_characters_not_bytes(
        client, submit_application, db):
    long_village = "গ্রাম" * 30                      # 150 characters
    form = default_form(village=long_village,
                                husband_name="স্বামীৰ নাম " * 3)
    resp, mobile = submit_application(client, form=form)
    assert resp.status_code == 200
    cur = db.cursor()
    cur.execute("SELECT village, husband_name FROM applications WHERE mobile = %s", (mobile,))
    village, husband = cur.fetchone()
    assert village == long_village[:100]
    assert husband == ("স্বামীৰ নাম " * 3).strip()[:100]
