"""Tests for Forcelet enhancements: validation rules, formula fields, flows,
approvals, record types, reports, history, criteria sharing, duplicates,
permission sets, webhooks, CSV import/export."""
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


# ------------------------------------------------------------ validation rules
def test_validation_rule_blocks_past_close_date(client, admin):
    r = client.post("/api/sobjects/Opportunity", headers=admin,
                    json={"Name": "Bad dates", "Stage": "Prospecting",
                          "CloseDate": "2020-01-01"})
    assert r.status_code == 422
    assert any("Close Date" in d for d in r.get_json()["details"])


def test_validation_rule_allows_closed_won_past_date(client, admin):
    r = client.post("/api/sobjects/Opportunity", headers=admin,
                    json={"Name": "Won long ago", "Stage": "Closed Won",
                          "CloseDate": "2020-01-01"})
    assert r.status_code == 201, r.get_json()


def test_admin_can_create_validation_rule(client, admin):
    r = client.post("/api/admin/validation-rules", headers=admin, json={
        "name": "Amount positive", "object": "Opportunity", "active": True,
        "condition": {"<": [{"field": "Amount"}, 0]},
        "message": "Amount cannot be negative"})
    assert r.status_code == 201
    r = client.post("/api/sobjects/Opportunity", headers=admin,
                    json={"Name": "Neg", "Stage": "Prospecting", "Amount": -5,
                          "CloseDate": "2026-12-01"})
    assert r.status_code == 422
    assert any("negative" in d for d in r.get_json()["details"])


# ------------------------------------------------------------ formula fields
def test_formula_field_computed_on_read(client, admin):
    r = client.post("/api/sobjects/Opportunity", headers=admin,
                    json={"Name": "Formula check", "Stage": "Prospecting",
                          "CloseDate": "2026-12-01"})
    rid = r.get_json()["Id"]
    r = client.get(f"/api/sobjects/Opportunity/{rid}", headers=admin)
    assert r.get_json()["DaysOpen"] >= 0
    # formula fields are not writable
    r = client.patch(f"/api/sobjects/Opportunity/{rid}", headers=admin,
                     json={"DaysOpen": 3})
    assert r.status_code == 422


# ------------------------------------------------------------ flows
def test_flow_creates_task_on_negotiation(client, admin):
    r = client.post("/api/sobjects/Opportunity", headers=admin,
                    json={"Name": "Flow opp", "Stage": "Proposal",
                          "CloseDate": "2026-12-01"})
    rid = r.get_json()["Id"]
    before = client.get("/api/sobjects/Task", headers=admin).get_json()
    r = client.patch(f"/api/sobjects/Opportunity/{rid}", headers=admin,
                     json={"Stage": "Negotiation"})
    assert r.status_code == 200, r.get_json()
    after = client.get("/api/sobjects/Task", headers=admin).get_json()
    assert len(after) == len(before) + 1
    assert "Flow opp" in after[0]["Subject"]


# ------------------------------------------------------------ approvals
def test_approval_auto_submit_and_manager_decides(client, admin, leo, maya):
    r = client.post("/api/sobjects/Opportunity", headers=leo,
                    json={"Name": "Big discount", "Stage": "Proposal",
                          "CloseDate": "2026-12-01", "DiscountPercent": 25})
    assert r.status_code == 201, r.get_json()
    rid = r.get_json()["Id"]
    # record is now locked for the owner
    r = client.patch(f"/api/sobjects/Opportunity/{rid}", headers=leo,
                     json={"Amount": 100})
    assert r.status_code == 423
    # maya (leo's manager) sees it in her inbox and approves
    inbox = client.get("/api/approvals", headers=maya).get_json()
    assert any(i["record_id"] == rid for i in inbox)
    req_id = next(i["id"] for i in inbox if i["record_id"] == rid)
    r = client.post(f"/api/approvals/{req_id}/approve", headers=maya,
                    json={"comment": "ok"})
    assert r.get_json()["status"] == "Approved"
    # unlocked after decision
    r = client.patch(f"/api/sobjects/Opportunity/{rid}", headers=leo,
                     json={"Amount": 100})
    assert r.status_code == 200


