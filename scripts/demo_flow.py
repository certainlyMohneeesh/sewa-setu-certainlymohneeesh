#!/usr/bin/env python3
"""Walk one new citizen through the whole Sewa Setu journey on the running stack.

    docker compose up -d                     # start the stack first
    python3 scripts/demo_flow.py             # citizen journey, printing every step and the OTP
    python3 scripts/demo_flow.py --approve   # then log in as staff and approve the application

Standard library only, so it runs on the host without installing anything.
Every run creates real application records in the database; nothing is deleted.
Demo mobiles start with 3: the seed data uses 6-9 and the probe suite cleans up 5.
"""

import argparse
import html as htmllib
import http.cookiejar
import io
import json
import mimetypes
import os
import random
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PDF = b"%PDF-1.4\n% Sewa Setu demo age proof\n%%EOF\n"


def say(text=""):
    print(text, flush=True)


def heading(number, title):
    say("")
    say("== Step %d: %s" % (number, title))


def stop(message):
    say("")
    say("STOPPED: " + message)
    sys.exit(1)


def read_dotenv():
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


def strip_tags(fragment):
    return htmllib.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


class Page(object):
    def __init__(self, status, url, body):
        self.status = status
        self.url = url
        self.body = body

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")

    def fail(self, what):
        """Stop with the portal's own flash message, which is what a citizen would read."""
        messages = [strip_tags(m) for m in
                    re.findall(r'<div class="flash"[^>]*>(.*?)</div>', self.text, re.S)]
        detail = "; ".join(messages) if messages else "HTTP %d at %s" % (self.status, self.url)
        stop("%s (%s)" % (what, detail))


class Browser(object):
    """One person's browser: keeps the session cookie and follows redirects."""

    def __init__(self, base):
        self.base = base
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def get(self, path):
        return self._open(urllib.request.Request(self.base + path, method="GET"))

    def post(self, path, form=None, files=None):
        headers = {}
        body = None
        if files:
            body, headers["Content-Type"] = multipart(form or {}, files)
        elif form is not None:
            body = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        return self._open(urllib.request.Request(self.base + path, data=body,
                                                 headers=headers, method="POST"))

    def _open(self, request):
        try:
            with self.opener.open(request, timeout=30) as resp:
                return Page(resp.status, resp.geturl(), resp.read())
        except urllib.error.HTTPError as err:
            return Page(err.code, err.geturl(), err.read())


def multipart(fields, files):
    boundary = uuid.uuid4().hex
    out = io.BytesIO()
    for name, value in fields.items():
        out.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                   % (boundary, name, value)).encode("utf-8"))
    for name, (filename, content) in files.items():
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        out.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                   "Content-Type: %s\r\n\r\n" % (boundary, name, filename, mime)).encode("utf-8"))
        out.write(content)
        out.write(b"\r\n")
    out.write(("--%s--\r\n" % boundary).encode("ascii"))
    return out.getvalue(), "multipart/form-data; boundary=" + boundary


def gateway_messages(base, mobile):
    query = urllib.parse.urlencode({"to": mobile})
    with urllib.request.urlopen(base + "/__gateway/api/messages?" + query, timeout=10) as resp:
        return json.load(resp)["messages"]


