"""Tests for functionality enhancements: queues, macros, bulk jobs,
report subscriptions, flow versioning, TOTP, quote PDF, knowledge suggest."""
import time

import pytest

from forcelet import totp_util
from forcelet.api import create_app
from helpers import login


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    app.config["TESTING"] = True
    return app.test_client()


def test_case_queues(client):
    h = login(client, "admin")
    assert client.get("/api/case-queues", headers=h).get_json() == []
    r = client.post("/api/case-queues", headers=h,
                    json={"name": "Q", "filters": {"==": [{"field": "Priority"}, "High"]}})
    assert r.status_code == 201
    qid = r.get_json()["id"]
    assert client.post("/api/case-queues", headers=h, json={}).status_code == 422
    c1 = client.post("/api/sobjects/Case", headers=h,
                     json={"Subject": "printer jam", "Priority": "High", "Origin": "Web"}).get_json()
    c2 = client.post("/api/sobjects/Case", headers=h,
                     json={"Subject": "meh", "Priority": "Low", "Origin": "Web"}).get_json()
    rows = client.get(f"/api/case-queues/{qid}/cases", headers=h).get_json()
    ids = {r["Id"] for r in rows}
    assert c1["Id"] in ids and c2["Id"] not in ids
    assert client.get("/api/case-queues/nope/cases", headers=h).status_code == 404
    r = client.put(f"/api/case-queues/{qid}", headers=h, json={"name": "Q2"})
    assert r.get_json()["name"] == "Q2"
    assert client.delete(f"/api/case-queues/{qid}", headers=h).status_code == 200


def test_macros(client):
    h = login(client, "admin")
    assert client.post("/api/macros", headers=h, json={"name": "x"}).status_code == 422
    r = client.post("/api/macros", headers=h, json={
        "name": "Close it",
        "actions": [{"set_fields": {"Status": "Closed"}},
                    {"add_comment": "done by macro"},
                    {"reassign": "u_leo0001"}]})
    assert r.status_code == 201
    mid = r.get_json()["id"]
    c = client.post("/api/sobjects/Case", headers=h,
                    json={"Subject": "m", "Status": "New", "Origin": "Web"}).get_json()
    r = client.post(f"/api/sobjects/Case/{c['Id']}/apply-macro",
                    headers=h, json={"macro_id": mid})
    assert r.status_code == 200
    assert r.get_json()["applied"] == ["set_fields", "add_comment", "reassign"]
    rec = client.get(f"/api/sobjects/Case/{c['Id']}", headers=h).get_json()
    assert rec["Status"] == "Closed"
    assert rec["OwnerId"] == "u_leo0001"
    feed = client.get(f"/api/feed?object=Case&record_id={c['Id']}", headers=h).get_json()
    assert any("done by macro" in (p.get("body") or "") for p in feed)
    assert client.post(f"/api/sobjects/Case/{c['Id']}/apply-macro",
                       headers=h, json={"macro_id": "nope"}).status_code == 404
    client.put(f"/api/macros/{mid}", headers=h, json={"active": False})
    assert client.post(f"/api/sobjects/Case/{c['Id']}/apply-macro",
                       headers=h, json={"macro_id": mid}).status_code == 404
    assert client.delete(f"/api/macros/{mid}", headers=h).status_code == 200


def test_bulk_jobs(client):
    h = login(client, "admin")
    assert client.post("/api/bulk-jobs", headers=h, json={}).status_code == 422
    r = client.post("/api/bulk-jobs", headers=h, json={
        "object": "Task", "operation": "insert",
        "rows": [{"Subject": f"b{i}", "Status": "Not Started"} for i in range(4)]})
    assert r.status_code == 201
    jid = r.get_json()["id"]
    for _ in range(40):
        j = client.get(f"/api/bulk-jobs/{jid}", headers=h).get_json()
        if j["status"] == "completed":
            break
        time.sleep(0.25)
    assert j["status"] == "completed", j
    assert j["succeeded"] == 4 and j["failed"] == 0
    rows = client.get("/api/sobjects/Task?search=b", headers=h).get_json()
    assert sum(1 for r in rows if (r.get("Subject") or "").startswith("b")) >= 4
    r = client.post("/api/bulk-jobs", headers=h, json={
        "object": "Task", "operation": "upsert", "external_id_field": "Subject",
        "rows": [{"Subject": "b0", "Status": "Completed"},
                 {"Subject": "brand-new", "Status": "Not Started"}]})
    jid2 = r.get_json()["id"]
    for _ in range(40):
        j = client.get(f"/api/bulk-jobs/{jid2}", headers=h).get_json()
        if j["status"] == "completed":
            break
        time.sleep(0.25)
    assert j["succeeded"] == 2, j
    lst = client.get("/api/bulk-jobs", headers=h).get_json()
    assert any(x["id"] == jid for x in lst)
    assert client.get("/api/bulk-jobs/nope", headers=h).status_code == 404