def test_non_approver_cannot_decide(client, admin, leo):
    r = client.post("/api/sobjects/Opportunity", headers=admin,
                    json={"Name": "Needs approval", "Stage": "Proposal",
                          "CloseDate": "2026-12-01", "DiscountPercent": 30})
    rid = r.get_json()["Id"]
    inbox = client.get("/api/approvals", headers=admin).get_json()
    req_id = next(i["id"] for i in inbox if i["record_id"] == rid)
    r = client.post(f"/api/approvals/{req_id}/reject", headers=leo, json={})
    assert r.status_code == 422  # leo is not the approver


# ------------------------------------------------------------ record types
def test_record_type_picklist_override(client, admin):
    r = client.get("/api/describe/Opportunity?record_type=Enterprise", headers=admin)
    stages = next(f for f in r.get_json()["fields"] if f["name"] == "Stage")
    assert "Security Review" in stages["picklist_values"]
    r = client.get("/api/describe/Opportunity", headers=admin)
    stages = next(f for f in r.get_json()["fields"] if f["name"] == "Stage")
    assert "Security Review" not in stages["picklist_values"]


# ------------------------------------------------------------ reports
def test_report_run_with_grouping(client, admin):
    reps = client.get("/api/reports", headers=admin).get_json()
    rep = next(r for r in reps if r["name"] == "Pipeline by Stage")
    r = client.get(f"/api/reports/{rep['id']}/run", headers=admin)
    body = r.get_json()
    assert body["row_count"] >= 2
    assert body["groups"], "should group by stage"
    assert any(g["key"] == "Proposal" for g in body["groups"])


def test_admin_can_create_report(client, admin):
    r = client.post("/api/admin/reports", headers=admin, json={
        "name": "Hot leads", "object": "Lead", "columns": ["LastName", "Rating"],
        "filters": {"==": [{"field": "Rating"}, "Hot"]}})
    assert r.status_code == 201
    rep_id = r.get_json()["id"]
    body = client.get(f"/api/reports/{rep_id}/run", headers=admin).get_json()
    assert all(row["Rating"] == "Hot" for row in body["rows"])


# ------------------------------------------------------------ history
def test_field_history_logged(client, admin):
    r = client.post("/api/sobjects/Account", headers=admin, json={"Name": "Hist Co"})
    rid = r.get_json()["Id"]
    client.patch(f"/api/sobjects/Account/{rid}", headers=admin,
                 json={"Phone": "555-0100", "Industry": "Technology"})
    hist = client.get(f"/api/sobjects/Account/{rid}/history", headers=admin).get_json()
    fields = {h["field_name"] for h in hist}
    assert {"Phone", "Industry"} <= fields
    assert all(h["changed_by_name"] == "Ava Admin" for h in hist)


# ------------------------------------------------------------ criteria sharing
def test_criteria_sharing_rule(client, admin):
    # leo creates a critical case; ana (support branch) should see it via the rule
    ana = login(client, "ana")
    leo = login(client, "leo")
    r = client.post("/api/sobjects/Case", headers=leo,
                    json={"Subject": "Outage!", "Priority": "Critical"})
    rid = r.get_json()["Id"]
    r = client.get(f"/api/sobjects/Case/{rid}", headers=ana)
    assert r.status_code == 200
    # a non-critical case stays hidden from ana
    r = client.post("/api/sobjects/Case", headers=leo,
                    json={"Subject": "Minor q", "Priority": "Low"})
    rid2 = r.get_json()["Id"]
    assert client.get(f"/api/sobjects/Case/{rid2}", headers=ana).status_code == 404


