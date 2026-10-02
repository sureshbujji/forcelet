"""Tests for P1 features: custom notifications, queues, multi-currency,
approval depth, flow composition (decision/subflow/invocable), Omni-Channel.
"""
import os
import sys
import tempfile

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


def _uid(client, h):
    return client.get("/api/me", headers=h).get_json()["id"]


# ================================================================ queues
def test_queue_crud_admin_only(client, admin, leo):
    r = client.post("/api/queues", headers=leo, json={"name": "Q1"})
    assert r.status_code == 403
    r = client.post("/api/queues", headers=admin,
                    json={"name": "Support Tier 1"})
    assert r.status_code == 201, r.get_json()
    qid = r.get_json()["id"]
    r = client.post("/api/queues", headers=admin, json={"name": ""})
    assert r.status_code == 400
    r = client.get("/api/queues", headers=leo)
    assert any(q["name"] == "Support Tier 1" for q in r.get_json())
    r = client.get(f"/api/queues/{qid}", headers=leo)
    assert r.status_code == 200
    r = client.put(f"/api/queues/{qid}", headers=admin,
                   json={"members": [_uid(client, leo)]})
    assert r.status_code == 200
    assert _uid(client, leo) in r.get_json()["members"]
    r = client.delete(f"/api/queues/{qid}", headers=leo)
    assert r.status_code == 403
    r = client.delete(f"/api/queues/{qid}", headers=admin)
    assert r.status_code == 200


# ================================================================ custom notifications
def _mk_type(client, admin, name="Deal Closed"):
    r = client.post("/api/notification-types", headers=admin, json={
        "name": name, "title_template": "Won: {{Trigger.Name}}",
        "body_template": "Amount {{Trigger.Amount}}"})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def test_notification_type_crud_and_send(client, admin, leo, maya):
    r = client.post("/api/notification-types", headers=leo,
                    json={"name": "Nope"})
    assert r.status_code == 403
    nt = _mk_type(client, admin)
    nid = nt["id"]
    r = client.get("/api/notification-types", headers=leo)
    assert any(t["name"] == "Deal Closed" for t in r.get_json())

    leo_id, maya_id = _uid(client, leo), _uid(client, maya)
    r = client.post(f"/api/notification-types/{nid}/send", headers=admin, json={
        "recipients": {"users": [leo_id, maya_id]}})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["sent"] == 2
    items = client.get("/api/notifications?unread_only=1",
                       headers=leo).get_json()
    assert any(n["ntype"] == "custom:Deal Closed" for n in items)

    # non-admin may only notify themselves
    r = client.post(f"/api/notification-types/{nid}/send", headers=leo, json={
        "recipients": {"users": [maya_id]}})
    assert r.status_code == 403
    r = client.post(f"/api/notification-types/{nid}/send", headers=leo, json={
        "recipients": {"users": [leo_id]}})
    assert r.status_code == 200

    # queue recipients
    r = client.post("/api/queues", headers=admin,
                    json={"name": "NotifyQ", "members": [maya_id]})
    qid = r.get_json()["id"]
    r = client.post(f"/api/notification-types/{nid}/send", headers=admin, json={
        "recipients": {"queues": [qid]}})
    assert r.get_json()["sent"] == 1
    assert r.get_json()["recipients"] == [maya_id]

    r = client.delete(f"/api/notification-types/{nid}", headers=admin)
    assert r.status_code == 200


def test_send_notification_flow_action(client, admin, leo):
    nt = _mk_type(client, admin, name="Big Opp")
    leo_id = _uid(client, leo)
    flow = {"name": "Notify on big opp", "object": "Opportunity",
            "trigger": "on_create", "active": True,
            "actions": [{"type": "send_notification",
                         "notification_type": nt["id"],
                         "recipients": {"users": [leo_id]}}]}
    r = client.post("/api/admin/flows", headers=admin, json=flow)
    assert r.status_code in (200, 201), r.get_json()
    r = client.post("/api/sobjects/Opportunity", headers=admin, json={
        "Name": "Mega", "Stage": "Prospecting", "CloseDate": "2026-12-01",
        "Amount": 99999})
    assert r.status_code == 201, r.get_json()
    items = client.get("/api/notifications?unread_only=1",
                       headers=leo).get_json()
    custom = [n for n in items if n["ntype"] == "custom:Big Opp"]
    assert len(custom) == 1
    assert "Mega" in custom[0]["title"]
    assert "99999" in custom[0]["body"]


