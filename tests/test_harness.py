# -*- coding: utf-8 -*-
"""Harness smoke tests: prove the test runner, database and fixtures work.

These assert only behaviour that is already correct today. They deliberately do
NOT cover any known bug; failing-first tests for each fix are added separately.
"""


def test_database_is_the_seeded_dump(db):
    cur = db.cursor()
    cur.execute("SELECT count(*) FROM applications")
    assert cur.fetchone()[0] >= 100000
    cur.execute("SELECT count(*) FROM portal_users")
    assert cur.fetchone()[0] >= 100000


def test_public_pages_are_served(client):
    assert client.get("/").status_code == 200
    assert client.get("/about").status_code == 200
    assert client.get("/apply").status_code == 200


def test_citizen_can_log_in_to_status_portal(client, make_citizen):
    citizen = make_citizen(name="Harness Citizen")
    resp = client.post("/status", data={"mobile": citizen["mobile"],
                                        "password": citizen["password"]})
    assert resp.status_code == 302
    assert "/application/%d" % citizen["id"] in resp.headers["Location"]


def test_wrong_status_password_is_rejected(client, make_citizen):
    citizen = make_citizen()
    resp = client.post("/status", data={"mobile": citizen["mobile"],
                                        "password": "00000000"})
    assert resp.status_code == 200


def test_full_application_journey_helper(client, submit_application, db, sms_outbox):
    resp, mobile = submit_application(client)
    assert resp.status_code == 200
    cur = db.cursor()
    cur.execute("SELECT applicant_name, status FROM applications WHERE mobile = %s",
                (mobile,))
    assert cur.fetchone() == ("Ramesh Kumar", "PENDING")
    assert any("OTP" in m["text"] and m["to"] == mobile for m in sms_outbox)


def test_test_data_is_cleaned_up_between_tests(db):
    cur = db.cursor()
    cur.execute("SELECT count(*) FROM applications WHERE mobile LIKE '5%'")
    assert cur.fetchone()[0] == 0