# ------------------------------------------------------------ duplicates
def test_duplicate_rule_blocks(client, admin):
    client.post("/api/sobjects/Lead", headers=admin,
                json={"LastName": "Dup", "Company": "DupCo",
                      "Email": "dup@example.com", "Status": "New"})
    r = client.post("/api/sobjects/Lead", headers=admin,
                    json={"LastName": "Dup2", "Company": "DupCo",
                          "Email": "dup@example.com", "Status": "New"})
    assert r.status_code == 409
    assert r.get_json()["duplicates"]
    # bypass flag allows it
    r = client.post("/api/sobjects/Lead?allow_duplicates=true", headers=admin,
                    json={"LastName": "Dup3", "Company": "DupCo",
                          "Email": "dup@example.com", "Status": "New"})
    assert r.status_code == 201


# ------------------------------------------------------------ permission sets
def test_permission_set_grants_delete(client, admin, maya):
    # maya's profile lacks Lead delete; the DeleteLeads set grants it
    r = client.post("/api/sobjects/Lead", headers=maya,
                    json={"LastName": "Gone", "Company": "GoneCo", "Status": "New"})
    assert r.status_code == 201, r.get_json()
    rid = r.get_json()["Id"]
    r = client.delete(f"/api/sobjects/Lead/{rid}", headers=maya)
    assert r.status_code == 200, r.get_json()


# ------------------------------------------------------------ webhooks
def test_webhook_delivery_logged(client, admin):
    r = client.post("/api/admin/webhooks", headers=admin, json={
        "name": "test hook", "object": "Account", "events": ["create"],
        "url": "http://127.0.0.1:9/unreachable", "active": True})
    assert r.status_code == 201
    client.post("/api/sobjects/Account", headers=admin, json={"Name": "Hook Co"})
    import time
    time.sleep(1.5)
    rows = client.get("/api/admin/webhook-deliveries", headers=admin).get_json()
    assert rows and rows[0]["event"] == "Account.create"
    assert rows[0]["status"] == "failed"  # nothing listening; the attempt is logged


# ------------------------------------------------------------ csv import/export
def test_csv_export_and_import(client, admin):
    import io
    r = client.get("/api/sobjects/Lead/export", headers=admin)
    assert r.status_code == 200
    assert "LastName" in r.text
    data = "LastName,Company,Status\nCsvOne,CsvCo,New\nCsvTwo,CsvCo,Working\n"
    r = client.post("/api/admin/import/Lead", headers=admin,
                    data={"file": (io.BytesIO(data.encode()), "leads.csv")},
                    content_type="multipart/form-data")
    body = r.get_json()
    assert body["created"] == 2 and body["failed"] == 0, body


# ------------------------------------------------------------ code triggers
@pytest.fixture()
def ana(client):
    return login(client, "ana")


def test_trigger_before_insert_sets_probability(client, admin):
    r = client.post("/api/sobjects/Opportunity", headers=admin,
                    json={"Name": "Trig opp", "Stage": "Negotiation",
                          "CloseDate": "2027-06-01"})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["Probability"] == 75


def test_trigger_before_insert_on_update(client, admin):
    r = client.post("/api/sobjects/Opportunity", headers=admin,
                    json={"Name": "Trig opp 2", "Stage": "Prospecting",
                          "CloseDate": "2027-06-01"})
    rid = r.get_json()["Id"]
    r = client.patch(f"/api/sobjects/Opportunity/{rid}", headers=admin,
                     json={"Stage": "Proposal"})
    assert r.status_code == 200
    assert r.get_json()["Probability"] == 50


def test_trigger_before_delete_blocks_open_opps(client, admin):
    a = client.post("/api/sobjects/Account", headers=admin,
                    json={"Name": "Doomed"}).get_json()
    client.post("/api/sobjects/Opportunity", headers=admin,
                json={"Name": "Open opp", "Stage": "Prospecting",
                      "CloseDate": "2027-01-01", "AccountId": a["Id"]})
    r = client.delete(f"/api/sobjects/Account/{a['Id']}", headers=admin)
    assert r.status_code == 422
    assert any("open opportunity" in d.lower() for d in r.get_json()["details"])


