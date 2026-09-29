"""Tests for batch 3: scheduled jobs, assignment rules, global search,
activities, email templates, packaging, audit trail, change data capture,
OAuth2 tokens, API keys, field encryption."""
import os
import sqlite3
import sys
import tempfile
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forcelet import automation
from forcelet.api import create_app
from test_forcelet import login


@pytest.fixture()
def client():
    db = tempfile.mktemp(suffix=".db")
    app = create_app(db)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    os.unlink(db)


@pytest.fixture()
def app_client():
    db = tempfile.mktemp(suffix=".db")
    app = create_app(db)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, app, db
    os.unlink(db)


@pytest.fixture()
def admin(client):
    return login(client, "admin")


@pytest.fixture()
def leo(client):
    return login(client, "leo")


def _admin_headers(app_client):
    c, app, _db = app_client
    return login(c, "admin"), c, app


def _raw_value(db, obj, rid, field):
    con = sqlite3.connect(db)
    try:
        return con.execute(f'SELECT "{field}" FROM sobj_{obj} WHERE id=?',
                           (rid,)).fetchone()[0]
    finally:
        con.close()


# ------------------------------------------------------------ field encryption
def test_encrypted_field_roundtrip(app_client):
    h, c, app = _admin_headers(app_client)
    db = app_client[2]
    r = c.post("/api/admin/objects/Lead/fields", headers=h,
               json={"name": "Secret_Note", "label": "Secret Note",
                     "type": "Text", "encrypted": True})
    assert r.status_code == 201, r.get_json()
    r = c.post("/api/sobjects/Lead", headers=h,
               json={"LastName": "Cipher", "Company": "Acme", "Status": "New",
                     "Secret_Note": "topsecret"})
    assert r.status_code == 201, r.get_json()
    rid = r.get_json()["Id"]
    # API returns plaintext for authorized readers ...
    r = c.get(f"/api/sobjects/Lead/{rid}", headers=h)
    assert r.get_json()["Secret_Note"] == "topsecret"
    # ... but the database holds ciphertext
    raw = _raw_value(db, "Lead", rid, "Secret_Note")
    assert raw.startswith("enc:") and "topsecret" not in raw


def test_encrypted_field_rejects_bad_combos(client, admin):
    r = client.post("/api/admin/objects/Lead/fields", headers=admin,
                    json={"name": "SSN", "label": "SSN", "type": "Text",
                          "encrypted": True, "unique": True})
    assert r.status_code == 422
    r = client.post("/api/admin/objects/Lead/fields", headers=admin,
                    json={"name": "Scr", "label": "Scr", "type": "Number",
                          "encrypted": True})
    assert r.status_code == 422


# ------------------------------------------------------------ assignment rules
def _user_id(client, admin, username):
    r = client.get("/api/admin/users", headers=admin)
    return next(u["id"] for u in r.get_json() if u["username"] == username)


def test_assignment_round_robin(client, admin):
    leo_id = _user_id(client, admin, "leo")
    maya_id = _user_id(client, admin, "maya")

    def mklead(src, n):
        r = client.post("/api/sobjects/Lead", headers=admin,
                        json={"LastName": n, "Company": "Acme", "Status": "New",
                              "LeadSource": src})
        assert r.status_code == 201, r.get_json()
        return r.get_json()

    a = mklead("Web", "WebOne")
    b = mklead("Web", "WebTwo")
    assert {a["OwnerId"], b["OwnerId"]} == {leo_id, maya_id}
    assert a["OwnerId"] != b["OwnerId"]  # round-robin alternates
    other = mklead("Partner", "PartnerOne")
    assert other["OwnerId"] == _user_id(client, admin, "admin")  # no rule matched


def test_assignment_rules_admin_only(client, leo):
    r = client.post("/api/admin/assignment-rules", headers=leo,
                    json={"name": "x", "object": "Lead"})
    assert r.status_code == 403


def test_assignment_rule_validation(client, admin):
    r = client.post("/api/admin/assignment-rules", headers=admin,
                    json={"name": "bad", "object": "Nope",
                          "assignee": {"type": "user", "username": "leo"}})
    assert r.status_code == 422
    r = client.post("/api/admin/assignment-rules", headers=admin,
                    json={"name": "bad2", "object": "Lead",
                          "assignee": {"type": "teleport"}})
    assert r.status_code == 422


# ------------------------------------------------------------ scheduled jobs
def _job_id(client, admin, name):
    r = client.get("/api/admin/scheduled-jobs", headers=admin)
    return next(j["id"] for j in r.get_json() if j["name"] == name)


