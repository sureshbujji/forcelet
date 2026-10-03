"""Tests for the sales-domain fixes batch (issues 1-15).

Covers: forecast period filtering + manager adjustments, activity create
permission, campaign influence visibility + recycle, quote PDF line-item
visibility, order/quote totals, order cancel, lead-conversion duplicate
rules, quote sync price-book consistency + manual line preservation,
create-order status gate, OLI price-book-entry active check, attainment
percent contract, forecast UI label, and quote->order UI wiring.
"""
import json
import os

import pytest

from helpers import login
from forcelet import forecasting
from forcelet.api import create_app

FRAG = os.path.join(os.path.dirname(__file__), "..", "metadata",
                    "fragments", "sales_core_objects.json")
WEB_INDEX = os.path.join(os.path.dirname(__file__), "..", "web", "index.html")


def _seed(app):
    with open(FRAG) as f:
        defs = json.load(f)
    reg = app.mf_registry
    for d in defs:
        if reg.get_object(d["name"]):
            fmap = reg.field_map(reg.get_object(d["name"]))
            for fld in d["fields"]:
                if fld["name"] not in fmap:
                    reg.add_field(d["name"], fld)
            continue
        reg.create_object(d["name"], d["label"], d["plural"],
                          is_custom=d.get("is_custom", True))
        for fld in d["fields"]:
            reg.add_field(d["name"], fld)
    try:
        reg.add_field("Account", {"name": "ParentAccountId",
                                  "label": "Parent Account",
                                  "type": "Lookup", "reference_to": "Account"})
    except ValueError:
        pass