def test_trigger_before_delete_allows_clean_account(client, admin):
    a = client.post("/api/sobjects/Account", headers=admin,
                    json={"Name": "Clean"}).get_json()
    r = client.delete(f"/api/sobjects/Account/{a['Id']}", headers=admin)
    assert r.status_code == 200


def test_trigger_after_insert_runs_dml(client, admin):
    before = client.get("/api/sobjects/Task", headers=admin).get_json()
    r = client.post("/api/sobjects/Contact", headers=admin,
                    json={"FirstName": "Wel", "LastName": "Come",
                          "Email": "wel.come.trig@example.com"})
    assert r.status_code == 201, r.get_json()
    after = client.get("/api/sobjects/Task", headers=admin).get_json()
    assert len(after) == len(before) + 1
    assert "Wel Come" in after[-1]["Subject"]


def test_trigger_errors_block_save(client, admin):
    r = client.post("/api/admin/triggers", headers=admin, json={
        "name": "Nope", "object": "Lead", "active": True,
        "events": ["before_insert"],
        "code": "errors.append('blocked by test trigger')"})
    assert r.status_code == 201
    r = client.post("/api/sobjects/Lead", headers=admin,
                    json={"LastName": "Blocked", "Company": "X",
                          "Email": "blocked.trig@example.com"})
    assert r.status_code == 422
    assert any("blocked by test trigger" in d for d in r.get_json()["details"])


def test_trigger_syntax_rejected(client, admin):
    r = client.post("/api/admin/triggers", headers=admin, json={
        "name": "Bad", "object": "Lead", "active": True,
        "events": ["before_insert"], "code": "def broken(:\n  pass"})
    assert r.status_code == 422


def test_trigger_non_admin_forbidden(client, leo):
    r = client.post("/api/admin/triggers", headers=leo, json={
        "name": "X", "object": "Lead", "active": True,
        "events": ["before_insert"], "code": "pass"})
    assert r.status_code == 403


def test_trigger_sandbox_blocks_imports(client, admin):
    r = client.post("/api/admin/triggers", headers=admin, json={
        "name": "Evil", "object": "Lead", "active": True,
        "events": ["before_insert"], "code": "import os"})
    assert r.status_code == 201
    r = client.post("/api/sobjects/Lead", headers=admin,
                    json={"LastName": "Evil", "Company": "X",
                          "Email": "evil.trig@example.com"})
    assert r.status_code == 422
    assert any("failed" in d.lower() or "import" in d.lower()
               for d in r.get_json()["details"])


# ------------------------------------------------------------ roll-up summaries
def test_rollup_sum_and_count(client, admin):
    a = client.post("/api/sobjects/Account", headers=admin,
                    json={"Name": "Rollup Co"}).get_json()
    client.post("/api/sobjects/Opportunity", headers=admin,
                json={"Name": "R1", "Stage": "Prospecting", "Amount": 100,
                      "CloseDate": "2027-01-01", "AccountId": a["Id"]})
    client.post("/api/sobjects/Opportunity", headers=admin,
                json={"Name": "R2", "Stage": "Closed Won", "Amount": 400,
                      "CloseDate": "2027-01-01", "AccountId": a["Id"]})
    r = client.get(f"/api/sobjects/Account/{a['Id']}", headers=admin).get_json()
    assert r["TotalPipeline"] == 500
    assert r["OpenOpportunityCount"] == 1  # Closed Won excluded by filter


def test_rollup_not_writable(client, admin):
    a = client.post("/api/sobjects/Account", headers=admin,
                    json={"Name": "Rollup W"}).get_json()
    r = client.patch(f"/api/sobjects/Account/{a['Id']}", headers=admin,
                     json={"TotalPipeline": 999})
    assert r.status_code == 422


def test_rollup_marked_computed_in_describe(client, admin):
    d = client.get("/api/describe/Account", headers=admin).get_json()
    f = next(x for x in d["fields"] if x["name"] == "TotalPipeline")
    assert f["computed"] and not f["editable"]