def test_scheduled_job_manual_run(client, admin):
    soon = (date.today() + timedelta(days=3)).isoformat()
    r = client.post("/api/sobjects/Opportunity", headers=admin,
                    json={"Name": "Closing Soon Opp", "Stage": "Prospecting",
                          "CloseDate": soon})
    assert r.status_code == 201, r.get_json()
    jid = _job_id(client, admin, "Close-date reminders")
    r = client.post(f"/api/admin/scheduled-jobs/{jid}/run", headers=admin)
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    r = client.get("/api/sobjects/Task", headers=admin,
                   query_string={"search": "Closing soon"})
    assert any("Closing Soon Opp" in t["Subject"] for t in r.get_json())
    r = client.get("/api/admin/scheduled-runs", headers=admin,
                   query_string={"job_id": jid})
    assert r.get_json() and r.get_json()[0]["status"] == "ok"


def test_scheduled_job_validation(client, admin):
    r = client.post("/api/admin/scheduled-jobs", headers=admin,
                    json={"name": "bad", "interval_minutes": 60, "code": "def broken(:"})
    assert r.status_code == 422
    r = client.post("/api/admin/scheduled-jobs", headers=admin,
                    json={"name": "bad", "interval_minutes": 0, "code": "pass"})
    assert r.status_code == 422


def test_run_due_jobs_executes_overdue(app_client):
    h, c, app = _admin_headers(app_client)
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security
    r = c.post("/api/admin/scheduled-jobs", headers=h,
               json={"name": "Due job", "interval_minutes": 60, "active": True,
                     "run_as": "admin",
                     "code": "create('Account', {'Name': 'DueJob Corp'})"})
    jid = r.get_json()["id"]
    job = store.config_get("mf_scheduled_jobs", jid)
    job["last_run"] = "2000-01-01T00:00:00"
    store.config_put("mf_scheduled_jobs", job)
    results = automation.run_due_scheduled_jobs(store, registry, security)
    assert any(x["job"] == "Due job" and x["ok"] for x in results)
    r = c.get("/api/search", headers=h, query_string={"q": "duejob"})
    assert any(rec["Name"] == "DueJob Corp"
               for grp in r.get_json() for rec in grp["records"])
    # not due anymore: second pass runs nothing
    assert automation.run_due_scheduled_jobs(store, registry, security) == []


# ------------------------------------------------------------ global search
def test_global_search(client, admin):
    r = client.post("/api/sobjects/Account", headers=admin,
                    json={"Name": "Zebra Stripes Inc"})
    assert r.status_code == 201
    r = client.get("/api/search", headers=admin, query_string={"q": "zebra"})
    groups = {g["object"]: g["records"] for g in r.get_json()}
    assert any(x["Name"] == "Zebra Stripes Inc" for x in groups["Account"])
    r = client.get("/api/search", headers=admin, query_string={"q": ""})
    assert r.get_json() == []


# ------------------------------------------------------------ activities + email
def test_activities_timeline(client, admin):
    r = client.post("/api/sobjects/Account", headers=admin, json={"Name": "Act Corp"})
    rid = r.get_json()["Id"]
    r = client.post(f"/api/sobjects/Account/{rid}/activities", headers=admin,
                    json={"type": "call", "subject": "Discovery call",
                          "body": "Spoke with the buyer."})
    assert r.status_code == 201
    r = client.get(f"/api/sobjects/Account/{rid}/activities", headers=admin)
    items = r.get_json()
    assert items[0]["activity_type"] == "call"
    assert items[0]["subject"] == "Discovery call"
    assert items[0]["created_by_name"]
    r = client.post(f"/api/sobjects/Account/{rid}/activities", headers=admin,
                    json={"type": "smoke"})
    assert r.status_code == 422


def test_send_email_template_merge(client, admin):
    r = client.get("/api/admin/email-templates", headers=admin)
    tpl = next(t for t in r.get_json() if t["name"] == "New lead welcome")
    r = client.post("/api/sobjects/Lead", headers=admin,
                    json={"FirstName": "Ada", "LastName": "L", "Company": "Acme",
                          "Status": "New", "Email": "ada@example.com"})
    rid = r.get_json()["Id"]
    r = client.post(f"/api/sobjects/Lead/{rid}/send-email", headers=admin,
                    json={"template_id": tpl["id"]})
    body = r.get_json()
    assert body["sent"] is True
    assert body["to"] == "ada@example.com"
    assert body["subject"] == "Welcome, Ada!"
    r = client.get("/api/admin/email-log", headers=admin)
    assert any(e["subject"] == "Welcome, Ada!" for e in r.get_json())
    r = client.get(f"/api/sobjects/Lead/{rid}/activities", headers=admin)
    assert any(a["activity_type"] == "email" for a in r.get_json())