# ================================================================ multi-currency
def test_currency_defaults_and_conversion(client, admin):
    r = client.get("/api/currencies", headers=admin)
    assert r.status_code == 200
    cur = {c["code"]: c for c in r.get_json()}
    assert cur["USD"]["is_corporate"] is True

    # dated rates: EUR strengthens over time
    r = client.post("/api/currencies/rates", headers=admin, json={
        "currency_code": "EUR", "start_date": "2026-01-01", "rate": 0.9})
    assert r.status_code == 201, r.get_json()
    r = client.post("/api/currencies/rates", headers=admin, json={
        "currency_code": "EUR", "start_date": "2026-06-01", "rate": 0.8})
    assert r.status_code == 201

    r = client.post("/api/currencies/convert", headers=admin, json={
        "amount": 90, "from": "EUR", "to": "USD", "date": "2026-03-01"})
    assert r.status_code == 200
    assert r.get_json()["amount"] == pytest.approx(100.0)  # 90 / 0.9

    r = client.post("/api/currencies/convert", headers=admin, json={
        "amount": 80, "from": "EUR", "to": "USD", "date": "2026-07-01"})
    assert r.get_json()["amount"] == pytest.approx(100.0)  # 80 / 0.8

    # EUR -> GBP cross conversion via corporate
    r = client.post("/api/currencies/rates", headers=admin, json={
        "currency_code": "GBP", "start_date": "2026-01-01", "rate": 0.75})
    assert r.status_code == 201
    r = client.post("/api/currencies/convert", headers=admin, json={
        "amount": 90, "from": "EUR", "to": "GBP", "date": "2026-03-01"})
    # 90 EUR -> 100 USD -> 75 GBP
    assert r.get_json()["amount"] == pytest.approx(75.0)

    # no rate on/before date -> 400
    r = client.post("/api/currencies/convert", headers=admin, json={
        "amount": 10, "from": "EUR", "to": "USD", "date": "2025-01-01"})
    assert r.status_code == 400


def test_currency_admin_guards(client, admin, leo):
    r = client.post("/api/currencies", headers=leo,
                    json={"code": "CHF", "name": "Swiss Franc"})
    assert r.status_code == 403
    r = client.post("/api/currencies", headers=admin,
                    json={"code": "US", "name": "Bad"})
    assert r.status_code == 400
    r = client.post("/api/currencies", headers=admin,
                    json={"code": "CHF", "name": "Swiss Franc"})
    assert r.status_code == 201
    r = client.post("/api/currencies/rates", headers=admin, json={
        "currency_code": "CHF", "start_date": "2026-01-01", "rate": -1})
    assert r.status_code == 400
    r = client.post("/api/currencies/rates", headers=admin, json={
        "currency_code": "USD", "start_date": "2026-01-01", "rate": 2})
    assert r.status_code == 400  # corporate rate is always 1.0
    # switch corporate currency
    r = client.put("/api/currencies/EUR/corporate", headers=admin)
    assert r.status_code == 200
    assert r.get_json()["is_corporate"] is True
    r = client.get("/api/currencies", headers=admin)
    corps = [c for c in r.get_json() if c["is_corporate"]]
    assert len(corps) == 1 and corps[0]["code"] == "EUR"


# ================================================================ approval depth
def _mk_process(client, admin, name, steps, obj="Opportunity"):
    r = client.post("/api/admin/approval-processes", headers=admin, json={
        "name": name, "object": obj, "active": True, "steps": steps})
    assert r.status_code in (200, 201), r.get_json()
    return r.get_json()


def _submit(client, admin, proc=None, **fields):
    # Record BEFORE the process: the create pipeline auto-submits when a
    # process already matches, which would make the explicit submit below
    # fail with "already pending".
    body = {"Name": "Appr", "Stage": "Proposal", "CloseDate": "2026-12-01"}
    body.update(fields)
    r = client.post("/api/sobjects/Opportunity", headers=admin, json=body)
    assert r.status_code == 201, r.get_json()
    rid = r.get_json()["Id"]
    if proc:
        _mk_process(client, admin, proc[0], proc[1])
    r = client.post(f"/api/sobjects/Opportunity/{rid}/submit-approval",
                    headers=admin)
    assert r.status_code in (200, 201), r.get_json()
    return r.get_json()