def test_admin_can_add_rollup_field(client, admin):
    r = client.post("/api/admin/objects/Contact/fields", headers=admin, json={
        "name": "CaseCount", "label": "Cases", "type": "Number",
        "rollup": {"object": "Case", "via": "ContactId", "func": "count"}})
    assert r.status_code == 201, r.get_json()


# ------------------------------------------------------------ list views
def test_list_view_filter_and_sort(client, admin):
    r = client.post("/api/list-views", headers=admin, json={
        "object": "Opportunity", "name": "Big deals",
        "columns": ["Name", "Amount"],
        "filters": {">": [{"field": "Amount"}, 1000]},
        "sort_by": "Amount", "sort_dir": "desc", "shared": True})
    assert r.status_code == 201
    vid = r.get_json()["id"]
    client.post("/api/sobjects/Opportunity", headers=admin,
                json={"Name": "Small", "Stage": "Prospecting", "Amount": 10,
                      "CloseDate": "2027-01-01"})
    client.post("/api/sobjects/Opportunity", headers=admin,
                json={"Name": "Huge", "Stage": "Prospecting", "Amount": 5000,
                      "CloseDate": "2027-01-01"})
    rows = client.get(f"/api/sobjects/Opportunity?view={vid}", headers=admin).get_json()
    names = [x["Name"] for x in rows]
    assert "Huge" in names and "Small" not in names
    amounts = [x["Amount"] for x in rows]
    assert amounts == sorted(amounts, reverse=True)


def test_list_view_user_id_filter(client, admin, leo):
    leo_id = next(u["id"] for u in client.get("/api/admin/users", headers=admin).get_json()
                  if u["username"] == "leo")
    views = client.get("/api/list-views/Opportunity", headers=leo).get_json()
    view = next(v for v in views if v["name"] == "My Open Pipeline")
    client.post("/api/sobjects/Opportunity", headers=leo,
                json={"Name": "Leos deal", "Stage": "Prospecting", "Amount": 50,
                      "CloseDate": "2027-01-01"})
    rows = client.get(f"/api/sobjects/Opportunity?view={view['id']}", headers=leo).get_json()
    assert "Leos deal" in {x["Name"] for x in rows}  # plus leo's two seeded opps
    assert all(x["OwnerId"] == leo_id for x in rows)
    # the same shared view resolves {"user_id": true} per viewer: admin owns no opps
    rows_admin = client.get(f"/api/sobjects/Opportunity?view={view['id']}",
                            headers=admin).get_json()
    assert rows_admin == []


def test_list_view_personal_visibility(client, leo, ana):
    r = client.post("/api/list-views", headers=leo, json={
        "object": "Account", "name": "Leos private", "columns": ["Name"]})
    assert r.status_code == 201
    vid = r.get_json()["id"]
    mine = client.get("/api/list-views/Account", headers=leo).get_json()
    assert any(v["id"] == vid for v in mine)
    others = client.get("/api/list-views/Account", headers=ana).get_json()
    assert not any(v["id"] == vid for v in others)
    # ana cannot sneak the view id in either: falls back to unfiltered list
    rows = client.get(f"/api/sobjects/Account?view={vid}", headers=ana).get_json()
    assert isinstance(rows, list)


def test_list_view_shared_requires_admin(client, leo):
    r = client.post("/api/list-views", headers=leo, json={
        "object": "Account", "name": "Shared attempt", "shared": True})
    assert r.status_code == 403


def test_list_search_and_sort_params(client, admin):
    client.post("/api/sobjects/Account", headers=admin, json={"Name": "Zebra Inc"})
    client.post("/api/sobjects/Account", headers=admin, json={"Name": "Alpha Inc"})
    rows = client.get("/api/sobjects/Account?search=zebra", headers=admin).get_json()
    assert [x["Name"] for x in rows] == ["Zebra Inc"]
    rows = client.get("/api/sobjects/Account?sort=Name&dir=desc", headers=admin).get_json()
    names = [x["Name"] for x in rows]
    assert names == sorted(names, reverse=True)
