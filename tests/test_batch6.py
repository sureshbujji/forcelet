"""Tests for batch 6: Web-to-Case + Email-to-Case, case SLA milestones and
escalation rules, Forecasting, Screen flows, and HTTP callouts via named
credentials."""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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
def admin(client):
    return login(client, "admin")


@pytest.fixture()
def leo(client):
    return login(client, "leo")


@pytest.fixture()
def maya(client):
    return login(client, "maya")


def _me(client, h):
    return client.get("/api/me", headers=h).get_json()


# ------------------------------------------------------------ web-to-case
def test_web_to_case_form_and_submit(client, admin):
    r = client.get("/api/public/web-to-case")
    assert r.status_code == 200 and b"Open a support case" in r.data

    r = client.post("/api/public/web-to-case", json={
        "LastName": "Webber", "Email": "w@example.com",
        "Subject": "Login broken", "Priority": "High",
        "Description": "Cannot log in since yesterday"})
    assert r.status_code == 201, r.get_json()
    cid = r.get_json()["id"]

    case = client.get(f"/api/sobjects/Case/{cid}", headers=admin).get_json()
    assert case["Origin"] == "Web"
    assert case["Subject"] == "Login broken"

    # SLA milestones were stamped per the seeded High-priority policy
    ms = client.get(f"/api/sobjects/Case/{cid}/milestones",
                    headers=admin).get_json()
    assert {m["name"] for m in ms} == {"First response", "Resolution"}
    assert all(m["due_at"] for m in ms)

    # auto-response email was logged
    log = client.get("/api/admin/email-log", headers=admin).get_json()
    assert any("We received your case" in (e.get("subject") or "")
               for e in log)


def test_email_to_case_webhook(client, admin):
    r = client.post("/api/public/email-to-case", json={
        "from_name": "Erin Example", "from_email": "erin@example.com",
        "subject": "Printer jam", "body": "The office printer is jammed."})
    assert r.status_code == 201, r.get_json()
    cid = r.get_json()["id"]
    case = client.get(f"/api/sobjects/Case/{cid}", headers=admin).get_json()
    assert case["Origin"] == "Email"
    assert "erin@example.com" in (case["Description"] or "")


# ------------------------------------------------------------ SLA + escalation
def test_sla_monitor_no_breach_is_quiet(client, admin, leo, maya):
    cid = client.post("/api/sobjects/Case", headers=leo,
                      json={"Subject": "Outage", "Priority": "High"}
                      ).get_json()["Id"]
    ms = client.get(f"/api/sobjects/Case/{cid}/milestones",
                    headers=admin).get_json()
    assert ms, "seeded SLA policy should stamp milestones"
    items = client.get("/api/admin/scheduled-jobs", headers=admin).get_json()
    job = next(j for j in items if j["name"] == "Case SLA monitor")
    r = client.post(f"/api/admin/scheduled-jobs/{job['id']}/run", headers=admin)
    assert r.get_json()["ok"] is True, r.get_json()
    # nothing overdue yet -> no breach notifications
    notes = client.get("/api/notifications?unread_only=1",
                       headers=leo).get_json()
    assert not any(n["ntype"] == "sla" for n in notes)


def test_sla_breach_engine_marks_and_notifies(client, admin, leo, maya):
    """Drive check_sla_breaches directly with an overdue milestone."""
    from forcelet import automation, store as store_mod, security as sec_mod
    from forcelet.metadata import MetadataRegistry
    db = tempfile.mktemp(suffix=".db")
    from forcelet.bootstrap import bootstrap
    st, reg, sec = bootstrap(db)
    try:
        admin_u = sec.get_user_by_username("admin")
        leo_u = sec.get_user_by_username("leo")
        cid = st.insert("Case", {"Subject": "Breach me", "Priority": "High",
                                 "owner_id": leo_u["id"],
                                 "created_by": admin_u["id"]})
        rec = st.get("Case", cid)
        automation.start_case_milestones(st, "Case", rec)
        # backdate all milestones to the past
        import datetime
        past = (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=3)).isoformat(timespec="seconds")
        for m in automation.case_milestones(st, "Case", cid):
            m["due_at"] = past
            st.config_put("mf_case_milestones", m)
        breached = automation.check_sla_breaches(st, sec)
        assert len(breached) == 2
        assert all(m["breached"] for m in automation.case_milestones(st, "Case", cid))
        # owner (leo) and his manager (maya) were notified
        for u in (leo_u, sec.get_user_by_username("maya")):
            notes = st.notifications_for(u["id"], unread_only=True)
            assert any(n["ntype"] == "sla" for n in notes), u["username"]
        # breach escalation rule fired via the monitor-job code path
        for m in breached:
            rec2 = st.get("Case", cid)
            automation.apply_escalation_rules(st, reg, sec, "Case", rec2, None,
                                             admin_u, trigger_on="sla_breach")
        assert st.get("Case", cid)["Status"] == "Escalated"
    finally:
        os.unlink(db)


