"""Tests for the Reports & Dashboards gap batch:
R1 report CRUD + clone, R2 CSV/XLSX export + pagination, R3 multi-level
grouping with subtotals, R4 parent-lookup columns (incl. null parent),
R5 relative-date filters, R6 report folders + visibility, R7 cross filters,
R8 summary formulas, R13 subscription attachments/conditions/recipients,
D1/D8 combined dashboard run, D2 dashboard filters, D3 drill-down,
D4 per-widget grouping override, D5 dashboard run_as, D6 dashboard folders,
D9 dashboard HTML snapshot digest."""
import base64
import io
import json
import zipfile

import pytest

from helpers import login
from forcelet.api import create_app
from forcelet import automation


@pytest.fixture()
def app(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def _h(client):
    return login(client)


def _rid(resp):
    body = resp.get_json()
    assert resp.status_code in (200, 201), body
    return body.get("Id") or body.get("id")


def _mk_account(c, h, name):
    return _rid(c.post("/api/sobjects/Account", headers=h, json={"Name": name}))


def _mk_opp(c, h, name, acct, stage, amount, close="2026-11-15"):
    return _rid(c.post("/api/sobjects/Opportunity", headers=h,
                       json={"Name": name, "AccountId": acct, "Stage": stage,
                             "Amount": amount, "CloseDate": close}))


@pytest.fixture()
def opps(client):
    """3 opps across 2 accounts / 2 stages for grouping tests."""
    c, h = client, _h(client)
    a1, a2 = _mk_account(c, h, "RD-A1"), _mk_account(c, h, "RD-A2")
    _mk_opp(c, h, "RDX-O1", a1, "Qualification", 100)
    _mk_opp(c, h, "RDX-O2", a1, "Qualification", 200)
    _mk_opp(c, h, "RDX-O3", a2, "Negotiation", 400)
    return h


RDX_FILTER = {"starts_with": [{"field": "Name"}, "RDX-"]}


def _mk_report(c, h, scope_rdx=False, **kw):
    body = {"name": "RD report", "object": "Opportunity",
            "columns": ["Name", "Stage", "Amount"], "active": True}
    if scope_rdx:
        body["filters"] = RDX_FILTER
    body.update(kw)
    if scope_rdx and "filters" in kw and kw["filters"] is not RDX_FILTER:
        body["filters"] = {"and": [RDX_FILTER, kw["filters"]]}
    r = c.post("/api/admin/reports", headers=h, json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


# ---------------- R1: CRUD + clone ----------------

def test_r1_report_crud_and_clone(client):
    c, h = client, _h(client)
    rid = _mk_report(c, h)
    r = c.put(f"/api/admin/reports/{rid}", headers=h,
              json={"name": "RD report v2"})
    assert r.status_code == 200 and r.get_json()["name"] == "RD report v2"
    r = c.post(f"/api/admin/reports/{rid}/clone", headers=h,
               json={"name": "RD copy"})
    assert r.status_code == 201, r.get_json()
    copy_id = r.get_json()["id"]
    assert copy_id != rid
    assert c.get("/api/reports", headers=h).get_json()
    r = c.delete(f"/api/admin/reports/{copy_id}", headers=h)
    assert r.status_code == 200
    ids = [x["id"] for x in c.get("/api/reports", headers=h).get_json()]
    assert copy_id not in ids and rid in ids


# ---------------- R2: export + pagination ----------------

def test_r2_export_csv_and_xlsx(client, opps):
    c, h = client, opps
    rid = _mk_report(c, h, scope_rdx=True)
    r = c.get(f"/api/reports/{rid}/export?format=csv", headers=h)
    assert r.status_code == 200
    text = r.data.decode("utf-8")
    assert "Name" in text.splitlines()[0] and "RDX-O1" in text
    r = c.get(f"/api/reports/{rid}/export?format=xlsx", headers=h)
    assert r.status_code == 200
    assert r.data[:2] == b"PK"
    zf = zipfile.ZipFile(io.BytesIO(r.data))
    sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "RDX-O1" in sheet and "Amount" in sheet
    assert "<row" in sheet
    r = c.get(f"/api/reports/{rid}/export?format=pdf", headers=h)
    assert r.status_code == 422


def test_r2_pagination(client):
    c, h = client, _h(client)
    for i in range(5):
        _mk_account(c, h, f"PG-{i}")
    rid = _mk_report(c, h, object="Account", columns=["Name"],
                     filters={"contains": [{"field": "Name"}, "PG-"]})
    r = c.get(f"/api/reports/{rid}/run?page=2&page_size=2", headers=h)
    d = r.get_json()
    assert d["page"] == 2 and d["pages"] == 3 and len(d["rows"]) == 2
    assert d["row_count"] == 5


# ---------------- R3: multi-level grouping + subtotals ----------------

def test_r3_group_tree_subtotals(client, opps):
    c, h = client, opps
    rid = _mk_report(c, h, scope_rdx=True, group_by=["Stage"],
                     aggregates=[{"func": "sum", "field": "Amount"}])
    d = c.get(f"/api/reports/{rid}/run", headers=h).get_json()
    tree = {n["key"]: n for n in d["group_tree"]}
    assert tree["Qualification"]["aggregates"]["sum_Amount"] == 300
    assert tree["Negotiation"]["aggregates"]["sum_Amount"] == 400
    assert tree["Qualification"]["count"] == 2
    assert d["grand_total"]["aggregates"]["sum_Amount"] == 700
    # two levels: nested children
    rid2 = _mk_report(c, h, scope_rdx=True, name="RD2", group_by=["Stage", "Name"],
                      aggregates=[{"func": "sum", "field": "Amount"}])
    d2 = c.get(f"/api/reports/{rid2}/run", headers=h).get_json()
    qual = [n for n in d2["group_tree"] if n["key"] == "Qualification"][0]
    assert len(qual["children"]) == 2
    assert qual["aggregates"]["sum_Amount"] == 300
    kids = {k["key"]: k["aggregates"]["sum_Amount"]
            for k in qual["children"]}
    assert kids == {"RDX-O1": 100, "RDX-O2": 200}


# ---------------- R4: parent lookup column ----------------

def test_r4_parent_lookup_with_null_parent(client):
    c, h = client, _h(client)
    a = _mk_account(c, h, "RD-Parent")
    _rid(c.post("/api/sobjects/Contact", headers=h,
                json={"FirstName": "Has", "LastName": "P1",
                      "AccountId": a}))
    _rid(c.post("/api/sobjects/Contact", headers=h,
                json={"FirstName": "No", "LastName": "P2"}))
    rid = _mk_report(c, h, name="RD contacts", object="Contact",
                     columns=["LastName", "Account.Name"],
                     filters={"or": [{"==": [{"field": "LastName"}, "P1"]},
                                     {"==": [{"field": "LastName"}, "P2"]}]})
    d = c.get(f"/api/reports/{rid}/run", headers=h).get_json()
    assert d["row_count"] == 2
    by_last = {r["LastName"]: r.get("Account.Name") for r in d["rows"]}
    assert by_last["P1"] == "RD-Parent"
    assert by_last["P2"] in (None, "")


# ---------------- R5: relative dates ----------------

def test_r5_relative_date_filters(client):
    c, h = client, _h(client)
    _mk_account(c, h, "RD-Today")
    rid = _mk_report(c, h, name="RD rel", object="Account",
                     columns=["Name"],
                     filters={"and": [{"==": [{"field": "Name"}, "RD-Today"]},
                                      {"is_this_month": [{"field": "CreatedDate"}]}]})
    d = c.get(f"/api/reports/{rid}/run", headers=h).get_json()
    assert d["row_count"] >= 1
    assert any(r["Name"] == "RD-Today" for r in d["rows"])
    rid2 = _mk_report(c, h, name="RD rel2", object="Account",
                      columns=["Name"],
                      filters={"and": [{"==": [{"field": "Name"}, "RD-Today"]},
                                       {"is_last_n_days": [{"field": "CreatedDate"}, 7]}]})
    d2 = c.get(f"/api/reports/{rid2}/run", headers=h).get_json()
    assert d2["row_count"] >= 1
    # negative: a far-future close date is not today
    a = _mk_account(c, h, "RD-Future")
    _mk_opp(c, h, "RDX-OF", a, "Negotiation", 10, close="2028-06-01")
    rid3 = _mk_report(c, h, name="RD rel3", columns=["Name"],
                      filters={"and": [RDX_FILTER,
                                       {"is_today": [{"field": "CloseDate"}]}]})
    d3 = c.get(f"/api/reports/{rid3}/run", headers=h).get_json()
    assert d3["row_count"] == 0


# ---------------- R6: folders ----------------

def test_r6_report_folder_visibility(client):
    c, h = client, _h(client)
    app = c.application
    sec = app.mf_security
    sec.create_user("repuser", "Rep User", "Standard User", None,
                    password="Pass12345!", email="repuser@example.com")
    uh = login(c, username="repuser", password="Pass12345!")
    f = c.post("/api/folders", headers=h,
               json={"name": "Priv", "kind": "report",
                     "visibility": "private"}).get_json()
    rid = _mk_report(c, h, name="RD priv", folder_id=f["id"])
    admin_ids = [x["id"] for x in c.get("/api/reports", headers=h).get_json()]
    user_ids = [x["id"] for x in c.get("/api/reports", headers=uh).get_json()]
    assert rid in admin_ids and rid not in user_ids
    f2 = c.post("/api/folders", headers=h,
                json={"name": "Shared", "kind": "report",
                      "visibility": "shared"}).get_json()
    rid2 = _mk_report(c, h, name="RD shared", folder_id=f2["id"])
    user_ids = [x["id"] for x in c.get("/api/reports", headers=uh).get_json()]
    assert rid2 in user_ids


# ---------------- R7: cross filters ----------------

def test_r7_cross_filters(client):
    c, h = client, _h(client)
    a1, a2 = _mk_account(c, h, "RD-X1"), _mk_account(c, h, "RD-X2")
    _mk_opp(c, h, "OX", a1, "Qualification", 50)
    rid_w = _mk_report(c, h, name="RD with", object="Account",
                       columns=["Name"],
                       cross_filters=[{"mode": "with", "object": "Opportunity",
                                       "via": "AccountId"}])
    dw = c.get(f"/api/reports/{rid_w}/run", headers=h).get_json()
    names_w = [r["Name"] for r in dw["rows"]]
    assert "RD-X1" in names_w and "RD-X2" not in names_w
    rid_wo = _mk_report(c, h, name="RD without", object="Account",
                        columns=["Name"],
                        cross_filters=[{"mode": "without",
                                        "object": "Opportunity",
                                        "via": "AccountId"}])
    dwo = c.get(f"/api/reports/{rid_wo}/run", headers=h).get_json()
    names_wo = [r["Name"] for r in dwo["rows"]]
    assert "RD-X2" in names_wo and "RD-X1" not in names_wo


# ---------------- R8: summary formulas ----------------

def test_r8_summary_formulas(client, opps):
    c, h = client, opps
    rid = _mk_report(c, h, scope_rdx=True, group_by=["Stage"],
                     aggregates=[{"func": "sum", "field": "Amount"}],
                     summary_formulas=[{"name": "double", "label": "Double",
                                        "formula": {"*": [{"agg": "sum_Amount"},
                                                          2]}}])
    d = c.get(f"/api/reports/{rid}/run", headers=h).get_json()
    tree = {n["key"]: n for n in d["group_tree"]}
    assert tree["Qualification"]["formulas"][0]["value"] == 600
    gt = {f["name"]: f["value"] for f in d["grand_total"]["formulas"]}
    assert gt["double"] == 1400


# ---------------- R10/R11/R12 quick checks ----------------

def test_r10_multi_sort_and_r11_bucket(client, opps):
    c, h = client, opps
    rid = _mk_report(c, h, scope_rdx=True, sort=[{"field": "Stage", "dir": "asc"},
                                                {"field": "Amount", "dir": "desc"}])
    rows = c.get(f"/api/reports/{rid}/run", headers=h).get_json()["rows"]
    quals = [r for r in rows if r["Stage"] == "Qualification"]
    assert [r["Amount"] for r in quals] == [200, 100]
    rid2 = _mk_report(c, h, scope_rdx=True, name="RD bucket",
                      bucket={"field": "Amount",
                              "buckets": [{"name": "S", "from": 0,
                                           "to": 250},
                                          {"name": "L", "from": 251,
                                           "to": None}]},
                      group_by=["_bucket"])
    d2 = c.get(f"/api/reports/{rid2}/run", headers=h).get_json()
    bk = {n["key"]: n["count"] for n in d2["group_tree"]}
    assert bk == {"S": 2, "L": 1}


# ---------------- R13: subscriptions ----------------

def test_r13_subscription_attachment_and_condition(client, opps):
    c, h = client, opps
    app = c.application
    store, sec = app.mf_store, app.mf_security
    me = c.get("/api/me", headers=h).get_json()
    rid = _mk_report(c, h, scope_rdx=True)
    r = c.post("/api/report-subscriptions", headers=h, json={
        "report_id": rid, "frequency": "monthly", "attachment": "csv",
        "condition": {"min_rows": 1},
        "recipients": [{"type": "user", "value": me["id"]},
                       {"type": "email", "value": "boss@example.com"}]})
    assert r.status_code == 201, r.get_json()
    sub = r.get_json()
    assert sub["attachment"] == "csv" and sub["frequency"] == "monthly"
    job = store.config_get("mf_scheduled_jobs", sub["job_id"])
    assert job["interval_minutes"] == 43200
    res = automation.send_report_digest(store, sec, sub["id"])
    assert res["ok"], res
    logged = store.email_log(limit=5)
    assert len(logged) == 1  # seeded admin has no email; boss resolves
    att = json.loads(logged[0]["attachments"])
    assert att and att[0]["filename"].endswith(".csv")
    csv_text = base64.b64decode(att[0]["data"]).decode("utf-8")
    assert "RDX-O1" in csv_text
    # condition not met -> skipped, nothing new logged
    n0 = len(store.email_log(limit=50))
    r2 = c.post("/api/report-subscriptions", headers=h, json={
        "report_id": rid, "frequency": "daily",
        "condition": {"min_rows": 9999},
        "recipients": ["x@example.com"]})
    res2 = automation.send_report_digest(store, sec, r2.get_json()["id"])
    assert res2["ok"] and "skipped" in res2["detail"]
    assert len(store.email_log(limit=50)) == n0
    # validation
    bad = c.post("/api/report-subscriptions", headers=h, json={
        "report_id": rid, "frequency": "hourly",
        "recipients": ["x@example.com"]})
    assert bad.status_code == 422


def test_r13_role_recipient_and_user_directory(client):
    c, h = client, _h(client)
    assert c.get("/api/users/directory", headers=h).status_code == 200
    assert c.get("/api/roles/directory", headers=h).status_code == 200


# ---------------- D1/D8: combined dashboard run ----------------

def test_d8_combined_run_and_d4_override(client, opps):
    c, h = client, opps
    rid = _mk_report(c, h, scope_rdx=True,
                     aggregates=[{"func": "sum", "field": "Amount"}])
    r = c.post("/api/dashboards", headers=h, json={
        "name": "RD dash",
        "widgets": [{"report_id": rid, "type": "bar", "w": 1},
                    {"report_id": rid, "type": "stat", "w": 1,
                     "group_by": "Stage"}]})
    assert r.status_code == 201, r.get_json()
    did = r.get_json()["id"]
    d = c.post(f"/api/dashboards/{did}/run", headers=h, json={}).get_json()
    assert len(d["widgets"]) == 2
    assert d["widgets"][0]["data"]["row_count"] == 3
    # D4: per-widget group_by override applies even though report has none
    groups = d["widgets"][1]["data"]["groups"]
    assert {g["key"] for g in groups} == {"Qualification", "Negotiation"}
    # 20-widget cap
    big = c.post("/api/dashboards", headers=h, json={
        "name": "big", "widgets": [{"report_id": rid}] * 21})
    assert big.status_code == 422


def test_d2_dashboard_filters_narrow(client, opps):
    c, h = client, opps
    rid = _mk_report(c, h, scope_rdx=True)
    r = c.post("/api/dashboards", headers=h, json={
        "name": "RD dash f",
        "widgets": [{"report_id": rid, "type": "bar"}],
        "filters": [{"==": [{"field": "Stage"}, "Negotiation"]}]})
    did = r.get_json()["id"]
    d = c.post(f"/api/dashboards/{did}/run", headers=h, json={}).get_json()
    assert d["widgets"][0]["data"]["row_count"] == 1
    assert len(d["widgets"][0]["data"]["rows"][0]) >= 1


def test_d3_drill_down(client, opps):
    c, h = client, opps
    rid = _mk_report(c, h, scope_rdx=True, group_by=["Stage"])
    d = c.get(f"/api/reports/{rid}/run?drill_field=Stage&drill_value=Negotiation",
              headers=h).get_json()
    assert d["row_count"] == 1
    assert all(r["Stage"] == "Negotiation" for r in d["rows"])


def test_d5_d6_dashboard_run_as_and_folder(client, opps):
    c, h = client, opps
    rid = _mk_report(c, h, scope_rdx=True)
    f = c.post("/api/folders", headers=h,
               json={"name": "DashF", "kind": "dashboard",
                     "visibility": "shared"}).get_json()
    r = c.post("/api/dashboards", headers=h, json={
        "name": "RD dash ras", "folder_id": f["id"], "run_as": "viewer",
        "widgets": [{"report_id": rid, "type": "bar"}]})
    assert r.status_code == 201, r.get_json()
    did = r.get_json()["id"]
    d = c.post(f"/api/dashboards/{did}/run", headers=h, json={}).get_json()
    assert d["run_as"] == "admin"
    names = [x["name"] for x in c.get("/api/dashboards", headers=h).get_json()]
    assert "RD dash ras" in names


def test_d9_dashboard_html_snapshot_digest(client, opps):
    c, h = client, opps
    app = c.application
    store, sec = app.mf_store, app.mf_security
    rid = _mk_report(c, h, scope_rdx=True)
    did = c.post("/api/dashboards", headers=h, json={
        "name": "RD dash snap",
        "widgets": [{"report_id": rid, "type": "table"}]}).get_json()["id"]
    r = c.post("/api/report-subscriptions", headers=h, json={
        "dashboard_id": did, "frequency": "weekly", "attachment": "html",
        "recipients": ["snap@example.com"]})
    assert r.status_code == 201, r.get_json()
    res = automation.send_report_digest(store, sec, r.get_json()["id"])
    assert res["ok"], res
    att = json.loads(store.email_log(limit=3)[0]["attachments"])
    assert att and att[0]["filename"].endswith(".html")
    html = base64.b64decode(att[0]["data"]).decode("utf-8")
    assert "<html>" in html and "RDX-O1" in html
