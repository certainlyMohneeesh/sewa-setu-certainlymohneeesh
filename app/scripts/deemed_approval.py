# -*- coding: utf-8 -*-
"""
Nightly deemed-approval job.

Under the Purvanchal Right to Public Services Act, an application not processed
within the statutory SLA stands approved. This job marks such applications
DEEMED_APPROVED and notifies the applicant by SMS.

Scheduled via cron, 02:00 daily. See deploy/crontab.
"""

import os
import sys
import configparser
from datetime import datetime, timedelta, timezone

import psycopg2
import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Statutory time is India Standard Time (UTC+05:30, no DST); stored timestamps
# are naive IST.
IST = timezone(timedelta(hours=5, minutes=30))

# Settings were merged from settings.ini into app.ini (CR-2026-031); keep
# settings.ini as a fallback for older deployments.
config = configparser.ConfigParser()
CONFIG_PATHS = [os.path.join(BASE_DIR, "config", "app.ini"),
                os.path.join(BASE_DIR, "config", "settings.ini")]
if not config.read(CONFIG_PATHS):
    sys.exit("deemed_approval: no configuration found in %s" % ", ".join(CONFIG_PATHS))

SLA_DAYS = config.getint("pension", "sla_days")
SMS_GATEWAY_URL = os.environ.get("SEWASETU_SMS_GATEWAY_URL",
                                 config.get("app", "sms_gateway_url"))

# Applications that crossed the SLA line within this many days of the cutoff
# get an SMS. Anything older is a backlog (e.g. the March-June 2026 outage):
# it is approved in the database but not texted, so a recovery run cannot flood
# the gateway with thousands of synchronous calls.
SMS_WINDOW_DAYS = 3


def now_ist():
    return datetime.now(IST).replace(tzinfo=None)


def db_setting(env_name, option):
    return os.environ.get(env_name) or config.get("database", option)


def main():
    conn = psycopg2.connect(
        host=db_setting("SEWASETU_DB_HOST", "host"),
        port=db_setting("SEWASETU_DB_PORT", "port"),
        dbname=db_setting("SEWASETU_DB_NAME", "name"),
        user=db_setting("SEWASETU_DB_USER", "user"),
        password=db_setting("SEWASETU_DB_PASSWORD", "password"),
    )
    cur = conn.cursor()
    now = now_ist()
    cutoff = now - timedelta(days=SLA_DAYS)
    cur.execute(
        """UPDATE applications
           SET status = 'DEEMED_APPROVED', decided_at = %s, decided_by = 'RTPS-AUTO'
           WHERE status = 'PENDING' AND submitted_at < %s
           RETURNING application_no, mobile, submitted_at""",
        (now, cutoff))
    rows = cur.fetchall()
    conn.commit()
    notify_after = cutoff - timedelta(days=SMS_WINDOW_DAYS)
    notified = 0
    for app_no, mobile, submitted_at in rows:
        if submitted_at < notify_after:
            continue
        try:
            requests.post(SMS_GATEWAY_URL + "/api/send", json={
                "to": mobile,
                "text": "Sewa Setu: your pension application %s stands approved "
                        "under the RTPS Act." % app_no}, timeout=5)
            notified += 1
        except Exception:
            pass
    print("%s deemed approval: %d applications approved (%d notified by SMS)"
          % (now.strftime("%Y-%m-%d %H:%M:%S"), len(rows), notified))
    cur.close()
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