def test_approval_multistep_advance(client, admin, leo, maya):
    leo_id, maya_id = _uid(client, leo), _uid(client, maya)
    req = _submit(client, admin, ("Two step", [
        {"name": "Manager", "approver": {"type": "user", "id": leo_id}},
        {"name": "Finance", "approver": {"type": "user", "id": maya_id}},
    ]))
    assert req["status"] == "Pending"
    assert req["current_step"] == 0
    rid = req["id"]
    # step 1 approver (leo) approves -> still pending, advanced
    r = client.post(f"/api/approvals/{rid}/approve", headers=leo, json={})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == "Pending"
    assert r.get_json()["current_step"] == 1
    # maya (step 2) was notified
    items = client.get("/api/notifications?unread_only=1",
                       headers=maya).get_json()
    assert any(n["ntype"] == "approval" for n in items)
    # step 2 approver cannot act before their turn... they can now
    r = client.post(f"/api/approvals/{rid}/approve", headers=maya, json={})
    assert r.get_json()["status"] == "Approved"
    assert len(r.get_json()["history"]) == 2


def test_approval_reject_ends_immediately(client, admin, leo, maya):
    leo_id, maya_id = _uid(client, leo), _uid(client, maya)
    req = _submit(client, admin, ("Reject fast", [
        {"name": "S1", "approver": {"type": "user", "id": leo_id}},
        {"name": "S2", "approver": {"type": "user", "id": maya_id}},
    ]), Name="Rej")
    rid = req["id"]
    r = client.post(f"/api/approvals/{rid}/reject", headers=leo,
                    json={"comment": "no"})
    assert r.get_json()["status"] == "Rejected"


def test_approval_queue_and_skip(client, admin, leo, maya):
    leo_id, maya_id = _uid(client, leo), _uid(client, maya)
    r = client.post("/api/queues", headers=admin,
                    json={"name": "ApproversQ", "members": [maya_id]})
    qid = r.get_json()["id"]
    # Amount 0 -> first step skipped, queue step pending
    req = _submit(client, admin, ("Queue+skip", [
        {"name": "Auto", "approver": {"type": "user", "id": leo_id},
         "skip_if": {"==": [{"field": "Amount"}, 0]}},
        {"name": "Queue step", "approver": {"type": "queue", "id": qid}},
    ]), Name="SkipMe", Amount=0)
    assert req["status"] == "Pending"
    assert req["current_step"] == 1
    assert req["history"][0]["decision"] == "Skipped"
    # queue member (maya) can approve
    r = client.post(f"/api/approvals/{req['id']}/approve", headers=maya,
                    json={})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == "Approved"
    # non-member (leo) cannot approve the queue step. The process already
    # exists, so creating the record auto-submits; pull it from the inbox.
    r = client.post("/api/sobjects/Opportunity", headers=admin, json={
        "Name": "SkipMe2", "Stage": "Proposal", "CloseDate": "2026-12-01",
        "Amount": 0})
    rid2 = r.get_json()["Id"]
    inbox = client.get("/api/approvals", headers=maya).get_json()
    req2 = next(i for i in inbox if i["record_id"] == rid2)
    assert req2["status"] == "Pending"
    r = client.post(f"/api/approvals/{req2['id']}/approve", headers=leo,
                    json={})
    assert r.status_code == 422


def test_approval_dynamic_field_approver(client, admin, leo, maya):
    maya_id = _uid(client, maya)
    # add a user-lookup field to carry the dynamic approver
    r = client.post("/api/admin/objects/Opportunity/fields", headers=admin,
                    json={"name": "Approver__c", "label": "Approver",
                          "type": "Text", "length": 36})
    assert r.status_code in (200, 201), r.get_json()
    req = _submit(client, admin, ("Dynamic", [
        {"name": "Dyn", "approver": {"type": "field", "field": "Approver__c"}},
    ]), Name="DynAppr", Approver__c=maya_id)
    rid = req["id"]
    r = client.post(f"/api/approvals/{rid}/approve", headers=leo, json={})
    assert r.status_code == 422  # leo is not the dynamic approver
    r = client.post(f"/api/approvals/{rid}/approve", headers=maya, json={})
    assert r.get_json()["status"] == "Approved"