def wait_for_otp(base, mobile, already_seen, timeout=30):
    """The citizen's phone: watch the inbox until the new OTP has been delivered."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for message in reversed(gateway_messages(base, mobile)):
            if message["id"] in already_seen or not (message["delivered"] and message["text"]):
                continue
            found = re.search(r"OTP is (\d{6})", message["text"])
            if found:
                return found.group(1), message["text"]
        time.sleep(0.5)
    stop("no OTP reached %s within %d seconds; check the SMS gateway console" % (mobile, timeout))


def application_fields(page):
    rows = re.findall(r"<tr><th>([^<]+)</th><td>(.*?)</td></tr>", page.text, re.S)
    return {label.strip(): strip_tags(value) for label, value in rows}


def main():
    parser = argparse.ArgumentParser(description="Walk one citizen through the Sewa Setu journey.")
    parser.add_argument("--base", default=os.environ.get("PORTAL_URL", "http://localhost:8000"),
                        help="portal address (default: %(default)s)")
    parser.add_argument("--mobile", help="10-digit mobile number "
                                         "(default: a new random number starting with 3)")
    parser.add_argument("--name", default="Ramesh Kumar", help="applicant's full name")
    parser.add_argument("--dob", default="05/10/1960",
                        help="date of birth as DD/MM/YYYY; the status password is DDMMYYYY")
    parser.add_argument("--document", help="age proof: PDF, JPG or PNG up to 5 MB "
                                           "(default: a tiny demo PDF)")
    parser.add_argument("--approve", action="store_true",
                        help="then log in as staff and approve the application")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    mobile = args.mobile or "3" + "".join(random.choice("0123456789") for _ in range(9))
    if not re.fullmatch(r"\d{10}", mobile):
        stop("--mobile must be exactly 10 digits")
    dob = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", args.dob)
    if not dob:
        stop("--dob must be in DD/MM/YYYY format")
    status_password = "".join(dob.groups())
    if args.document:
        doc_name = os.path.basename(args.document)
        try:
            with open(args.document, "rb") as fh:
                doc_bytes = fh.read()
        except OSError as exc:
            stop("cannot read --document: %s" % exc)
    else:
        doc_name, doc_bytes = "age-proof.pdf", DEFAULT_PDF

    citizen = Browser(base)

    heading(1, "Checking the stack is running")
    if citizen.get("/").status != 200 or citizen.get("/__gateway/").status != 200:
        stop("the portal is not answering at %s. Start it with: docker compose up -d" % base)
    say("Portal:        %s/" % base)
    say("SMS gateway:   %s/__gateway/   (the citizen's phone inbox)" % base)
    say("Demo mobile:   %s" % mobile)

    heading(2, "Applying: the citizen enters a mobile number and the CAPTCHA sum")
    page = citizen.get("/apply")
    sum_match = re.search(r"What is (\d+) \+ (\d+)\?", page.text)
    if not sum_match:
        stop("CAPTCHA question not found on /apply")
    captcha = int(sum_match.group(1)) + int(sum_match.group(2))
    seen = {m["id"] for m in gateway_messages(base, mobile)}
    page = citizen.post("/apply", form={"mobile": mobile, "captcha": str(captcha)})
    if not page.url.endswith("/verify"):
        page.fail("the application could not start")
    say("CAPTCHA answered. The portal has sent an OTP to %s through the SMS gateway." % mobile)

    heading(3, "Reading the OTP from the SMS gateway (the citizen's phone)")
    otp, text = wait_for_otp(base, mobile, seen)
    say("SMS received:  %s" % text)
    say("OTP:           %s" % otp)

    heading(4, "Entering the OTP")
    page = citizen.post("/verify", form={"otp": otp})
    if not page.url.endswith("/form/1"):
        page.fail("the OTP was not accepted")
    say("Mobile verified. Moving on to the form.")

    heading(5, "Filling in the three form steps and uploading the age proof")
    steps = [
        ("personal details", 1, "/form/2", {
            "applicant_name": args.name, "dob": args.dob, "gender": "Male",
            "marital_status": "Married", "husband_name": "", "husband_employer": ""}),
        ("village and block", 2, "/form/3", {"village": "Rampur", "block": "Sonari"}),
        ("bank details", 3, "/upload", {"bank_account": "123456789012", "ifsc": "SBIN0001234"}),
    ]
    for label, number, next_path, values in steps:
        page = citizen.post("/form/%d" % number, form=values)
        if not page.url.endswith(next_path):
            page.fail("step %d (%s) was not accepted" % (number, label))
        say("Step %d (%s) saved." % (number, label))
    page = citizen.post("/upload", files={"document": (doc_name, doc_bytes)})
    if not page.url.endswith("/declaration"):
        page.fail("the age proof was not accepted")
    say("Age proof uploaded: %s" % doc_name)

    heading(6, "Reviewing the declaration and submitting")
    page = citizen.post("/declaration")
    ack = re.search(r"/ack/(\d+)\.pdf", page.text)
    if not ack:
        page.fail("the application was not submitted")
    app_id = int(ack.group(1))
    app_no = application_fields(page).get("Application Number", "?")
    say("Submitted. Application number: %s (record id %d)" % (app_no, app_id))

    heading(7, "Downloading the acknowledgment slip")
    slip = citizen.get("/ack/%d.pdf" % app_id)
    if slip.status != 200 or not slip.body.startswith(b"%PDF"):
        stop("the acknowledgment slip could not be downloaded (HTTP %d)" % slip.status)
    slip_path = os.path.join(tempfile.gettempdir(), "sewasetu-demo-%d.pdf" % app_id)
    with open(slip_path, "wb") as fh:
        fh.write(slip.body)
    say("Saved %d bytes to %s" % (len(slip.body), slip_path))

    heading(8, "Coming back later: logging into the status portal")
    portal = Browser(base)
    page = portal.post("/status", form={"mobile": mobile, "password": status_password})
    if not page.url.endswith("/application/%d" % app_id):
        page.fail("status login failed")
    shown = application_fields(page)
    say("Logged in with password %s (date of birth as DDMMYYYY)." % status_password)
    say("Application no:  %s" % shown.get("Application No"))
    say("Applicant name:  %s" % shown.get("Applicant Name"))
    say("Name matches the typed name: %s" % ("yes" if shown.get("Applicant Name") == args.name
                                             else "NO"))
    say("Date of birth:   %s  (stored from DD/MM/YYYY)" % shown.get("Date of Birth"))
    say("Status:          %s" % shown.get("Status"))

    if args.approve:
        heading(9, "Staff approval in the admin panel")
        env = read_dotenv()
        admin_user, admin_secret = env.get("ADMIN_USERNAME"), env.get("ADMIN_PASSWORD")
        if not admin_user or not admin_secret:
            stop("no admin login found in .env; create it with: sh scripts/gen-env.sh")
        staff = Browser(base)
        page = staff.post("/admin", form={"username": admin_user, "password": admin_secret})
        if not page.url.endswith("/admin/dashboard"):
            page.fail("admin login failed")
        say("Logged in to /admin as %s." % admin_user)
        page = staff.post("/admin/approve/%d" % app_id)
        if not page.url.endswith("/admin/applications"):
            page.fail("the approval did not go through")
        say("Approved application %s." % app_no)
        shown = application_fields(portal.get("/application/%d" % app_id))
        say("The citizen now sees status: %s" % shown.get("Status"))

    heading(10 if args.approve else 9, "Demo complete. Where to look")
    say("Citizen status:  %s/status   mobile %s, password %s" % (base, mobile, status_password))
    say("SMS inbox:       %s/__gateway/" % base)
    say("Admin panel:     %s/admin   (login from .env: ADMIN_USERNAME / ADMIN_PASSWORD)" % base)
    say("Acknowledgment:  %s" % slip_path)
    say("Container logs:  docker compose logs -f app")


if __name__ == "__main__":
    main()