def test_report_subscriptions(client):
    h = login(client, "admin")
    rep = client.post("/api/admin/reports", headers=h,
                      json={"name": "R", "object": "Case"}).get_json()
    r = client.post("/api/report-subscriptions", headers=h,
                    json={"report_id": rep["id"], "recipients": ["a@x.com"]})
    assert r.status_code == 201
    sid = r.get_json()["id"]
    assert r.get_json()["frequency"] == "daily"
    assert r.get_json()["job_id"]
    assert client.post("/api/report-subscriptions", headers=h,
                       json={"recipients": ["a@x.com"]}).status_code == 422
    assert client.post("/api/report-subscriptions", headers=h,
                       json={"report_id": rep["id"], "recipients": []}).status_code == 422
    from forcelet import automation
    app = client.application
    res = automation.send_report_digest(app.mf_store, app.mf_security, sid)
    assert res["ok"]
    log = client.get("/api/admin/email-log", headers=h).get_json()
    assert any(e.get("recipient") == "a@x.com" for e in log)
    r = client.put(f"/api/report-subscriptions/{sid}", headers=h,
                   json={"frequency": "weekly"})
    assert r.status_code == 200
    job = app.mf_store.config_get("mf_scheduled_jobs", r.get_json()["job_id"])
    assert job["interval_minutes"] == 10080
    assert client.delete(f"/api/report-subscriptions/{sid}", headers=h).status_code == 200
    assert app.mf_store.config_get("mf_scheduled_jobs", job["id"]) is None


def test_flow_versioning(client):
    h = login(client, "admin")
    fid = client.get("/api/admin/flows", headers=h).get_json()[0]["id"]
    assert client.get(f"/api/admin/flows/{fid}/versions", headers=h).get_json() == []
    client.patch(f"/api/admin/flows/{fid}", headers=h, json={"name": "Edited name"})
    vers = client.get(f"/api/admin/flows/{fid}/versions", headers=h).get_json()
    assert len(vers) == 1 and vers[0]["version"] == 1
    vid = vers[0]["id"]
    full = client.get(f"/api/admin/flows/{fid}/versions/{vid}", headers=h).get_json()
    assert "definition" in full and isinstance(full["definition"], dict)
    r = client.post(f"/api/admin/flows/{fid}/rollback", headers=h,
                    json={"version_id": vid})
    assert r.status_code == 200
    assert r.get_json()["name"] != "Edited name"
    vers2 = client.get(f"/api/admin/flows/{fid}/versions", headers=h).get_json()
    assert len(vers2) == 2
    assert client.get("/api/admin/flows/nope/versions", headers=h).status_code == 404
    assert client.post(f"/api/admin/flows/{fid}/rollback", headers=h,
                       json={"version_id": "nope"}).status_code == 404


def test_totp_flow(client):
    h = login(client, "admin")
    assert client.get("/api/me/totp/status", headers=h).get_json() == {"enabled": False}
    sec = client.post("/api/me/totp/setup", headers=h).get_json()
    assert sec["secret"] and sec["otpauth_url"].startswith("otpauth://totp/")
    assert client.post("/api/me/totp/enable", headers=h,
                       json={"code": "000000"}).status_code == 422
    code = totp_util.current_code(sec["secret"])
    assert client.post("/api/me/totp/enable", headers=h,
                       json={"code": code}).get_json() == {"enabled": True}
    assert client.get("/api/me/totp/status", headers=h).get_json() == {"enabled": True}
    # the shared login() helper already rotated the seeded password
    r = client.post("/api/login", json={"username": "admin", "password": "TestPass123!"})
    assert r.get_json()["totp_required"] is True
    chal = r.get_json()["challenge"]
    assert "token" not in r.get_json()
    assert client.post("/api/login/totp",
                       json={"challenge": chal, "code": "000000"}).status_code == 401
    r = client.post("/api/login/totp",
                    json={"challenge": chal,
                          "code": totp_util.current_code(sec["secret"])})
    assert r.status_code == 200 and r.get_json()["token"]
    h2 = {"Authorization": "Bearer " + r.get_json()["token"]}
    assert client.post("/api/login/totp",
                       json={"challenge": chal, "code": code}).status_code == 401
    assert client.post("/api/me/totp/disable", headers=h2,
                       json={"password": "wrong"}).status_code == 403
    assert client.post("/api/me/totp/disable", headers=h2,
                       json={"password": "TestPass123!"}).get_json() == {"enabled": False}
    r = client.post("/api/login", json={"username": "admin", "password": "TestPass123!"})
    assert "token" in r.get_json()


def test_quote_pdf(client):
    h = login(client, "admin")
    q = client.post("/api/sobjects/Quote", headers=h,
                    json={"Name": "Q1", "Status": "Draft"}).get_json()
    client.post("/api/sobjects/QuoteLineItem", headers=h,
                json={"QuoteId": q["Id"], "Quantity": 2, "UnitPrice": 50,
                      "Discount": 10})
    r = client.get(f"/api/quotes/{q['Id']}/pdf", headers=h)
    assert r.status_code == 200
    assert r.content_type == "application/pdf"
    assert r.data.startswith(b"%PDF-1.4")
    assert b"xref" in r.data and r.data.rstrip().endswith(b"%%EOF")
    assert client.get("/api/quotes/nope/pdf", headers=h).status_code == 404


def test_knowledge_suggest_public(client):
    assert client.get("/api/public/knowledge-suggest").get_json() == []
    assert client.get("/api/public/knowledge-suggest?q=ab").get_json() == []
    h = login(client, "admin")
    a = client.post("/api/sobjects/KnowledgeArticle", headers=h, json={
        "Title": "How to install the widget", "Summary": "Install steps",
        "Body": "First unbox the widget, then install it carefully.",
        "Status": "Published"}).get_json()
    hits = client.get("/api/public/knowledge-suggest?q=install").get_json()
    assert any(x["id"] == a["Id"] for x in hits)
    assert all(set(x) == {"id", "title", "summary"} for x in hits)