@pytest.fixture()
def app(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    app.config["TESTING"] = True
    _seed(app)
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def h(client):
    return login(client)


@pytest.fixture()
def store(app):
    return app.mf_store


@pytest.fixture()
def admin_id(app):
    return app.mf_security.get_user_by_username("admin")["id"]


def _id(resp_json):
    return resp_json["Id"]


def _mk(client, h, obj, fields):
    r = client.post(f"/api/sobjects/{obj}", headers=h, json=fields)
    assert r.status_code in (200, 201), r.get_json()
    return _id(r.get_json())


def _get(client, h, obj, rid):
    r = client.get(f"/api/sobjects/{obj}/{rid}", headers=h)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _opp(client, h, name="Acme Deal", **kw):
    fields = {"Name": name, "Stage": "Prospecting"}
    fields.update(kw)
    return _mk(client, h, "Opportunity", fields)


def _pb_setup(client, h, pb_name="Standard", unit_price=50.0, active=True):
    pb = _mk(client, h, "PriceBook", {"Name": pb_name})
    prod = _mk(client, h, "Product", {"Name": "Widget-" + pb_name,
                                      "IsActive": True})
    pbe = _mk(client, h, "PriceBookEntry",
              {"PriceBookId": pb, "ProductId": prod,
               "UnitPrice": unit_price, "IsActive": active})
    return pb, prod, pbe


def _quote(client, h, opp_id, status="Draft", **kw):
    fields = {"Name": "Q", "OpportunityId": opp_id, "Status": status}
    fields.update(kw)
    return _mk(client, h, "Quote", fields)


def _qli(client, h, quote_id, qty=2, price=100.0, disc=10.0, pbe_id=None):
    body = {"QuoteId": quote_id, "Quantity": qty,
            "UnitPrice": price, "Discount": disc}
    if pbe_id:
        body["PriceBookEntryId"] = pbe_id
    r = client.post("/api/sobjects/QuoteLineItem", headers=h, json=body)
    assert r.status_code in (200, 201), r.get_json()
    return _id(r.get_json())


def _make_user(client, h, username, role, password="UserPass1!",
               profile="Standard User"):
    r = client.post("/api/admin/users", headers=h,
                    json={"username": username, "name": username,
                          "email": f"{username}@example.com",
                          "profile": profile, "password": password,
                          "role": role})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    r = client.post("/api/login",
                    json={"username": username, "password": password})
    assert r.status_code == 200, r.get_json()
    login_body = r.get_json()
    if login_body.get("must_change_password"):
        hh = {"Authorization": "Bearer " + login_body["token"]}
        r2 = client.post("/api/change-password", headers=hh,
                         json={"current": password, "new": "ChangedPass1!"})
        assert r2.status_code == 200, r2.get_json()
        r = client.post("/api/login",
                        json={"username": username,
                              "password": "ChangedPass1!"})
        assert r.status_code == 200, r.get_json()
        login_body = r.get_json()
    return {"Authorization": "Bearer " + login_body["token"]}, body["id"]


# ------------------------------------------------------- 1: period filter
def test_forecast_summary_filters_by_quarter(client, h, admin_id):
    _opp(client, h, "Q4 deal", CloseDate="2026-10-15", Amount=10000,
         ForecastCategory="Commit")
    _opp(client, h, "Q1 deal", CloseDate="2027-01-15", Amount=20000,
         ForecastCategory="Commit")
    r = client.get(f"/api/platform/forecasts/summary?owner_id={admin_id}"
                   "&period=2026-Q4", headers=h)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["open_count"] == 1
    assert body["commit"] == 10000.0
    r = client.get(f"/api/platform/forecasts/summary?owner_id={admin_id}"
                   "&period=2027-Q1", headers=h)
    assert r.get_json()["commit"] == 20000.0


def test_forecast_summary_includes_dateless_opps(client, h, admin_id):
    # Backward compatibility: a dateless opp cannot be attributed to a
    # period, so it counts in every period (and with no period at all).
    _opp(client, h, "Dateless", Amount=7000, ForecastCategory="Pipeline")
    for period in ("2026-Q4", "2026-Q1"):
        r = client.get(f"/api/platform/forecasts/summary?owner_id={admin_id}"
                       f"&period={period}", headers=h)
        assert r.get_json()["pipeline"] == 7000.0
    body = client.get(f"/api/platform/forecasts/summary?owner_id={admin_id}",
                      headers=h).get_json()
    assert body["pipeline"] == 7000.0


def test_forecast_summary_closed_excluded_from_period(client, h, admin_id):
    _opp(client, h, "Won in Q4", CloseDate="2026-11-01", Amount=5000,
         Stage="Closed Won")
    body = client.get(f"/api/platform/forecasts/summary?owner_id={admin_id}"
                      "&period=2026-Q4", headers=h).get_json()
    assert body["open_count"] == 0


# --------------------------------------- 2: activity create permission
def test_activity_post_requires_create_permission(client, h, app):
    # Two users in the same role subtree: the standard user creates/owns
    # the account, the read-only user can see it (same subtree) but has
    # read without create on Account.
    std_h, _std_id = _make_user(client, h, "act_std", "Sales Rep")
    ro_h, _ro_id = _make_user(client, h, "act_ro", "Sales Rep",
                              profile="Read Only")
    acct = _mk(client, std_h, "Account", {"Name": "Acme"})
    # The read-only user can read the timeline...
    r = client.get(f"/api/sobjects/Account/{acct}/activities", headers=ro_h)
    assert r.status_code == 200, r.get_json()
    # ...but cannot post to it with read-only access.
    r = client.post(f"/api/sobjects/Account/{acct}/activities", headers=ro_h,
                    json={"type": "note", "subject": "x", "body": "y"})
    assert r.status_code == 404, r.get_json()
    # Sanity: the owning standard user (create access) can post.
    r = client.post(f"/api/sobjects/Account/{acct}/activities", headers=std_h,
                    json={"type": "note", "subject": "ok", "body": "ok"})
    assert r.status_code == 201, r.get_json()


# --------------------------------- 3: campaign influence visibility
def test_campaign_influence_requires_opp_visibility(client, h, app, store):
    # The endpoint also requires create on CampaignInfluence; grant it to
    # Standard User so the test exercises the visibility gate, not the
    # object-permission gate.
    prof = store.meta_get("mf_profiles", "Standard User")
    prof["object_permissions"]["CampaignInfluence"]["create"] = True
    store.meta_put("mf_profiles", "Standard User", prof)
    mgr_h, mgr_id = _make_user(client, h, "mgr1", "Sales Manager")
    sup_h, _sup_id = _make_user(client, h, "sup1", "Support Agent")
    opp = _opp(client, mgr_h, "Mgr deal")
    camp_id = _mk(client, mgr_h, "Campaign", {"Name": "Camp2"})
    # give the opp a primary campaign via direct update
    client.patch(f"/api/sobjects/Opportunity/{opp}", headers=mgr_h,
                 json={"PrimaryCampaignId": camp_id})
    # Support agent cannot see the manager's opportunity.
    r = client.post("/api/platform/campaign-influence/attribute", headers=sup_h,
                    json={"opportunity_id": opp, "model": "Primary Campaign Source"})
    assert r.status_code == 404, r.get_json()
    r = client.get("/api/platform/campaign-influence/report"
                   f"?opportunity_id={opp}", headers=sup_h)
    assert r.status_code == 404, r.get_json()
    # Manager can attribute and report.
    r = client.post("/api/platform/campaign-influence/attribute", headers=mgr_h,
                    json={"opportunity_id": opp, "model": "Primary Campaign Source"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["attributed"] == 1
    r = client.get("/api/platform/campaign-influence/report"
                   f"?opportunity_id={opp}", headers=mgr_h)
    assert r.status_code == 200, r.get_json()


def test_campaign_influence_idempotent_delete_recycles(client, h, store):
    opp = _opp(client, h, "Rec deal")
    camp = _mk(client, h, "Campaign", {"Name": "RC"})
    client.patch(f"/api/sobjects/Opportunity/{opp}", headers=h,
                 json={"PrimaryCampaignId": camp})
    r = client.post("/api/platform/campaign-influence/attribute", headers=h,
                    json={"opportunity_id": opp})
    assert r.status_code == 200, r.get_json()
    first_ids = {c["id"] for c in
                 store.query("CampaignInfluence", owner_ids=None, limit=100)}
    assert len(first_ids) == 1
    # Second attribution replaces the first via the recycle bin.
    r = client.post("/api/platform/campaign-influence/attribute", headers=h,
                    json={"opportunity_id": opp})
    assert r.status_code == 200, r.get_json()
    r = client.get("/api/recycle-bin", headers=h)
    assert r.status_code == 200, r.get_json()
    recycled = [e for e in r.get_json()
                if e.get("object_name") == "CampaignInfluence"]
    assert {e.get("record_id") for e in recycled} >= first_ids


# ------------------------------------------------- 4: quote PDF visibility
def test_quote_pdf_excludes_invisible_line_items(client, h, app):
    opp = _opp(client, h, "PDF deal")
    qid = _quote(client, h, opp)
    pb, prod, pbe = _pb_setup(client, h, pb_name="PDFBook", unit_price=25.0)
    _qli(client, h, qid, qty=1, price=25.0, disc=0, pbe_id=pbe)
    sec = app.mf_security
    orig = sec.can_see_record

    def deny_qli(user, rec, obj_name=None, _seen=None):
        if obj_name == "QuoteLineItem":
            return False
        return orig(user, rec, obj_name, _seen=_seen)

    r = client.get(f"/api/quotes/{qid}/pdf", headers=h)
    assert r.status_code == 200
    assert b"TOTAL: 25.00" in r.data  # control: visible without patch
    sec.can_see_record = deny_qli
    try:
        r = client.get(f"/api/quotes/{qid}/pdf", headers=h)
        assert r.status_code == 200
        assert b"TOTAL: 25.00" not in r.data
        assert b"TOTAL: 0.00" in r.data
    finally:
        sec.can_see_record = orig


# --------------------------------- 5: stored order + quote totals
def test_quote_grand_total_stored_on_line_writes(client, h, store):
    opp = _opp(client, h, "GT deal")
    qid = _quote(client, h, opp)
    assert store.get("Quote", qid).get("GrandTotal") in (None, 0, 0.0)
    lid = _qli(client, h, qid, qty=2, price=100.0, disc=10.0)  # 180.00
    assert store.get("Quote", qid).get("GrandTotal") == 180.0
    _qli(client, h, qid, qty=1, price=50.0, disc=0)  # +50.00
    assert store.get("Quote", qid).get("GrandTotal") == 230.0
    r = client.patch(f"/api/sobjects/QuoteLineItem/{lid}", headers=h,
                     json={"Quantity": 1})  # 90.00 now
    assert r.status_code == 200, r.get_json()
    assert store.get("Quote", qid).get("GrandTotal") == 140.0
    r = client.delete(f"/api/sobjects/QuoteLineItem/{lid}", headers=h)
    assert r.status_code == 200, r.get_json()
    assert store.get("Quote", qid).get("GrandTotal") == 50.0


def test_create_order_sets_total_and_status_gate(client, h, store):
    opp = _opp(client, h, "OC deal")
    qid = _quote(client, h, opp, status="Draft")
    _qli(client, h, qid, qty=2, price=100.0, disc=10.0)  # unit -> 90.00, line 180.00
    r = client.post(f"/api/sales/quotes/{qid}/create-order", headers=h)
    assert r.status_code == 422, r.get_json()  # Draft is not orderable
    client.patch(f"/api/sobjects/Quote/{qid}", headers=h,
                 json={"Status": "Approved"})
    r = client.post(f"/api/sales/quotes/{qid}/create-order", headers=h)
    assert r.status_code == 201, r.get_json()
    oid = r.get_json()["order"]["Id"]
    assert store.get("Order", oid).get("TotalAmount") == 180.0
    # Rejected quotes are not orderable either.
    q2 = _quote(client, h, opp, status="Rejected")
    _qli(client, h, q2, qty=1, price=10.0, disc=0)
    r = client.post(f"/api/sales/quotes/{q2}/create-order", headers=h)
    assert r.status_code == 422, r.get_json()


def test_order_activation_recomputes_total(client, h, store):
    opp = _opp(client, h, "OA deal")
    qid = _quote(client, h, opp, status="Approved")
    _qli(client, h, qid, qty=2, price=100.0, disc=10.0)
    oid = client.post(f"/api/sales/quotes/{qid}/create-order",
                      headers=h).get_json()["order"]["Id"]
    items = [i for i in store.query("OrderItem", owner_ids=None, limit=100)
             if i.get("OrderId") == oid]
    assert len(items) == 1
    # Change a line item after order creation; activation must recompute.
    r = client.patch(f"/api/sobjects/OrderItem/{items[0]['id']}", headers=h,
                     json={"Quantity": 3})
    assert r.status_code == 200, r.get_json()
    r = client.post(f"/api/sales/orders/{oid}/activate", headers=h)
    assert r.status_code == 200, r.get_json()
    assert store.get("Order", oid).get("TotalAmount") == 270.0  # 3 * 90.00


# ------------------------------------------------- 6: cancel clears date
def test_order_cancel_clears_activated_date(client, h, store):
    opp = _opp(client, h, "CX deal")
    qid = _quote(client, h, opp, status="Approved")
    _qli(client, h, qid, qty=1, price=40.0, disc=0)
    oid = client.post(f"/api/sales/quotes/{qid}/create-order",
                      headers=h).get_json()["order"]["Id"]
    client.post(f"/api/sales/orders/{oid}/activate", headers=h)
    assert store.get("Order", oid).get("ActivatedDate")
    r = client.post(f"/api/sales/orders/{oid}/cancel", headers=h)
    assert r.status_code == 200, r.get_json()
    rec = store.get("Order", oid)
    assert rec.get("Status") == "Cancelled"
    assert rec.get("ActivatedDate") is None


# --------------------------------------- 7: conversion duplicate rules
def _dup_setup(store, obj_name="Account", field="Name"):
    mid = store.insert("MatchingRule",
                       {"Name": "R", "ObjectName": obj_name, "Fields": field,
                        "MatchType": "Exact", "IsActive": True})
    store.insert("DuplicateRule",
                 {"Name": "D", "ObjectName": obj_name, "MatchingRuleId": mid,
                  "Action": "Block", "Message": "dup!", "IsActive": True,
                  "AppliesOn": "Both"})


def test_lead_conversion_blocked_by_duplicate_rule(client, h, store):
    acct_id = _mk(client, h, "Account", {"Name": "DupCo"})
    _dup_setup(store)
    lid = _mk(client, h, "Lead",
              {"LastName": "Doe", "Company": "DupCo",
               "Email": "doe@dupco.example"})
    r = client.post(f"/api/sobjects/Lead/{lid}/convert", headers=h, json={})
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["error"] == "dup!"
    # Compensated: no converted account/contact left behind for this lead.
    dupcos = [a for a in store.query("Account", owner_ids=None, limit=100)
              if a.get("Name") == "DupCo"]
    assert len(dupcos) == 1 and dupcos[0]["id"] == acct_id


def test_lead_conversion_warn_rule_does_not_block(client, h, store):
    _mk(client, h, "Account", {"Name": "WarnCo"})
    mid = store.insert("MatchingRule",
                       {"Name": "R", "ObjectName": "Account", "Fields": "Name",
                        "MatchType": "Exact", "IsActive": True})
    store.insert("DuplicateRule",
                 {"Name": "D", "ObjectName": "Account", "MatchingRuleId": mid,
                  "Action": "Warn", "Message": "possible dup",
                  "IsActive": True, "AppliesOn": "Both"})
    lid = _mk(client, h, "Lead",
              {"LastName": "Roe", "Company": "WarnCo",
               "Email": "roe@warnco.example"})
    r = client.post(f"/api/sobjects/Lead/{lid}/convert", headers=h, json={})
    assert r.status_code == 201, r.get_json()
    assert "possible dup" in (r.get_json().get("warnings") or [])


# --------------------------------------- 8: sync price-book consistency
def test_quote_sync_rejects_mismatched_pricebook(client, h):
    opp = _opp(client, h, "PB deal")
    pb1, _p1, _e1 = _pb_setup(client, h, pb_name="BookA")
    pb2, _p2, _e2 = _pb_setup(client, h, pb_name="BookB")
    qid = _quote(client, h, opp, PriceBookId=pb1)
    client.patch(f"/api/sobjects/Opportunity/{opp}", headers=h,
                 json={"PriceBookId": pb2})
    _qli(client, h, qid, qty=1, price=10.0, disc=0)
    r = client.post(f"/api/sales/quotes/{qid}/sync", headers=h)
    assert r.status_code == 422, r.get_json()
    assert "price book" in r.get_json()["error"].lower()


def test_quote_sync_adopts_pricebook_when_opp_has_none(client, h):
    opp = _opp(client, h, "PB adopt")
    pb1, _p1, _e1 = _pb_setup(client, h, pb_name="BookC")
    qid = _quote(client, h, opp, PriceBookId=pb1)
    _qli(client, h, qid, qty=1, price=10.0, disc=0)
    r = client.post(f"/api/sales/quotes/{qid}/sync", headers=h)
    assert r.status_code == 200, r.get_json()
    assert _get(client, h, "Opportunity", opp)["PriceBookId"] == pb1


# --------------------------------------- 12: OLI active-entry check
def test_oli_direct_create_rejects_inactive_entry(client, h):
    opp = _opp(client, h, "PBE deal")
    _pb, _prod, pbe_active = _pb_setup(client, h, pb_name="Active")
    _pb2, _prod2, pbe_dead = _pb_setup(client, h, pb_name="Dead",
                                       active=False)
    r = client.post("/api/sales/opportunity-line-items", headers=h, json={
        "opportunity_id": opp, "PriceBookEntryId": pbe_dead,
        "Quantity": 1, "UnitPrice": 10.0})
    assert r.status_code == 422, r.get_json()
    assert "not active" in r.get_json()["error"]
    r = client.post("/api/sales/opportunity-line-items", headers=h, json={
        "opportunity_id": opp, "PriceBookEntryId": "no-such-entry",
        "Quantity": 1, "UnitPrice": 10.0})
    assert r.status_code == 422, r.get_json()
    r = client.post("/api/sales/opportunity-line-items", headers=h, json={
        "opportunity_id": opp, "PriceBookEntryId": pbe_active,
        "Quantity": 1, "UnitPrice": 10.0})
    assert r.status_code in (200, 201), r.get_json()


# --------------------------------------- 13: sync preserves manual OLIs
def test_quote_sync_leaves_manual_olis_untouched(client, h, store):
    opp = _opp(client, h, "Manual deal")
    _pb, _prod, pbe = _pb_setup(client, h, pb_name="SyncBook", unit_price=60.0)
    qid = _quote(client, h, opp)
    _qli(client, h, qid, qty=2, price=60.0, disc=0, pbe_id=pbe)
    # Manual line item on the same price-book entry, different quantity.
    manual = _mk(client, h, "OpportunityLineItem",
                 {"OpportunityId": opp, "PriceBookEntryId": pbe,
                  "Quantity": 5, "UnitPrice": 60.0, "Discount": 0})
    r = client.post(f"/api/sales/quotes/{qid}/sync", headers=h)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["created"] == 1 and body["updated"] == 0
    mrec = store.get("OpportunityLineItem", manual)
    assert mrec.get("Quantity") == 5  # untouched
    assert not mrec.get("SourceQuoteId")
    assert not mrec.get("SourceQuoteLineItemId")
    synced = [o for o in store.query("OpportunityLineItem", owner_ids=None,
                                     limit=100)
              if o.get("OpportunityId") == opp
              and o.get("SourceQuoteId") == qid]
    assert len(synced) == 1
    assert synced[0].get("Quantity") == 2
    assert synced[0].get("SourceQuoteLineItemId")
    # Re-sync updates the previously synced row, never the manual one.
    r = client.post(f"/api/sales/quotes/{qid}/sync", headers=h)
    assert r.get_json()["updated"] == 1
    assert r.get_json()["created"] == 0
    assert store.get("OpportunityLineItem", manual).get("Quantity") == 5


# --------------------------------------- 10: manager forecast adjustments
def test_manager_can_adjust_report_forecast(client, h, admin_id):
    mgr_h, mgr_id = _make_user(client, h, "fmgr", "Sales Manager")
    rep_h, rep_id = _make_user(client, h, "frep", "Sales Rep")
    _opp(client, rep_h, "Rep deal", CloseDate="2026-10-10", Amount=40000,
         ForecastCategory="Commit")
    r = client.post("/api/platform/forecasts/adjust", headers=mgr_h, json={
        "owner_id": rep_id, "period": "2026-Q4", "adjusted_amount": 45000,
        "note": "stretch"})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["adjusted_amount"] == 45000.0
    assert body["owner_id"] == rep_id
    summary = client.get(f"/api/platform/forecasts/summary?owner_id={rep_id}"
                         "&period=2026-Q4", headers=mgr_h).get_json()
    assert summary["commit"] == 40000.0  # original preserved
    assert summary["adjusted_amount"] == 45000.0
    assert summary["adjustment"]["note"] == "stretch"


def test_non_manager_cannot_adjust(client, h):
    rep_h, rep_id = _make_user(client, h, "nrep", "Sales Rep")
    r = client.post("/api/platform/forecasts/adjust", headers=rep_h, json={
        "owner_id": rep_id, "period": "2026-Q4", "adjusted_amount": 1000})
    assert r.status_code == 403, r.get_json()


def test_manager_cannot_adjust_outside_subtree(client, h):
    mgr_h, _mgr_id = _make_user(client, h, "omgr", "Sales Manager")
    sup_h, sup_id = _make_user(client, h, "osup", "Support Agent")
    r = client.post("/api/platform/forecasts/adjust", headers=mgr_h, json={
        "owner_id": sup_id, "period": "2026-Q4", "adjusted_amount": 1000})
    assert r.status_code == 403, r.get_json()
    # Admins can adjust anyone.
    r = client.post("/api/platform/forecasts/adjust", headers=h, json={
        "owner_id": sup_id, "period": "2026-Q4", "adjusted_amount": 1000})
    assert r.status_code == 200, r.get_json()


def test_adjust_validates_input(client, h, admin_id):
    r = client.post("/api/platform/forecasts/adjust", headers=h, json={
        "owner_id": admin_id, "period": "2026-10", "adjusted_amount": 100})
    assert r.status_code == 422, r.get_json()  # monthly not allowed here
    r = client.post("/api/platform/forecasts/adjust", headers=h, json={
        "owner_id": admin_id, "period": "2026-Q4",
        "adjusted_amount": "not-a-number"})
    assert r.status_code == 422, r.get_json()
    r = client.post("/api/platform/forecasts/adjust", headers=h, json={
        "owner_id": admin_id, "period": "2026-Q4", "adjusted_amount": -5})
    assert r.status_code == 422, r.get_json()


# --------------------------------------- 14: attainment percent contract
def test_monthly_forecast_attainment_is_percent(client, h, admin_id):
    period = "2026-10"
    _opp(client, h, "Won", CloseDate="2026-10-05", Amount=10000,
         Stage="Closed Won", Probability=100)
    _opp(client, h, "Pipe", CloseDate="2026-10-20", Amount=20000,
         Stage="Proposal", Probability=50)
    r = client.post("/api/admin/forecast-quotas", headers=h, json={
        "user_id": admin_id, "period": period, "quota": 40000})
    assert r.status_code == 201, r.get_json()
    r = client.get(f"/api/forecasts?period={period}", headers=h)
    assert r.status_code == 200, r.get_json()
    row = next(x for x in r.get_json()["rows"]
               if x["user_id"] == admin_id)
    # closed 10000 + weighted 20000*50% = 20000; 20000/40000 = 50%
    assert row["attainment"] == 50.0
    assert "lost_values" in r.get_json()["forecast_type"]


# --------------------------------------- 11 + 15: UI wiring and labels
def test_ui_has_quote_order_actions():
    html = open(WEB_INDEX).read()
    assert "onclick=\"createOrder(" in html
    assert "onclick=\"activateOrder(" in html
    assert "onclick=\"cancelOrder(" in html
    assert "async function createOrder(qid)" in html
    assert "async function activateOrder(oid)" in html
    assert "async function cancelOrder(oid)" in html
    assert "/create-order" in html and "/activate" in html and "/cancel" in html


def test_forecast_label_names_lost_stage_settings():
    html = open(WEB_INDEX).read()
    assert "Lost deals are excluded per the forecast type's lost-stage settings" \
        in html
    assert "Lost deals are excluded.</p>" not in html
    # Attainment is no longer multiplied by 100 in the UI.
    assert "(x.attainment*100)" not in html