# ================================================================ flow composition
def _mk_opp(client, admin, **kw):
    body = {"Name": "T", "Stage": "Prospecting", "CloseDate": "2026-12-01"}
    body.update(kw)
    r = client.post("/api/sobjects/Opportunity", headers=admin, json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["Id"]


def test_flow_decision_branching(client, admin):
    flow = {"name": "Decider", "object": "Opportunity", "trigger": "on_create",
            "active": True, "actions": [{
                "type": "decision",
                "outcomes": [{
                    "label": "Big",
                    "condition": {"==": [{"field": "Stage"}, "Negotiation"]},
                    "actions": [{"type": "set_fields", "object": "Opportunity",
                                 "fields": {"Description": "big deal"}}]}],
                "default_actions": [{"type": "set_fields",
                                     "object": "Opportunity",
                                     "fields": {"Description": "regular"}}]}]}
    r = client.post("/api/admin/flows", headers=admin, json=flow)
    assert r.status_code in (200, 201), r.get_json()
    oid = _mk_opp(client, admin, Name="BigOne", Stage="Negotiation")
    assert client.get(f"/api/sobjects/Opportunity/{oid}",
                      headers=admin).get_json()["Description"] == "big deal"
    oid = _mk_opp(client, admin, Name="SmallOne")
    assert client.get(f"/api/sobjects/Opportunity/{oid}",
                      headers=admin).get_json()["Description"] == "regular"


def test_flow_subflow_with_inputs(client, admin):
    sub = {"name": "SubTagger", "object": "Opportunity", "trigger": "none",
           "active": True, "actions": [
               {"type": "set_fields", "object": "Opportunity",
                "fields": {"Description": "tagged:{{Trigger.tag}}"}}]}
    r = client.post("/api/admin/flows", headers=admin, json=sub)
    assert r.status_code in (200, 201), r.get_json()
    main = {"name": "MainCaller", "object": "Opportunity",
            "trigger": "on_create",
            "active": True, "actions": [
                {"type": "subflow", "flow": "SubTagger",
                 "inputs": {"tag": "vip"}}]}
    r = client.post("/api/admin/flows", headers=admin, json=main)
    assert r.status_code in (200, 201), r.get_json()
    oid = _mk_opp(client, admin, Name="Sub")
    desc = client.get(f"/api/sobjects/Opportunity/{oid}",
                      headers=admin).get_json()["Description"]
    assert desc == "tagged:vip"


def test_flow_subflow_cycle_guard(client, admin):
    a = {"name": "CycleA", "object": "Opportunity", "trigger": "on_create",
         "active": True, "actions": [
             {"type": "subflow", "flow": "CycleB", "inputs": {}}]}
    b = {"name": "CycleB", "object": "Opportunity", "trigger": "none",
         "active": True, "actions": [
             {"type": "subflow", "flow": "CycleA", "inputs": {}}]}
    for f in (a, b):
        r = client.post("/api/admin/flows", headers=admin, json=f)
        assert r.status_code in (200, 201)
    # must terminate via MAX_FLOW_DEPTH, not hang
    _mk_opp(client, admin, Name="Cyc")


def test_flow_invocable_convert_currency(client, admin):
    r = client.get("/api/invocable-actions", headers=admin)
    assert "Convert Currency" in r.get_json()
    r = client.post("/api/currencies/rates", headers=admin, json={
        "currency_code": "EUR", "start_date": "2026-01-01", "rate": 0.9})
    assert r.status_code == 201
    flow = {"name": "CurrencyFlow", "object": "Opportunity",
            "trigger": "on_create", "active": True, "actions": [
                {"type": "invocable", "name": "Convert Currency",
                 "inputs": {"amount": "{{Trigger.Amount}}", "from": "EUR",
                            "to": "USD", "date": "2026-02-01"}},
                {"type": "set_fields", "object": "Opportunity",
                 "fields": {"Description": "usd:{{Trigger.converted_amount}}"}}]}
    r = client.post("/api/admin/flows", headers=admin, json=flow)
    assert r.status_code in (200, 201), r.get_json()
    r = client.post("/api/sobjects/Opportunity", headers=admin, json={
        "Name": "Euro deal", "Stage": "Prospecting",
        "CloseDate": "2026-12-01", "Amount": 90})
    oid = r.get_json()["Id"]
    desc = client.get(f"/api/sobjects/Opportunity/{oid}",
                      headers=admin).get_json()["Description"]
    assert desc.startswith("usd:100")


# ================================================================ omni-channel
def _mk_agent(client, admin, username):
    r = client.post("/api/admin/users", headers=admin, json={
        "username": username, "name": username.title(),
        "profile": "Standard User", "password": "forcelet"})
    assert r.status_code == 201, r.get_json()
    h = login(client, username, "forcelet")
    return h, _uid(client, h)


def _omni_setup(client, admin, members):
    r = client.post("/api/queues", headers=admin,
                    json={"name": "OmniQ", "members": members})
    qid = r.get_json()["id"]
    r = client.post("/api/omni/routing-configs", headers=admin, json={
        "name": "Case routing", "channel": "Cases", "queue": qid,
        "priority": 0})
    assert r.status_code == 201, r.get_json()
    return qid


def test_omni_presence_and_routing(client, admin):
    h1, id1 = _mk_agent(client, admin, "agent1")
    h2, id2 = _mk_agent(client, admin, "agent2")
    _omni_setup(client, admin, [id1, id2])

    r = client.put("/api/omni/presence", headers=h1,
                   json={"status": "Available"})
    assert r.get_json()["status"] == "Available"
    r = client.put("/api/omni/presence", headers=h1,
                   json={"status": "Napping"})
    assert r.status_code == 400

    # agent2 stays Offline -> all work goes to agent1
    r = client.post("/api/sobjects/Case", headers=admin, json={
        "Subject": "Help", "Status": "New"})
    case_id = r.get_json()["Id"]
    r = client.post("/api/omni/work", headers=admin, json={
        "object_name": "Case", "record_id": case_id, "channel": "Cases"})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["status"] == "assigned"
    assert r.get_json()["assigned_to"] == id1

    # agent1's own queue
    r = client.get("/api/omni/work/mine", headers=h1)
    assert len(r.get_json()) == 1
    wid = r.get_json()[0]["id"]
    r = client.post(f"/api/omni/work/{wid}/complete", headers=h1)
    assert r.get_json()["status"] == "completed"


def test_omni_least_loaded_and_capacity(client, admin):
    h1, id1 = _mk_agent(client, admin, "agenta")
    h2, id2 = _mk_agent(client, admin, "agentb")
    _omni_setup(client, admin, [id1, id2])
    client.put("/api/omni/presence", headers=h1, json={"status": "Available"})
    client.put("/api/omni/presence", headers=h2, json={"status": "Available"})
    # cap agent A at 1
    r = client.put("/api/omni/capacity", headers=admin,
                   json={"user_id": id1, "max_capacity": 1})
    assert r.status_code == 200

    def _work():
        r = client.post("/api/omni/work", headers=admin, json={
            "object_name": "Case", "record_id": "x", "channel": "Cases"})
        assert r.status_code == 201
        return r.get_json()

    w1 = _work()
    w2 = _work()
    w3 = _work()
    # least-loaded: first to A (tie broken somehow), second to B, third to B
    # (A is at capacity)
    assignees = [w1["assigned_to"], w2["assigned_to"], w3["assigned_to"]]
    assert id1 in assignees and id2 in assignees
    assert assignees.count(id1) == 1  # capacity respected
    assert w3["assigned_to"] == id2

    # decline returns work to the queue and it can re-route
    r = client.post(f"/api/omni/work/{w3['id']}/decline", headers=h2)
    assert r.get_json()["status"] == "queued"
    r = client.post(f"/api/omni/work/{w3['id']}/route", headers=admin)
    assert r.get_json()["routed"] == 1


def test_omni_no_eligible_agent_stays_queued(client, admin):
    h1, id1 = _mk_agent(client, admin, "agentx")
    _omni_setup(client, admin, [id1])
    # agent offline -> stays queued
    r = client.post("/api/omni/work", headers=admin, json={
        "object_name": "Case", "record_id": "y", "channel": "Cases"})
    assert r.get_json()["status"] == "queued"
    assert r.get_json()["assigned_to"] is None
    # supervisor snapshot shows it
    qid = client.get("/api/queues", headers=admin).get_json()[0]["id"]
    r = client.get(f"/api/omni/queues/{qid}/work", headers=admin)
    assert len(r.get_json()["queued"]) == 1


# ---------------------------------------------------------------------------
# Generic admin CRUD for the P1 config tables (drives the Setup UI tiles)
# ---------------------------------------------------------------------------

def _rows(r):
    d = r.get_json()
    return d["rows"] if isinstance(d, dict) else d


def test_admin_crud_queues(client, admin):
    r = client.post("/api/admin/queues", headers=admin,
                    json={"name": "Tier1", "members": ["u_1", "u_2"]})
    assert r.status_code == 201
    qid = r.get_json()["id"]
    assert r.get_json()["members"] == ["u_1", "u_2"]
    r = client.get("/api/admin/queues", headers=admin)
    assert any(q["id"] == qid for q in _rows(r))
    r = client.patch(f"/api/admin/queues/{qid}", headers=admin,
                     json={"description": "Front line"})
    assert r.get_json()["description"] == "Front line"
    r = client.delete(f"/api/admin/queues/{qid}", headers=admin)
    assert r.status_code == 200


def test_admin_crud_notification_types(client, admin):
    r = client.post("/api/admin/notification-types", headers=admin,
                    json={"name": "case_escalated", "label": "Case escalated",
                          "recipients": [{"type": "queue", "id": "q1"}]})
    assert r.status_code == 201
    nid = r.get_json()["id"]
    r = client.post("/api/admin/notification-types", headers=admin,
                    json={"label": "no name"})
    assert r.status_code == 422
    client.delete(f"/api/admin/notification-types/{nid}", headers=admin)


def test_admin_currency_validation_and_corporate(client, admin):
    # seeded currencies exist; duplicates rejected, codes uppercased
    r = client.post("/api/admin/currencies", headers=admin,
                    json={"code": "usd", "name": "Duplicate"})
    assert r.status_code == 422
    r = client.post("/api/admin/currencies", headers=admin,
                    json={"code": "CHF", "name": "Swiss Franc"})
    assert r.status_code == 201
    assert r.get_json()["code"] == "CHF"
    r = client.post("/api/admin/currencies", headers=admin,
                    json={"code": "US", "name": "Bad"})
    assert r.status_code == 422
    # switching corporate unsets the previous one
    chf = [c for c in _rows(client.get("/api/admin/currencies", headers=admin))
           if c["code"] == "CHF"][0]
    r = client.patch(f"/api/admin/currencies/{chf['id']}", headers=admin,
                     json={"is_corporate": True})
    assert r.status_code == 200
    corps = [c["code"] for c in _rows(
        client.get("/api/admin/currencies", headers=admin))
        if c.get("is_corporate")]
    assert corps == ["CHF"]


def test_admin_exchange_rate_validation(client, admin):
    r = client.post("/api/admin/exchange-rates", headers=admin,
                    json={"name": "r", "from_currency": "eur",
                          "to_currency": "usd", "rate": "1.10",
                          "effective_date": "2026-01-01"})
    assert r.status_code == 201
    assert r.get_json()["rate"] == 1.10
    # L10: stored in the canonical shape read by get_rate/convert
    assert r.get_json()["currency_code"] == "EUR"
    assert r.get_json()["start_date"] == "2026-01-01"
    r = client.post("/api/admin/exchange-rates", headers=admin,
                    json={"name": "bad", "from_currency": "CHF",
                          "to_currency": "USD", "rate": "nope",
                          "effective_date": "2026-01-01"})
    assert r.status_code == 422


def test_admin_crud_omni_config(client, admin):
    r = client.post("/api/admin/service-channels", headers=admin,
                    json={"name": "Chats", "object": "Case"})
    assert r.status_code == 201
    r = client.post("/api/admin/routing-configs", headers=admin,
                    json={"name": "chat-route", "channel_id": "c1",
                          "queue_id": "q1"})
    assert r.status_code == 201
    r = client.post("/api/admin/routing-configs", headers=admin,
                    json={"channel_id": "c1", "queue_id": "q1"})
    assert r.status_code == 422  # name required
    r = client.post("/api/admin/invocable-actions", headers=admin,
                    json={"name": "score_lead",
                          "inputs": [{"name": "lead_id", "type": "text",
                                      "required": True}]})
    assert r.status_code == 201
    assert r.get_json()["inputs"][0]["name"] == "lead_id"