def test_escalation_rule_on_save(client, admin, leo):
    r = client.post("/api/sobjects/Case", headers=leo,
                    json={"Subject": "urgent: production down"})
    assert r.status_code == 201, r.get_json()
    cid = r.get_json()["Id"]
    case = client.get(f"/api/sobjects/Case/{cid}", headers=leo).get_json()
    assert case["Priority"] == "Critical"  # seeded VIP escalation rule


def test_milestones_completed_on_close(client, admin, leo):
    cid = client.post("/api/sobjects/Case", headers=leo,
                      json={"Subject": "Close me", "Priority": "Low"}
                      ).get_json()["Id"]
    ms = client.get(f"/api/sobjects/Case/{cid}/milestones",
                    headers=leo).get_json()
    assert all(not m["completed_at"] for m in ms)
    r = client.patch(f"/api/sobjects/Case/{cid}", headers=leo,
                     json={"Status": "Closed"})
    assert r.status_code == 200
    ms = client.get(f"/api/sobjects/Case/{cid}/milestones",
                    headers=leo).get_json()
    assert ms and all(m["completed_at"] for m in ms)


def test_sla_policies_crud(client, admin, leo):
    r = client.post("/api/admin/sla-policies", headers=admin, json={
        "name": "Test SLA", "object": "Case", "active": True,
        "priority": "Critical",
        "milestones": [{"name": "Ack", "target_minutes": 15}]})
    assert r.status_code == 201, r.get_json()
    pid = r.get_json()["id"]
    r = client.get("/api/admin/sla-policies", headers=admin)
    assert any(p["id"] == pid for p in r.get_json())
    # non-admin cannot manage policies
    r = client.post("/api/admin/sla-policies", headers=leo, json={"name": "x"})
    assert r.status_code == 403
    r = client.delete(f"/api/admin/sla-policies/{pid}", headers=admin)
    assert r.get_json()["deleted"] is True


# ------------------------------------------------------------ forecasts
def _opp(client, h, name, stage, amount, prob, close):
    body = {"Name": name, "Stage": stage, "Amount": amount,
            "Probability": prob, "CloseDate": close}
    r = client.post("/api/sobjects/Opportunity", headers=h, json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["Id"]


def test_forecast_math_and_quotas(client, admin, leo):
    period = "2026-10"
    leo_id = _me(client, leo)["id"]
    _opp(client, leo, "Won deal", "Closed Won", 10000, 100, "2026-10-15")
    # stage->probability trigger maps Proposal to 50%
    _opp(client, leo, "Pipe deal", "Proposal", 20000, 50, "2026-10-20")
    _opp(client, leo, "Lost deal", "Closed Lost", 5000, 0, "2026-10-21")
    _opp(client, leo, "Other month", "Proposal", 99999, 80, "2026-11-05")
    r = client.post("/api/admin/forecast-quotas", headers=admin, json={
        "user_id": leo_id, "period": period, "quota": 30000})
    assert r.status_code == 201
    # upsert replaces the quota for the same user+period
    r = client.post("/api/admin/forecast-quotas", headers=admin, json={
        "user_id": leo_id, "period": period, "quota": 40000})
    assert r.status_code == 201
    assert len(client.get("/api/admin/forecast-quotas",
                          headers=admin).get_json()) == 1

    r = client.get(f"/api/forecasts?period={period}", headers=admin)
    assert r.status_code == 200
    row = next(x for x in r.get_json()["rows"] if x["user_id"] == leo_id)
    assert row["closed_amount"] == 10000
    assert row["weighted_pipeline"] == 10000  # 20000 * 50%
    assert row["forecast"] == 20000
    assert row["quota"] == 40000
    assert row["attainment"] == pytest.approx(0.5)

    # leo sees himself (and nobody above him)
    r = client.get(f"/api/forecasts?period={period}", headers=leo)
    ids = [x["user_id"] for x in r.get_json()["rows"]]
    assert leo_id in ids and _me(client, admin)["id"] not in ids

    r = client.get("/api/forecasts?period=nope", headers=admin)
    assert r.status_code == 422


# ------------------------------------------------------------ screen flows
def _flow_id(client, admin, name="Quick contact create"):
    flows = client.get("/api/screen-flows", headers=admin).get_json()
    return next(f["id"] for f in flows if f["name"] == name)


def test_screen_flow_happy_path(client, admin):
    fid = _flow_id(client, admin)
    r = client.post(f"/api/screen-flows/{fid}/start", headers=admin)
    assert r.status_code == 201, r.get_json()
    run_id, screen = r.get_json()["run_id"], r.get_json()["screen"]
    assert screen["title"] == "New contact"
    assert any(f["required"] for f in screen["fields"])

    r = client.post(f"/api/screen-flows/runs/{run_id}/next", headers=admin,
                    json={"values": {"FirstName": "Flow", "LastName": "Tester",
                                     "Email": "flow@test.co"}})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["screen"]["title"] == "Role"

    r = client.post(f"/api/screen-flows/runs/{run_id}/next", headers=admin,
                    json={"values": {"Title": "QA", "Description": "hi"}})
    body = r.get_json()
    assert r.status_code == 200, body
    assert body["status"] == "completed"
    assert body["result"]["created"][0]["object"] == "Contact"
    cid = body["result"]["created"][0]["id"]
    contact = client.get(f"/api/sobjects/Contact/{cid}",
                         headers=admin).get_json()
    assert contact["LastName"] == "Tester" and contact["Email"] == "flow@test.co"

    # completed runs reject further input
    r = client.post(f"/api/screen-flows/runs/{run_id}/next", headers=admin,
                    json={"values": {}})
    assert r.status_code == 422


def test_screen_flow_validation(client, admin):
    fid = _flow_id(client, admin)
    run_id = client.post(f"/api/screen-flows/{fid}/start",
                         headers=admin).get_json()["run_id"]
    # missing required LastName + bad email
    r = client.post(f"/api/screen-flows/runs/{run_id}/next", headers=admin,
                    json={"values": {"Email": "not-an-email"}})
    assert r.status_code == 422
    assert any("required" in e.lower() or "Last name" in e
               for e in r.get_json()["errors"])
    # run is still on screen 1
    r = client.get(f"/api/screen-flows/runs/{run_id}", headers=admin)
    assert r.get_json()["screen"]["title"] == "New contact"


def test_screen_flow_unknown_404(client, admin):
    r = client.post("/api/screen-flows/nope/start", headers=admin)
    assert r.status_code == 404


# ------------------------------------------------------------ callouts
class _Echo(BaseHTTPRequestHandler):
    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.headers.get("Authorization") != "Bearer s3cret":
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(
            {"echo": body.decode(), "path": self.path}).encode())

    do_GET = _handle
    do_POST = _handle

    def log_message(self, *a):
        pass


