"""Tests for the L-gap engine fixes: L5 (transactional lead conversion),
L7 (flow execution governor), L8 (unified save pipeline for system DML),
L11 (loop / get_records / assignment / delete_record / wait flow actions).
"""
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forcelet import automation
from forcelet.api import create_app
from helpers import login


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


def _ctx(client):
    app = client.application
    return app.mf_store, app.mf_registry, app.mf_security


def _user(store, username):
    return next(u for u in store.meta_all("mf_users")
                if u["username"] == username)


def _mk_lead(c, h, **kw):
    body = {"LastName": "Prospect", "Company": "ProspectCo", "Status": "New"}
    body.update(kw)
    r = c.post("/api/sobjects/Lead", headers=h, json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["Id"]


def _mk_flow(c, h, obj, actions, name="LFlow", trigger="on_create"):
    r = c.post("/api/admin/flows", headers=h, json={
        "name": name, "object": obj, "trigger": trigger,
        "active": True, "actions": actions})
    assert r.status_code in (200, 201), r.get_json()
    body = r.get_json()
    return body.get("id") or body.get("Id")


def _history(store, field_name):
    return store._fetchall(
        "SELECT * FROM mf_history WHERE field_name=?", (field_name,))


# ------------------------------------------------------------------ L5
def test_lead_convert_contact_failure_compensates_account(client, admin):
    h, (store, registry, security) = admin, _ctx(client)
    # any Contact insert now fails in after_insert
    r = client.post("/api/admin/triggers", headers=h, json={
        "name": "BreakContacts", "object": "Contact", "active": True,
        "events": ["after_insert"], "code": "errors.append('no contacts')"})
    assert r.status_code == 201
    lid = _mk_lead(client, h)
    r = client.post(f"/api/sobjects/Lead/{lid}/convert", headers=h, json={})
    assert r.status_code == 422, r.get_json()
    # the Account created mid-conversion must have been rolled back
    orphans = [a for a in store.query("Account", limit=10000)
               if a.get("Name") == "ProspectCo"]
    assert orphans == []
    # and the lead is untouched
    lead = store.get("Lead", lid)
    assert lead["Status"] != "Converted"


def test_lead_convert_success_still_works(client, admin):
    h = admin
    lid = _mk_lead(client, h, FirstName="OK")
    r = client.post(f"/api/sobjects/Lead/{lid}/convert", headers=h, json={})
    assert r.status_code == 201, r.get_json()
    res = r.get_json()
    assert res["account_id"] and res["contact_id"]


# ------------------------------------------------------------------ L7
def test_flow_action_budget_caps_runaway_loop(client, admin):
    h, (store, registry, security) = admin, _ctx(client)
    items = list(range(2000))
    _mk_flow(client, h, "Opportunity", [{
        "type": "loop", "collection": items, "item_var": "it",
        "actions": [{"type": "log", "message": "iter"}]}],
        name="Runaway")
    r = client.post("/api/sobjects/Opportunity", headers=h, json={
        "Name": "Budget", "Stage": "Prospecting", "CloseDate": "2026-12-01"})
    assert r.status_code == 201, r.get_json()
    logs = _history(store, "__flow__")
    assert 0 < len(logs) < 2000  # stopped by the governor, not the list
    trips = _history(store, "__flow_governor__")
    assert len(trips) == 1
    assert "budget" in (trips[0]["new_value"] or "")


def test_flow_callout_budget(client, admin, monkeypatch):
    h, (store, registry, security) = admin, _ctx(client)
    calls = []

    def fake_callout(store_, credential, method="POST", path="",
                     headers=None, body=None):
        calls.append(path)
        return {"status": 200, "ok": True, "body": ""}

    monkeypatch.setattr(automation, "invoke_callout", fake_callout)
    _mk_flow(client, h, "Lead", [
        {"type": "http_callout", "credential": "", "path": f"/x/{i}"}
        for i in range(15)], name="CalloutStorm")
    _mk_lead(client, h)
    assert len(calls) == automation.FLOW_CALLOUT_BUDGET
    assert len(_history(store, "__flow_governor__")) == 1


# ------------------------------------------------------------------ L8
def _sysops(client, username="admin"):
    store, registry, security = _ctx(client)
    user = _user(store, username)
    return automation._trigger_dml_ops(store, registry, security, user, 0, [])


def test_system_create_runs_assignment_rules(client, admin):
    h = admin
    store = _ctx(client)[0]
    r = client.post("/api/admin/assignment-rules", headers=h, json={
        "name": "ToLeo", "object": "Lead", "active": True,
        "assignee": {"type": "user", "username": "leo"}})
    assert r.status_code in (200, 201), r.get_json()
    query, create, update = _sysops(client)
    rec = create("Lead", {"LastName": "Sys", "Company": "SysCo"})
    leo = _user(store, "leo")
    assert rec["owner_id"] == leo["id"]


def test_system_create_assigns_autonumber(client, admin):
    h = admin
    r = client.post("/api/admin/objects", headers=h, json={
        "name": "Invoice", "label": "Invoice", "plural": "Invoices"})
    assert r.status_code in (200, 201), r.get_json()
    r = client.post("/api/admin/objects/Invoice/fields", headers=h, json={
        "name": "InvNo", "label": "Invoice No", "type": "AutoNumber",
        "auto_prefix": "INV-", "auto_start": 100, "auto_width": 5})
    assert r.status_code == 201, r.get_json()
    _, create, _ = _sysops(client)
    rec = create("Invoice", {})
    assert rec["InvNo"] == "INV-00100"
    rec2 = create("Invoice", {})
    assert rec2["InvNo"] == "INV-00101"


def test_system_create_emits_change_event(client, admin):
    store = _ctx(client)[0]
    _, create, _ = _sysops(client)
    rec = create("Task", {"Subject": "sys task"})
    rows = store._fetchall(
        "SELECT * FROM mf_change_events WHERE object_name='Task' AND record_id=?",
        (rec["id"],))
    assert len(rows) == 1
    assert rows[0]["event"] == "create"


def test_system_create_fires_webhooks(client, admin):
    h = admin
    store = _ctx(client)[0]
    r = client.post("/api/admin/webhooks", headers=h, json={
        "name": "Hook", "object": "Task", "events": ["create"],
        "url": "http://127.0.0.1:9/nope", "active": True})
    assert r.status_code in (200, 201), r.get_json()
    _, create, _ = _sysops(client)
    rec = create("Task", {"Subject": "hook task"})
    deadline = time.time() + 12
    rows = []
    while time.time() < deadline:  # webhook delivery runs on a thread
        rows = store._fetchall(
            "SELECT * FROM mf_webhook_deliveries WHERE event='Task.create'",
            ())
        if rows:
            break
        time.sleep(0.25)
    assert rows, "system-path create did not dispatch the webhook"


def test_system_create_duplicate_block(client, admin):
    h = admin
    mid = client.post("/api/platform/matching-rules", headers=h, json={
        "Name": "M", "ObjectName": "Lead", "Fields": "Email",
        "MatchType": "Exact", "IsActive": True}).get_json()["Id"]
    r = client.post("/api/platform/duplicate-rules", headers=h, json={
        "Name": "D", "ObjectName": "Lead", "MatchingRuleId": mid,
        "Action": "Block", "AppliesOn": "Both", "IsActive": True})
    assert r.status_code == 201, r.get_json()
    _, create, _ = _sysops(client)
    create("Lead", {"LastName": "Dup", "Company": "DupCo",
                    "Email": "dup@example.com"})
    with pytest.raises(automation.TriggerAbort):
        create("Lead", {"LastName": "Dup2", "Company": "DupCo",
                        "Email": "dup@example.com"})


def test_system_update_runs_post_automation(client, admin):
    store = _ctx(client)[0]
    _, create, update = _sysops(client)
    rec = create("Task", {"Subject": "before"})
    new_rec = update("Task", rec["id"], {"Subject": "after"})
    assert new_rec["Subject"] == "after"
    rows = store._fetchall(
        "SELECT * FROM mf_change_events WHERE object_name='Task' AND record_id=?",
        (rec["id"],))
    assert [r["event"] for r in rows] == ["create", "update"]
    hist = store._fetchall(
        "SELECT * FROM mf_history WHERE object_name='Task' AND record_id=?",
        (rec["id"],))
    assert hist, "system-path update did not log history"


# ------------------------------------------------------------------ L11
def test_flow_loop_action(client, admin):
    h, (store, registry, security) = admin, _ctx(client)
    _mk_flow(client, h, "Lead", [{
        "type": "loop", "collection": ["a", "b", "c"], "item_var": "it",
        "actions": [{"type": "log", "message": "saw:{{Trigger.it}}"}]}],
        name="Looper")
    _mk_lead(client, h)
    msgs = sorted(r["new_value"] for r in _history(store, "__flow__"))
    assert msgs == ["saw:a", "saw:b", "saw:c"]


def test_flow_get_records_and_assignment(client, admin):
    h = admin
    store, registry, security = _ctx(client)
    user = _user(store, "admin")
    acct = client.post("/api/sobjects/Account", headers=h,
                       json={"Name": "ACME"}).get_json()["Id"]
    for ln in ("One", "Two"):
        client.post("/api/sobjects/Contact", headers=h,
                    json={"LastName": ln, "AccountId": acct})
    record = {"_object": "Lead", "id": "x"}
    automation._run_flow_action(
        store, registry, security,
        {"type": "get_records", "object": "Contact",
         "filters": {"AccountId": acct}, "variable": "cs"},
        record, user, 0, "Lead", automation._new_flow_budget())
    assert len(record["cs"]) == 2
    automation._run_flow_action(
        store, registry, security,
        {"type": "assignment", "variable": "greeting", "value": "hello"},
        record, user, 0, "Lead", automation._new_flow_budget())
    assert record["greeting"] == "hello"


def test_flow_delete_record(client, admin):
    h, (store, registry, security) = admin, _ctx(client)
    tid = client.post("/api/sobjects/Task", headers=h,
                      json={"Subject": "doomed"}).get_json()["Id"]
    _mk_flow(client, h, "Lead", [
        {"type": "assignment", "variable": "doomed", "value": tid},
        {"type": "delete_record", "object": "Task",
         "record_id": "{{Trigger.doomed}}"}], name="Deleter")
    _mk_lead(client, h)
    assert store.get("Task", tid) is None


def test_flow_wait_persists_and_resumes(client, admin):
    h, (store, registry, security) = admin, _ctx(client)
    _mk_flow(client, h, "Lead", [
        {"type": "log", "message": "before"},
        {"type": "wait", "duration_minutes": 60},
        {"type": "log", "message": "after"}], name="Waiter")
    _mk_lead(client, h)
    msgs = [r["new_value"] for r in _history(store, "__flow__")]
    assert "before" in msgs and "after" not in msgs
    waits = store.config_all("mf_flow_waits")
    assert len(waits) == 1
    # fast-forward the wait into the past and run the scheduler hook
    w = dict(waits[0])
    w["resume_at"] = "2020-01-01T00:00:00+00:00"
    store.config_put("mf_flow_waits", w)
    resumed = automation.process_due_flow_waits(store, registry, security)
    assert resumed == [w["id"]]
    msgs = [r["new_value"] for r in _history(store, "__flow__")]
    assert "after" in msgs
    assert store.config_all("mf_flow_waits") == []


def test_flow_wait_past_until_is_noop(client, admin):
    h, (store, registry, security) = admin, _ctx(client)
    _mk_flow(client, h, "Lead", [
        {"type": "wait", "until": "2020-01-01T00:00:00+00:00"},
        {"type": "log", "message": "continued"}], name="PastWait")
    _mk_lead(client, h)
    assert store.config_all("mf_flow_waits") == []
    assert [r["new_value"] for r in _history(store, "__flow__")] == ["continued"]


def test_flow_action_type_validation(client, admin):
    h = admin
    r = client.post("/api/admin/flows", headers=h, json={
        "name": "Bad", "object": "Lead", "trigger": "on_create",
        "active": True, "actions": [{"type": "teleport"}]})
    assert r.status_code == 422, r.get_json()
    assert "teleport" in str(r.get_json())
    r = client.post("/api/admin/flows", headers=h, json={
        "name": "Good", "object": "Lead", "trigger": "on_create",
        "active": True, "actions": [
            {"type": "loop", "collection": [], "actions": []},
            {"type": "get_records", "object": "Lead"},
            {"type": "assignment", "variable": "x", "value": 1},
            {"type": "delete_record", "object": "Task"},
            {"type": "wait", "duration_minutes": 5},
            {"type": "decision", "outcomes": [{
                "label": "o", "condition": {},
                "actions": [{"type": "bogus"}]}]}]})
    assert r.status_code == 422  # nested unknown type is caught too
