# -*- coding: utf-8 -*-
"""Reproduce the log numbers quoted in FORENSICS.md.

    python3 forensics/analyze_logs.py [LOG_DIR]

LOG_DIR defaults to logs/sewasetu-logs-jan-jun-2026 (extract the archive first).
Standard library only.
"""

import collections
import gzip
import os
import re
import sys
from datetime import datetime

LOG_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs", "sewasetu-logs-jan-jun-2026")

NGINX = re.compile(r'^(\S+) - - \[([^\]]+)\] "(\S+) (\S+) [^"]*" (\d+) (\d+) "[^"]*" "([^"]*)" rt=([\d.]+)')


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def norm(path):
    path = path.split("?")[0]
    path = re.sub(r"^/ack/\d+\.pdf$", "/ack/<id>.pdf", path)
    return re.sub(r"^/application/\d+$", "/application/<id>", path)


def cron():
    text = open(os.path.join(LOG_DIR, "cron.log"), encoding="utf-8").read().splitlines()
    ok = [l for l in text if "deemed approval:" in l]
    print("== cron.log")
    print("successful runs: %d, first %s, last %s" % (len(ok), ok[0][:10], ok[-1][:10]))
    print("auto-approved by cron in log: %d" % sum(int(l.split()[4]) for l in ok))
    print("tracebacks: %d; FileNotFoundError settings.ini: %d" % (
        sum(l.startswith("Traceback") for l in text),
        sum("settings.ini" in l and "FileNotFoundError" in l for l in text)))


def applog():
    print("== sewasetu-app.log")
    lines = open(os.path.join(LOG_DIR, "sewasetu-app.log"), encoding="utf-8").read().splitlines()
    print("levels:", dict(collections.Counter(l.split()[2] for l in lines)))
    fatal = [l for l in lines if "too many clients already" in l]
    print("FATAL too many clients: %d (on day>=28: %d)" % (
        len(fatal), sum(int(l[8:10]) >= 28 for l in fatal)))
    print("by day:", dict(sorted(collections.Counter(l[:10] for l in fatal).items())))
    print("ERROR generating acknowledgment:", sum("ERROR in app: generating" in l for l in lines))
    print("INFO generating acknowledgment:", sum("INFO in app: generating" in l for l in lines))
    print("sms gateway read timeouts:", sum("sms gateway error" in l for l in lines))
    print("'otp expired' / WARNING lines:", sum("otp expired" in l or " WARNING " in l for l in lines))


def nginx():
    print("== nginx-access.log.gz")
    rows = []
    with gzip.open(os.path.join(LOG_DIR, "nginx-access.log.gz"), "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = NGINX.match(line)
            ip, ts, method, path, status, _bytes, ua, rt = m.groups()
            rows.append((ip, datetime.strptime(ts, "%d/%b/%Y:%H:%M:%S %z"), method,
                         norm(path), int(status), ua, float(rt)))
    print("requests: %d; status:" % len(rows), dict(collections.Counter(r[4] for r in rows)))
    print("distinct user agents: %d" % len({r[5] for r in rows}))
    print("per-endpoint volume:")
    for (method, path), n in collections.Counter((r[2], r[3]) for r in rows).most_common():
        print("  %7d %s %s" % (n, method, path))

    ack = [r for r in rows if r[3] == "/ack/<id>.pdf"]
    print("-- /ack/<id>.pdf: %d requests, status %s, distinct IPs %d, distinct UAs %d" % (
        len(ack), dict(collections.Counter(r[4] for r in ack)), len({r[0] for r in ack}),
        len({r[5] for r in ack})))
    decl = [r for r in rows if r[2] == "POST" and r[3] == "/declaration"]
    print("-- POST /declaration: %d, status %s" % (
        len(decl), dict(collections.Counter(r[4] for r in decl))))

    spike = [r for r in rows if r[1].day >= 28]
    normal = [r for r in rows if r[1].day < 28]
    print("-- month-end (day>=28): %d requests over %d days (avg %d/day); "
          "other days: %d requests over %d days (avg %d/day)" % (
              len(spike), len({r[1].date() for r in spike}), len(spike) // len({r[1].date() for r in spike}),
              len(normal), len({r[1].date() for r in normal}), len(normal) // len({r[1].date() for r in normal})))
    for label, sel in (("normal", normal), ("day>=28", spike)):
        print("   rt %-8s p50=%.3f p95=%.3f p99=%.3f" % (
            label, pct([r[6] for r in sel], .5), pct([r[6] for r in sel], .95), pct([r[6] for r in sel], .99)))
    bad = [r for r in rows if r[4] >= 500]
    print("-- 5xx: %d total, on day>=28: %d, endpoints: %s" % (
        len(bad), sum(r[1].day >= 28 for r in bad),
        dict(collections.Counter((r[2], r[3], r[4]) for r in bad))))
    per_day = collections.defaultdict(lambda: [0, 0, 0])
    for r in rows:
        d = per_day[r[1].strftime("%Y-%m-%d")]
        d[0] += 1
        d[1] += r[4] == 500
        d[2] += r[4] == 504
    for day in sorted(per_day):
        if per_day[day][1] or per_day[day][2]:
            print("   %s requests=%6d 500=%4d 504=%4d" % ((day,) + tuple(per_day[day])))
    for key in (("POST", "/declaration"), ("GET", "/application/<id>"), ("GET", "/ack/<id>.pdf")):
        for label, sel in (("normal", normal), ("day>=28", spike)):
            v = [r[6] for r in sel if (r[2], r[3]) == key]
            print("   rt %s %-18s %-8s n=%6d p50=%.3f p95=%.3f" % (
                key[0], key[1], label, len(v), pct(v, .5), pct(v, .95)))
    print("-- 404 paths:", dict(collections.Counter(r[3] for r in rows if r[4] == 404)))
    print("-- /admin requests:", sum(r[3].startswith("/admin") for r in rows))


if __name__ == "__main__":
    cron()
    applog()
    nginx()
