"""Tests for the L4/L6/L10 critical integrity fixes.

L4  - CSV import insert mode runs the full create pipeline (_do_create):
      triggers, flows, roll-ups, assignment rules, webhooks, approvals,
      emails, divisions fire and AutoNumber fields are assigned.
L6  - Generic deletes cascade Lookup-based detail children (Opportunity,
      Campaign, WorkOrder, PriceBook, Quote) with no orphans; children go
      through the recycle bin; PriceBook delete is blocked while an entry
      is referenced by line items.
L10 - Exchange rates created through the generic admin API (Setup UI shape)
      are stored canonically and used by convert; legacy rows are migrated.
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers import login
from forcelet.api import create_app


@pytest.fixture()
def client():
    import tempfile
    db = tempfile.mktemp(suffix=".db")
    app = create_app(db)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    # Tolerant teardown: a concurrent suite run may clean the shared tmp dir.
    for p in (db, db + "-wal", db + "-shm"):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


@pytest.fixture()
def admin(client):
    return login(client, "admin")


def _rid(resp):
    body = resp.get_json()
    assert resp.status_code in (200, 201), body
    return body.get("Id") or body.get("id")


def _mkobj(c, h, name, fields):
    r = c.post("/api/admin/objects", headers=h,
               json={"name": name, "label": name, "plural": name + "s"})
    assert r.status_code == 201, r.get_json()
    for f in fields:
        r = c.post(f"/api/admin/objects/{name}/fields", headers=h, json=f)
        assert r.status_code == 201, (f, r.get_json())


# ------------------------------------------------------------------ L4
def test_import_insert_runs_pipeline_and_assigns_autonumber(client, admin):
    _mkobj(client, admin, "ImpInv", [
        {"name": "Title", "label": "Title", "type": "Text", "length": 80},
        {"name": "InvNo", "label": "Inv No", "type": "AutoNumber",
         "auto_prefix": "INV-", "auto_start": 1, "auto_width": 4},
    ])
    # record-triggered flow on create that stamps Title
    flow = {"name": "Stamp on import", "object": "ImpInv",
            "trigger": "on_create", "active": True,
            "actions": [{"type": "set_fields", "object": "ImpInv",
                         "fields": {"Title": "flow-stamped"}}]}
    r = client.post("/api/admin/flows", headers=admin, json=flow)
    assert r.status_code in (200, 201), r.get_json()

    csv_data = "Title\nfirst\nsecond\n"
    data = {"file": (io.BytesIO(csv_data.encode("utf-8")), "imp.csv")}
    r = client.post("/api/admin/import/ImpInv?mode=insert", headers=admin,
                    data=data, content_type="multipart/form-data")
    body = r.get_json()
    assert r.status_code == 200, body
    assert body["created"] == 2 and body["failed"] == 0, body

    rows = client.get("/api/sobjects/ImpInv", headers=admin).get_json()
    rows = rows["rows"] if isinstance(rows, dict) else rows
    assert len(rows) == 2
    # AutoNumber assigned (was NULL before the L4 fix)
    numbers = sorted(x["InvNo"] for x in rows)
    assert numbers == ["INV-0001", "INV-0002"], numbers
    # flow fired on each imported row
    assert {x["Title"] for x in rows} == {"flow-stamped"}


def test_import_insert_reports_row_errors(client, admin):
    _mkobj(client, admin, "ImpReq", [
        {"name": "Name", "label": "Name", "type": "Text", "length": 80},
        {"name": "Email", "label": "Email", "type": "Email"},
    ])
    csv_data = "Name,Email\nok-row,ok@example.com\nbad-row,not-an-email\n"
    data = {"file": (io.BytesIO(csv_data.encode("utf-8")), "imp.csv")}
    r = client.post("/api/admin/import/ImpReq?mode=insert", headers=admin,
                    data=data, content_type="multipart/form-data")
    body = r.get_json()
    assert r.status_code == 200, body
    assert body["created"] == 1, body
    assert body["failed"] == 1, body
    assert body["errors"] and body["errors"][0]["row"] == 3


# ------------------------------------------------------------------ L6
def _opp_with_children(c, h):
    acc = _rid(c.post("/api/sobjects/Account", headers=h,
                      json={"Name": "Cascade Acct"}))
    con = _rid(c.post("/api/sobjects/Contact", headers=h,
                      json={"LastName": "Cascade", "AccountId": acc}))
    opp = _rid(c.post("/api/sobjects/Opportunity", headers=h,
                      json={"Name": "Cascade Opp", "AccountId": acc,
                            "Stage": "Prospecting", "CloseDate": "2026-12-01"}))
    oli = _rid(c.post("/api/sobjects/OpportunityLineItem", headers=h,
                      json={"OpportunityId": opp, "Quantity": 2,
                            "UnitPrice": 50}))
    sched = _rid(c.post("/api/sobjects/RevenueSchedule", headers=h,
                        json={"OpportunityLineItemId": oli,
                              "Period": "2026-11-01", "Amount": 100}))
    ocr = _rid(c.post("/api/sobjects/OpportunityContactRole", headers=h,
                      json={"OpportunityId": opp, "ContactId": con,
                            "Role": "Decision Maker"}))
    otm = _rid(c.post("/api/sobjects/OpportunityTeamMember", headers=h,
                      json={"OpportunityId": opp, "UserId": "admin",
                            "TeamRole": "Sales Rep"}))
    split = _rid(c.post("/api/sobjects/OpportunitySplit", headers=h,
                        json={"OpportunityId": opp, "TeamMemberId": otm,
                              "SplitPercentage": 100}))
    return {"opp": opp, "oli": oli, "sched": sched, "ocr": ocr,
            "otm": otm, "split": split}


def test_delete_opportunity_cascades_children_no_orphans(client, admin):
    ids = _opp_with_children(client, admin)
    r = client.delete(f"/api/sobjects/Opportunity/{ids['opp']}", headers=admin)
    body = r.get_json()
    assert r.status_code == 200, body
    cascaded = {o for o, _ in body.get("cascaded", [])}
    assert {"OpportunityLineItem", "OpportunityContactRole",
            "OpportunityTeamMember", "OpportunitySplit",
            "RevenueSchedule"} <= cascaded, body
    # children are gone ...
    for obj, rid in (("OpportunityLineItem", ids["oli"]),
                     ("RevenueSchedule", ids["sched"]),
                     ("OpportunityContactRole", ids["ocr"]),
                     ("OpportunityTeamMember", ids["otm"]),
                     ("OpportunitySplit", ids["split"])):
        r = client.get(f"/api/sobjects/{obj}/{rid}", headers=admin)
        assert r.status_code == 404, (obj, r.get_json())
    # ... and went through the recycle bin
    bin_rows = client.get("/api/recycle-bin", headers=admin).get_json()
    bin_rows = bin_rows["rows"] if isinstance(bin_rows, dict) else bin_rows
    binned = {(b.get("object") or b.get("object_name"), b.get("record_id"))
              for b in bin_rows}
    for obj, rid in (("OpportunityLineItem", ids["oli"]),
                     ("OpportunityContactRole", ids["ocr"])):
        assert (obj, rid) in binned, (obj, binned)


def test_delete_campaign_cascades_members_and_influence(client, admin):
    camp = _rid(client.post("/api/sobjects/Campaign", headers=admin,
                            json={"Name": "Cascade Camp"}))
    con = _rid(client.post("/api/sobjects/Contact", headers=admin,
                           json={"LastName": "CampMember"}))
    cm = _rid(client.post("/api/sobjects/CampaignMember", headers=admin,
                          json={"CampaignId": camp, "ContactId": con}))
    ci = _rid(client.post("/api/sobjects/CampaignInfluence", headers=admin,
                          json={"CampaignId": camp, "ContactId": con,
                                "InfluencePercent": 50}))
    r = client.delete(f"/api/sobjects/Campaign/{camp}", headers=admin)
    assert r.status_code == 200, r.get_json()
    for obj, rid in (("CampaignMember", cm), ("CampaignInfluence", ci)):
        r = client.get(f"/api/sobjects/{obj}/{rid}", headers=admin)
        assert r.status_code == 404, (obj, r.get_json())


def test_delete_pricebook_blocked_while_entry_referenced(client, admin):
    prod = _rid(client.post("/api/sobjects/Product", headers=admin,
                            json={"Name": "Block Widget"}))
    pb = _rid(client.post("/api/sobjects/PriceBook", headers=admin,
                          json={"Name": "Block PB"}))
    entry = _rid(client.post("/api/sobjects/PriceBookEntry", headers=admin,
                             json={"PriceBookId": pb, "ProductId": prod,
                                   "UnitPrice": 10}))
    acc = _rid(client.post("/api/sobjects/Account", headers=admin,
                           json={"Name": "PB Acct"}))
    opp = _rid(client.post("/api/sobjects/Opportunity", headers=admin,
                           json={"Name": "PB Opp", "AccountId": acc,
                                 "Stage": "Prospecting",
                                 "CloseDate": "2026-12-01"}))
    oli = _rid(client.post("/api/sobjects/OpportunityLineItem", headers=admin,
                           json={"OpportunityId": opp, "Quantity": 1,
                                 "UnitPrice": 10, "PriceBookEntryId": entry}))
    # blocked with a clear error while the entry is referenced
    r = client.delete(f"/api/sobjects/PriceBook/{pb}", headers=admin)
    assert r.status_code == 422, r.get_json()
    assert "referenced" in r.get_json()["error"].lower()
    # after removing the reference, the delete cascades the entry
    r = client.delete(f"/api/sobjects/OpportunityLineItem/{oli}", headers=admin)
    assert r.status_code == 200, r.get_json()
    r = client.delete(f"/api/sobjects/PriceBook/{pb}", headers=admin)
    assert r.status_code == 200, r.get_json()
    r = client.get(f"/api/sobjects/PriceBookEntry/{entry}", headers=admin)
    assert r.status_code == 404, r.get_json()


def test_delete_workorder_cascades_service_appointments(client, admin):
    wo = _rid(client.post("/api/sobjects/WorkOrder", headers=admin,
                          json={"Name": "Cascade WO"}))
    sa = _rid(client.post("/api/sobjects/ServiceAppointment", headers=admin,
                          json={"Name": "Cascade SA", "WorkOrderId": wo}))
    r = client.delete(f"/api/sobjects/WorkOrder/{wo}", headers=admin)
    assert r.status_code == 200, r.get_json()
    r = client.get(f"/api/sobjects/ServiceAppointment/{sa}", headers=admin)
    assert r.status_code == 404, r.get_json()


# ------------------------------------------------------------------ L10
def test_admin_created_rate_is_used_by_convert(client, admin):
    r = client.post("/api/admin/exchange-rates", headers=admin, json={
        "name": "EUR test", "from_currency": "EUR", "to_currency": "USD",
        "effective_date": "2026-01-01", "rate": 0.9})
    assert r.status_code == 201, r.get_json()
    row = r.get_json()
    # stored in the canonical shape read by get_rate/convert
    assert row["currency_code"] == "EUR", row
    assert row["start_date"] == "2026-01-01", row
    assert "from_currency" not in row and "effective_date" not in row, row
    r = client.post("/api/currencies/convert", headers=admin, json={
        "amount": 90, "from": "EUR", "to": "USD", "date": "2026-06-01"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["amount"] == pytest.approx(100.0)


def test_admin_rate_rejects_non_corporate_target(client, admin):
    r = client.post("/api/admin/exchange-rates", headers=admin, json={
        "from_currency": "EUR", "to_currency": "GBP",
        "effective_date": "2026-01-01", "rate": 1.2})
    assert r.status_code == 422, r.get_json()
    r = client.post("/api/admin/exchange-rates", headers=admin, json={
        "from_currency": "USD", "to_currency": "USD",
        "effective_date": "2026-01-01", "rate": 1.0})
    assert r.status_code == 422, r.get_json()


def test_legacy_rate_rows_are_migrated(client, admin):
    store = client.application.mf_store
    # re-arm the one-time migration, then plant an old-shape row
    store.config_delete("mf_schema_migrations", "exchange_rates_canonical_v1")
    store.config_put("mf_exchange_rates", {
        "from_currency": "GBP", "to_currency": "USD",
        "effective_date": "2026-02-01", "rate": 0.8, "active": True})
    rows = client.get("/api/currencies/rates?currency=GBP",
                      headers=admin).get_json()
    gbp = [x for x in rows if x.get("currency_code") == "GBP"]
    assert len(gbp) == 1, rows
    assert gbp[0]["start_date"] == "2026-02-01", gbp[0]
    assert "from_currency" not in gbp[0], gbp[0]
    r = client.post("/api/currencies/convert", headers=admin, json={
        "amount": 80, "from": "GBP", "to": "USD", "date": "2026-06-01"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["amount"] == pytest.approx(100.0)