# ------------------------------------------------------------ packaging
def test_package_export_import_roundtrip(client, admin):
    r = client.get("/api/admin/packages/export", headers=admin)
    assert r.status_code == 200
    pkg = r.get_json()
    assert pkg["package_version"] == 1
    assert "scheduled_jobs" in pkg["config"]
    assert any(j["name"] == "Close-date reminders"
               for j in pkg["config"]["scheduled_jobs"])
    # delete something, then restore it via import
    rid = next(j["id"] for j in pkg["config"]["assignment_rules"])
    client.delete(f"/api/admin/assignment-rules/{rid}", headers=admin)
    r = client.post("/api/admin/packages/import", headers=admin,
                    json={"package": pkg})
    summary = r.get_json()
    assert summary["config"]["assignment_rules"] == 1
    r = client.get("/api/admin/assignment-rules", headers=admin)
    assert any(x["name"] == "Round-robin web leads" for x in r.get_json())


def test_package_import_rejects_garbage(client, admin):
    r = client.post("/api/admin/packages/import", headers=admin,
                    json={"package": {"nope": True}})
    assert r.status_code == 422


# ------------------------------------------------------------ audit trail
def test_audit_trail_records_admin_actions(client, admin, leo):
    r = client.post("/api/admin/validation-rules", headers=admin,
                    json={"name": "Audit probe", "object": "Account",
                          "condition": {}, "message": "x"})
    assert r.status_code == 201
    r = client.get("/api/admin/audit-trail", headers=admin)
    entries = r.get_json()
    match = [e for e in entries if e["entity_name"] == "Audit probe"]
    assert match and match[0]["username"] == "admin"
    assert match[0]["action"] == "create"
    assert match[0]["entity_type"] == "validation-rules"
    r = client.get("/api/admin/audit-trail", headers=leo)
    assert r.status_code == 403


# ------------------------------------------------------------ change data capture
def test_change_events_cdc_flow(client, admin):
    r = client.post("/api/sobjects/Account", headers=admin,
                    json={"Name": "CDC Corp"})
    rid = r.get_json()["Id"]
    client.patch(f"/api/sobjects/Account/{rid}", headers=admin,
                 json={"Phone": "555-0100"})
    client.delete(f"/api/sobjects/Account/{rid}", headers=admin)
    r = client.get("/api/change-events", headers=admin,
                   query_string={"since": 0, "object": "Account"})
    mine = [e for e in r.get_json() if e["record_id"] == rid]
    kinds = [e["event"] for e in mine]
    assert kinds == ["create", "update", "delete"]
    assert "Phone" in mine[1]["changed_fields"]
    # since= filters replay correctly
    seq = mine[0]["seq"]
    r = client.get("/api/change-events", headers=admin,
                   query_string={"since": seq, "object": "Account"})
    assert [e["record_id"] for e in r.get_json()].count(rid) == 2


# ------------------------------------------------------------ oauth2 + api keys
def test_oauth_password_and_refresh_rotation(client):
    r = client.post("/api/oauth/token",
                    json={"grant_type": "password", "username": "leo",
                          "password": "forcelet"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["token_type"] == "Bearer" and body["refresh_token"]
    hdr = {"Authorization": f"Bearer {body['access_token']}"}
    assert client.get("/api/sobjects/Account", headers=hdr).status_code == 200
    r = client.post("/api/oauth/token",
                    json={"grant_type": "refresh_token",
                          "refresh_token": body["refresh_token"]})
    assert r.status_code == 200
    body2 = r.get_json()
    assert body2["refresh_token"] != body["refresh_token"]
    # single-use rotation: the old refresh token is dead
    r = client.post("/api/oauth/token",
                    json={"grant_type": "refresh_token",
                          "refresh_token": body["refresh_token"]})
    assert r.status_code == 401
    r = client.post("/api/oauth/token",
                    json={"grant_type": "password", "username": "leo",
                          "password": "wrong"})
    assert r.status_code == 401


def test_api_key_lifecycle(client, admin):
    r = client.post("/api/api-keys", headers=admin, json={"name": "ci key"})
    assert r.status_code == 201
    body = r.get_json()
    assert body["key"].startswith("mf_live_")
    assert "Copy this key now" in body["warning"]
    hdr = {"Authorization": f"Bearer {body['key']}"}
    assert client.get("/api/sobjects/Account", headers=hdr).status_code == 200
    r = client.get("/api/api-keys", headers=admin)
    assert any(k["id"] == body["id"] and k["name"] == "ci key" for k in r.get_json())
    # the raw key is never shown again
    assert all("key" not in k for k in r.get_json())
    r = client.delete(f"/api/api-keys/{body['id']}", headers=admin)
    assert r.get_json()["deleted"] is True
    assert client.get("/api/sobjects/Account", headers=hdr).status_code == 401
