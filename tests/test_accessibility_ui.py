# -*- coding: utf-8 -*-
"""Accessibility features for elderly and low-literacy citizens.

Covers the server-rendered parts: read-aloud buttons with translated text, no
placeholder-only fields, persistent hints, the IFSC bank lookup endpoint, and the
senior-friendly CSS (56 px targets, AAA contrast). Client-side formatting lives in
portal.js and is checked by the live-stack run.
"""
import os
import re

import pytest

import ifsc_lookup

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


@pytest.fixture(autouse=True)
def offline_directory(monkeypatch):
    # Keep unit tests off the network; the bundled bank table is what is under test.
    monkeypatch.setattr(ifsc_lookup, "LOOKUP_URL", "")
    ifsc_lookup._cache.clear()


def contrast(fg, bg):
    def lum(h):
        h = h.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        rgb = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
    hi, lo = sorted([lum(fg), lum(bg)], reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_ifsc_endpoint_names_the_bank(client):
    r = client.get("/api/ifsc/sbin0001234")
    assert r.status_code == 200
    assert r.get_json()["bank"] == "State Bank of India"


def test_ifsc_endpoint_rejects_bad_codes(client):
    for code in ("SBIN1001234", "SBIN000123", "../../etc", "SB1N0001234"):
        assert client.get("/api/ifsc/" + code).status_code in (400, 404)


def test_ifsc_unknown_bank_has_no_name(client):
    assert client.get("/api/ifsc/ZZZZ0000001").get_json()["bank"] is None


def test_ifsc_branch_comes_from_directory_and_fails_soft(monkeypatch):
    monkeypatch.setattr(ifsc_lookup, "LOOKUP_URL", "http://directory.invalid/{code}")

    class Resp:
        status_code = 200

        def json(self):
            return {"BANK": "State Bank of India", "BRANCH": "MATUNGA", "CITY": "MUMBAI"}

    monkeypatch.setattr(ifsc_lookup.requests, "get", lambda *a, **k: Resp())
    info = ifsc_lookup.lookup("SBIN0001234")
    assert info["branch"] == "MATUNGA" and info["city"] == "MUMBAI"

    ifsc_lookup._cache.clear()

    def boom(*a, **k):
        raise ifsc_lookup.requests.ConnectionError()

    monkeypatch.setattr(ifsc_lookup.requests, "get", boom)
    info = ifsc_lookup.lookup("SBIN0001234")
    assert info["bank"] == "State Bank of India" and info["branch"] is None
    assert "SBIN0001234" not in ifsc_lookup._cache  # failure is not cached


@pytest.fixture
def verified_client(client, mobile_factory, otp_for):
    mobile = mobile_factory()
    client.get("/apply")
    with client.session_transaction() as s:
        captcha = str(s.get("captcha_answer", ""))
    client.post("/apply", data={"mobile": mobile, "captcha": captcha})
    client.post("/verify", data={"otp": otp_for(mobile)})
    return client


@pytest.mark.parametrize("path", ["/apply", "/status"])
def test_public_pages_have_listen_buttons_in_three_languages(client, path):
    html = client.get(path).get_data(as_text=True)
    assert 'class="listen"' in html
    for lang in ("en", "hi", "as"):
        assert re.search(r'data-say-%s="[^"]+"' % lang, html)


def test_every_form_field_label_has_a_listen_button(verified_client):
    pages = {p: verified_client.get(p).get_data(as_text=True)
             for p in ("/form/1", "/form/2", "/form/3", "/upload")}
    assert pages["/form/1"].count('class="listen"') >= 4
    assert pages["/form/2"].count('class="listen"') == 2
    assert pages["/form/3"].count('class="listen"') == 2
    assert pages["/upload"].count('class="listen"') == 1
    assert 'id="ifsc-bank"' in pages["/form/3"]


def test_no_placeholder_only_fields(client):
    for path in ("/apply", "/status"):
        assert "placeholder=" not in client.get(path).get_data(as_text=True)


def test_dob_field_keeps_a_persistent_hint(verified_client):
    html = verified_client.get("/form/1").get_data(as_text=True)
    assert 'id="dob-hint"' in html and "placeholder=" not in html


def test_voice_controls_exist_in_base_page(client):
    html = client.get("/apply").get_data(as_text=True)
    assert 'id="voice-lang"' in html and 'aria-live="polite"' in html


def test_inputs_and_buttons_are_at_least_56px_tall():
    css = open(os.path.join(STATIC, "portal.css")).read()
    for selector in (r"\.btn\{", r"\.listen\{", r"input\[type=text\][^{]*\{"):
        block = re.search(selector + r"[^}]*\}", css).group(0)
        assert "min-height:56px" in block, selector


def test_text_colours_meet_wcag_aaa():
    css = open(os.path.join(STATIC, "portal.css")).read()
    pairs = [
        ("#111", "#fff"), ("#333", "#fff"), ("#0a4a1a", "#fff"), ("#9b001c", "#fff"),
        ("#7a0016", "#fdecea"), ("#0b3d6e", "#fff"), ("#fff", "#0b3d6e"),
        ("#0a4a1a", "#e4e7ec"), ("#333", "#e4e7ec"), ("#0b3d6e", "#e8f0fb"),
        ("#fff", "#0a4a1a"), ("#111", "#fff4d6"), ("#333", "#f0f2f5"),
    ]
    for fg, bg in pairs:
        assert contrast(fg, bg) >= 7, (fg, bg)
    # The colours the stylesheet actually uses for status text are the ones checked above.
    for colour in ("#0a4a1a", "#9b001c"):
        assert colour in css
    assert "#176b2c" not in css and "#b00020" not in css