@pytest.fixture()
def echo_server():
    srv = HTTPServer(("127.0.0.1", 0), _Echo)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_named_credential_crud_and_invoke(client, admin, leo, echo_server):
    r = client.post("/api/admin/named-credentials", headers=admin, json={
        "name": "Echo API", "url": echo_server, "auth_type": "bearer",
        "secret": "s3cret"})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["has_secret"] is True
    assert "secret" not in body and "secret_enc" not in body

    # secret is encrypted at rest
    stored = client.get("/api/admin/named-credentials",
                        headers=admin).get_json()
    assert stored[0]["has_secret"] is True
    assert "secret_enc" not in stored[0]

    # non-admins cannot manage or invoke
    r = client.post("/api/admin/named-credentials", headers=leo,
                    json={"name": "x", "url": "https://x.co"})
    assert r.status_code == 403
    r = client.post("/api/admin/callouts/invoke", headers=leo, json={
        "credential": "Echo API", "path": "/"})
    assert r.status_code == 403

    r = client.post("/api/admin/callouts/invoke", headers=admin, json={
        "credential": "Echo API", "method": "POST", "path": "/hook",
        "body": {"hello": "world"}})
    assert r.status_code == 200, r.get_json()
    res = r.get_json()
    assert res["ok"] is True and res["status"] == 200
    assert json.loads(res["body"])["path"] == "/hook"

    # wrong secret -> 401 surfaces as ok=False
    client.post("/api/admin/named-credentials", headers=admin, json={
        "name": "Echo API", "url": echo_server, "auth_type": "bearer",
        "secret": "wrong"})
    r = client.post("/api/admin/callouts/invoke", headers=admin, json={
        "credential": "Echo API", "path": "/"})
    assert r.get_json()["ok"] is False

    r = client.post("/api/admin/callouts/invoke", headers=admin, json={
        "credential": "Nope", "path": "/"})
    assert "Unknown credential" in r.get_json()["error"]


def test_flow_http_callout_action(client, admin, echo_server):
    client.post("/api/admin/named-credentials", headers=admin, json={
        "name": "Echo API", "url": echo_server, "auth_type": "bearer",
        "secret": "s3cret"})
    r = client.post("/api/admin/flows", headers=admin, json={
        "name": "Ping on high case", "object": "Case",
        "trigger": "on_create", "active": True,
        "condition": {"==": [{"field": "Priority"}, "High"]},
        "actions": [{"type": "http_callout", "credential": "Echo API",
                     "method": "POST", "path": "/case",
                     "body": {"subject": "{{Trigger.Subject}}"}}]})
    assert r.status_code == 201, r.get_json()
    cid = client.post("/api/sobjects/Case", headers=admin,
                      json={"Subject": "Callout case",
                            "Priority": "High"}).get_json()["Id"]
    hist = client.get(f"/api/sobjects/Case/{cid}/history",
                      headers=admin).get_json()
    entries = [h for h in hist
               if h.get("field_name") == "__callout__"]
    assert entries and "status=200" in entries[0]["new_value"]


def test_new_config_in_package_export(client, admin):
    client.post("/api/admin/sla-policies", headers=admin, json={
        "name": "Pkg SLA", "object": "Case", "active": True,
        "priority": "*", "milestones": []})
    r = client.get("/api/admin/packages/export", headers=admin)
    pkg = r.get_json()["config"]
    assert "Pkg SLA" in [p["name"] for p in pkg.get("sla_policies", [])]
    assert any("sla" in p["name"].lower()
               for p in pkg.get("escalation_rules", []))
