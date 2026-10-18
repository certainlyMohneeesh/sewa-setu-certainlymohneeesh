"""IFSC reassurance for the bank-details form.

The bank name comes from a bundled table of the four-letter bank codes, so it works
offline. The branch comes from a public IFSC directory and is best effort: if that
service is slow or unreachable the citizen still sees the bank name, and the form
is never blocked. Only the public IFSC code leaves the server, never any citizen data.
"""
import os
import re
import threading

import requests

IFSC_RE = re.compile(r"[A-Z]{4}0[A-Z0-9]{6}")

BANKS = {
    "SBIN": "State Bank of India", "PUNB": "Punjab National Bank", "BARB": "Bank of Baroda",
    "CNRB": "Canara Bank", "UBIN": "Union Bank of India", "BKID": "Bank of India",
    "IDIB": "Indian Bank", "CBIN": "Central Bank of India", "IOBA": "Indian Overseas Bank",
    "UCBA": "UCO Bank", "MAHB": "Bank of Maharashtra", "PSIB": "Punjab & Sind Bank",
    "HDFC": "HDFC Bank", "ICIC": "ICICI Bank", "UTIB": "Axis Bank",
    "KKBK": "Kotak Mahindra Bank", "YESB": "Yes Bank", "IDFB": "IDFC First Bank",
    "INDB": "IndusInd Bank", "FDRL": "Federal Bank", "KARB": "Karnataka Bank",
    "SIBL": "South Indian Bank", "RATN": "RBL Bank", "BDBL": "Bandhan Bank",
    "AIRP": "Airtel Payments Bank", "IPOS": "India Post Payments Bank",
    "ESFB": "Equitas Small Finance Bank", "AUBL": "AU Small Finance Bank",
    "UJVN": "Ujjivan Small Finance Bank", "JAKA": "Jammu & Kashmir Bank",
    "TMBL": "Tamilnad Mercantile Bank", "CIUB": "City Union Bank", "DLXB": "Dhanlaxmi Bank",
    "KVBL": "Karur Vysya Bank", "CSBK": "CSB Bank", "SRCB": "Saraswat Co-operative Bank",
}

LOOKUP_URL = os.environ.get("IFSC_LOOKUP_URL", "https://ifsc.razorpay.com/{code}")
TIMEOUT = 3
_cache = {}
_lock = threading.Lock()


def _branch_from_directory(code):
    if not LOOKUP_URL:
        return None
    try:
        r = requests.get(LOOKUP_URL.format(code=code), timeout=TIMEOUT)
        if r.status_code != 200:
            return {}
        data = r.json()
    except (requests.RequestException, ValueError):
        return None
    return {"bank": data.get("BANK"), "branch": data.get("BRANCH"),
            "city": data.get("CITY") or data.get("DISTRICT")}


def lookup(code):
    """Return a dict with bank/branch/city, or None when the code has a bad shape."""
    code = (code or "").strip().upper()
    if not IFSC_RE.fullmatch(code):
        return None
    with _lock:
        if code in _cache:
            return _cache[code]
    result = {"ifsc": code, "bank": BANKS.get(code[:4]), "branch": None, "city": None}
    found = _branch_from_directory(code)
    if found:
        result["bank"] = found.get("bank") or result["bank"]
        result["branch"] = found.get("branch")
        result["city"] = found.get("city")
    # Do not cache a failed upstream call, so a later keystroke can retry.
    if found is not None:
        with _lock:
            _cache[code] = result
    return result
