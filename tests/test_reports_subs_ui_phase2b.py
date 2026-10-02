"""Tests for Phase 2b: per-recipient digests, dashboard refresh, refresh-sync.

- send_report_digest runs the report (or each dashboard widget's report)
  AS each recipient, so every recipient gets their own sharing-scoped
  snapshot. Raw-email recipients fall back to the default run user and
  their copy is marked as a shared view.
- condition.min_rows is evaluated per recipient.
- POST /api/dashboards/<did>/refresh-sync manages the scheduled refresh
  job; automation.refresh_dashboard runs it and persists last_run_at.
"""
import pytest

from forcelet import automation
from forcelet.api import create_app
from helpers import login


@pytest.fixture()
def app(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def _mk_profile(client, h, name, obj_perms):
    r = client.post("/api/admin/profiles", headers=h, json={
        "name": name, "object_permissions": obj_perms, "field_permissions": {}})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def _mk_user(client, h, username, profile, email):
    r = client.post("/api/admin/users", headers=h, json={
        "username": username, "name": username.title(), "profile": profile,
        "password": "UserPass1!", "email": email})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def _mk_report(client, h, name, obj, columns):
    r = client.post("/api/admin/reports", headers=h, json={
        "name": name, "object": obj, "columns": columns})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def _mk_dashboard(client, h, name, widgets):
    r = client.post("/api/dashboards", headers=h, json={
        "name": name, "widgets": widgets})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def _mk_sub(client, h, recipients, report_id=None, dashboard_id=None,
            condition=None, attachment="none"):
    body = {"frequency": "daily", "attachment": attachment,
            "recipients": recipients, "condition": condition or {}}
    if report_id:
        body["report_id"] = report_id
    if dashboard_id:
        body["dashboard_id"] = dashboard_id
    r = client.post("/api/report-subscriptions", headers=h, json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def _digest_log(app):
    return {e["recipient"]: e
            for e in app.mf_store.email_log(limit=100)
            if e.get("template") == "report-digest"}


@pytest.fixture()
def env(client):
    """Admin + alice (owns an Account) + bob (owns nothing)."""
    h_admin = login(client)
    prof = _mk_profile(client, h_admin, "sub2b", {
        "Account": {"read": True, "create": True, "edit": True}})
    alice = _mk_user(client, h_admin, "sub2balice", prof["name"], "alice@x.test")
    bob = _mk_user(client, h_admin, "sub2bbob", prof["name"], "bob@x.test")
    h_alice = login(client, "sub2balice", password="UserPass1!")
    h_bob = login(client, "sub2bbob", password="UserPass1!")
    client.post("/api/sobjects/Account", headers=h_admin, json={"Name": "AdminCo"})
    client.post("/api/sobjects/Account", headers=h_alice, json={"Name": "AliceCo"})
    return {"admin": h_admin, "alice": h_alice, "bob": h_bob,
            "alice_id": alice["id"], "bob_id": bob["id"]}


def test_per_recipient_digest_scopes(app, client, env):
    rep_id = _mk_report(client, env["admin"], "All Accounts", "Account", ["Name"])
    sid = _mk_sub(client, env["admin"],
                  [{"type": "user", "value": env["alice_id"]},
                   {"type": "user", "value": env["bob_id"]}],
                  report_id=rep_id)
    res = automation.send_report_digest(app.mf_store, app.mf_security, sid)
    assert res["ok"], res
    log = _digest_log(app)
    assert "alice@x.test" in log and "bob@x.test" in log
    assert "1 record(s)" in log["alice@x.test"]["body"]
    assert "0 record(s)" in log["bob@x.test"]["body"]


def test_min_rows_evaluated_per_recipient(app, client, env):
    rep_id = _mk_report(client, env["admin"], "All Accounts", "Account", ["Name"])
    sid = _mk_sub(client, env["admin"],
                  [{"type": "user", "value": env["alice_id"]},
                   {"type": "user", "value": env["bob_id"]}],
                  report_id=rep_id, condition={"min_rows": 1})
    res = automation.send_report_digest(app.mf_store, app.mf_security, sid)
    assert res["ok"], res
    assert "skipped by condition" in res["detail"]
    log = _digest_log(app)
    assert "alice@x.test" in log
    assert "bob@x.test" not in log


def test_raw_email_fallback_is_marked(app, client, env):
    rep_id = _mk_report(client, env["admin"], "All Accounts", "Account", ["Name"])
    sid = _mk_sub(client, env["admin"],
                  [{"type": "email", "value": "outsider@x.test"}],
                  report_id=rep_id)
    res = automation.send_report_digest(app.mf_store, app.mf_security, sid)
    assert res["ok"], res
    log = _digest_log(app)
    assert "outsider@x.test" in log
    assert "Shared-view copy" in log["outsider@x.test"]["body"]


def test_unresolvable_user_recipient_skipped(app, client, env):
    rep_id = _mk_report(client, env["admin"], "All Accounts", "Account", ["Name"])
    sid = _mk_sub(client, env["admin"],
                  [{"type": "user", "value": "no-such-user"},
                   {"type": "email", "value": "x@y.test"}],
                  report_id=rep_id)
    res = automation.send_report_digest(app.mf_store, app.mf_security, sid)
    assert res["ok"], res
    log = _digest_log(app)
    assert "x@y.test" in log
    assert len(log) == 1


def test_dashboard_digest_per_recipient(app, client, env):
    rep_id = _mk_report(client, env["admin"], "All Accounts", "Account", ["Name"])
    did = _mk_dashboard(client, env["admin"], "Dash",
                        [{"report_id": rep_id, "type": "table"}])
    sid = _mk_sub(client, env["admin"],
                  [{"type": "user", "value": env["alice_id"]},
                   {"type": "user", "value": env["bob_id"]}],
                  dashboard_id=did)
    res = automation.send_report_digest(app.mf_store, app.mf_security, sid)
    assert res["ok"], res
    log = _digest_log(app)
    assert "1 record(s)" in log["alice@x.test"]["body"]
    assert "0 record(s)" in log["bob@x.test"]["body"]


def test_refresh_sync_creates_job(app, client, env):
    rep_id = _mk_report(client, env["admin"], "All Accounts", "Account", ["Name"])
    did = _mk_dashboard(client, env["admin"], "Dash",
                        [{"report_id": rep_id, "type": "table"}])
    r = client.post(f"/api/dashboards/{did}/refresh-sync", headers=env["admin"],
                    json={"refresh_schedule": "daily"})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["refresh_schedule"] == "daily"
    assert body["refresh_job_id"]
    jobs = {j["id"]: j for j in app.mf_store.config_all("mf_scheduled_jobs")}
    assert body["refresh_job_id"] in jobs
    assert "refresh_dashboard" in jobs[body["refresh_job_id"]]["code"]


def test_refresh_sync_validation_and_disable(app, client, env):
    did = _mk_dashboard(client, env["admin"], "Dash", [])
    r = client.post(f"/api/dashboards/{did}/refresh-sync", headers=env["admin"],
                    json={"refresh_schedule": "hourly"})
    assert r.status_code == 422
    r = client.post(f"/api/dashboards/{did}/refresh-sync", headers=env["admin"],
                    json={"refresh_schedule": "weekly"})
    jid = r.get_json()["refresh_job_id"]
    assert jid
    r = client.post(f"/api/dashboards/{did}/refresh-sync", headers=env["admin"],
                    json={"refresh_schedule": None})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["refresh_job_id"] is None
    jobs = {j["id"]: j for j in app.mf_store.config_all("mf_scheduled_jobs")}
    assert jid not in jobs


def test_refresh_sync_forbidden_for_non_owner(app, client, env):
    did = _mk_dashboard(client, env["admin"], "Dash", [])
    r = client.post(f"/api/dashboards/{did}/refresh-sync", headers=env["bob"],
                    json={"refresh_schedule": "daily"})
    assert r.status_code == 403


def test_refresh_dashboard_sets_last_run_at(app, client, env):
    rep_id = _mk_report(client, env["admin"], "All Accounts", "Account", ["Name"])
    did = _mk_dashboard(client, env["admin"], "Dash",
                        [{"report_id": rep_id, "type": "table"}])
    res = automation.refresh_dashboard(app.mf_store, app.mf_security, did)
    assert res["ok"], res
    dash = app.mf_store.config_get("mf_dashboards", did)
    assert dash.get("last_run_at")


def test_subscription_crud_still_works(client, env):
    h = env["admin"]
    rep_id = _mk_report(client, h, "All Accounts", "Account", ["Name"])
    sid = _mk_sub(client, h, [{"type": "email", "value": "a@b.test"}],
                  report_id=rep_id)
    r = client.get("/api/report-subscriptions", headers=h)
    assert r.status_code == 200
    assert any(s["id"] == sid for s in r.get_json())
    r = client.put(f"/api/report-subscriptions/{sid}", headers=h,
                   json={"frequency": "weekly"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["frequency"] == "weekly"
    r = client.delete(f"/api/report-subscriptions/{sid}", headers=h)
    assert r.status_code == 200
    r = client.get("/api/report-subscriptions", headers=h)
    assert not any(s["id"] == sid for s in r.get_json())
