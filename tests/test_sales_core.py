"""Tests for sales core: OpportunityLineItem, RevenueSchedule, quote syncing,
account hierarchy, opportunity contact roles, teams and splits."""
import json
import os

import pytest

from helpers import login
from forcelet.api import create_app
from forcelet.api import sales_core

FRAG = os.path.join(os.path.dirname(__file__), "..", "metadata",
                    "fragments", "sales_core_objects.json")


def _seed(app):
    with open(FRAG) as f:
        defs = json.load(f)
    reg = app.mf_registry
    for d in defs:
        if reg.get_object(d["name"]):
            # Already provisioned by the merged standard metadata; ensure any
            # fields the fragment defines are present, then continue.
            fmap = reg.field_map(reg.get_object(d["name"]))
            for fld in d["fields"]:
                if fld["name"] not in fmap:
                    reg.add_field(d["name"], fld)
            continue
        reg.create_object(d["name"], d["label"], d["plural"],
                          is_custom=d.get("is_custom", True))
        for fld in d["fields"]:
            reg.add_field(d["name"], fld)
    # ParentAccountId is a merge-time addition to Account; add at runtime here.
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
def admin_id(app):
    return app.mf_security.get_user_by_username("admin")["id"]


def _id(resp_json):
    return resp_json["Id"]


def _mk(client, h, obj, fields):
    r = client.post(f"/api/sobjects/{obj}", headers=h, json=fields)
    assert r.status_code in (200, 201), r.get_json()
    return _id(r.get_json())


def _opp(client, h, name="Acme Deal"):
    return _mk(client, h, "Opportunity", {"Name": name, "Stage": "Prospecting"})


def _pb_setup(client, h):
    pb = _mk(client, h, "PriceBook", {"Name": "Standard"})
    prod = _mk(client, h, "Product", {"Name": "Widget", "IsActive": True})
    pbe = _mk(client, h, "PriceBookEntry",
              {"PriceBookId": pb, "ProductId": prod,
               "UnitPrice": 50.0, "IsActive": True})
    return pb, prod, pbe


def _oli(client, h, opp_id, qty=2, price=100.0, disc=10.0):
    r = client.post("/api/sales/opportunity-line-items", headers=h, json={
        "opportunity_id": opp_id, "Quantity": qty,
        "UnitPrice": price, "Discount": disc})
    assert r.status_code in (200, 201), r.get_json()
    return _id(r.get_json())


def _amount(client, h, opp_id):
    r = client.get(f"/api/sobjects/Opportunity/{opp_id}", headers=h)
    assert r.status_code == 200, r.get_json()
    return r.get_json()["Amount"]


# ------------------------------------------------------------ objects
def test_objects_registered(app):
    names = {o["name"] for o in app.mf_registry.list_objects()}
    for n in ["OpportunityLineItem", "RevenueSchedule", "QuoteSync",
              "OpportunityContactRole", "OpportunityTeamMember",
              "OpportunitySplit"]:
        assert n in names, n
    fields = {f["name"] for f in
              app.mf_registry.get_object("OpportunityLineItem")["fields"]}
    assert {"OpportunityId", "PriceBookEntryId", "ProductId", "Quantity",
            "UnitPrice", "Discount", "TotalPrice", "ServiceDate",
            "Description", "SourceQuoteId"} <= fields
    acct_fields = {f["name"] for f in
                   app.mf_registry.get_object("Account")["fields"]}
    assert "ParentAccountId" in acct_fields


# ------------------------------------------------------------ line items
def test_oli_create_computes_total_and_rolls_up(client, h):
    opp = _opp(client, h)
    lid = _oli(client, h, opp, qty=2, price=100.0, disc=10.0)
    r = client.get(f"/api/sales/opportunity-line-items/{lid}", headers=h)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["TotalPrice"] == 180.0
    assert _amount(client, h, opp) == 180.0


def test_oli_create_validation(client, h):
    opp = _opp(client, h)
    r = client.post("/api/sales/opportunity-line-items", headers=h,
                    json={"Quantity": 1, "UnitPrice": 10.0})
    assert r.status_code == 404  # missing opportunity
    r = client.post("/api/sales/opportunity-line-items", headers=h, json={
        "opportunity_id": opp, "Quantity": 1, "UnitPrice": 10.0,
        "Discount": 150.0})
    assert r.status_code == 422
    r = client.post("/api/sales/opportunity-line-items", headers=h, json={
        "opportunity_id": opp, "Quantity": -1, "UnitPrice": 10.0})
    assert r.status_code == 422


def test_oli_update_recomputes(client, h):
    opp = _opp(client, h)
    lid = _oli(client, h, opp, qty=2, price=100.0, disc=10.0)
    r = client.put(f"/api/sales/opportunity-line-items/{lid}", headers=h,
                   json={"Quantity": 3})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["TotalPrice"] == 270.0
    assert _amount(client, h, opp) == 270.0


def test_oli_delete_recomputes_and_cascades_schedules(client, h):
    opp = _opp(client, h)
    lid1 = _oli(client, h, opp, qty=1, price=100.0, disc=0.0)
    _oli(client, h, opp, qty=1, price=50.0, disc=0.0)
    assert _amount(client, h, opp) == 150.0
    r = client.post("/api/sales/revenue-schedules/generate", headers=h, json={
        "line_item_id": lid1, "installments": 2, "start_date": "2026-10-01"})
    assert r.status_code == 201, r.get_json()
    r = client.delete(f"/api/sales/opportunity-line-items/{lid1}", headers=h)
    assert r.status_code == 200, r.get_json()
    assert _amount(client, h, opp) == 50.0
    r = client.get(f"/api/sales/revenue-schedules?line_item_id={lid1}",
                   headers=h)
    assert r.get_json() == []


def test_oli_list_filters_by_opportunity(client, h):
    o1, o2 = _opp(client, h, "Deal 1"), _opp(client, h, "Deal 2")
    _oli(client, h, o1)
    _oli(client, h, o2)
    r = client.get(f"/api/sales/opportunity-line-items?opportunity_id={o1}",
                   headers=h)
    assert r.status_code == 200
    assert len(r.get_json()) == 1


def test_oli_from_pricebook(client, h):
    opp = _opp(client, h)
    pb, prod, pbe = _pb_setup(client, h)
    dead = _mk(client, h, "PriceBookEntry",
               {"PriceBookId": pb, "ProductId": prod,
                "UnitPrice": 10.0, "IsActive": False})
    # inactive entry rejected
    r = client.post("/api/sales/opportunity-line-items/from-pricebook",
                    headers=h, json={
                        "opportunity_id": opp, "pricebook_id": pb,
                        "items": [{"pricebookentry_id": dead, "quantity": 1}]})
    assert r.status_code == 422
    # wrong price book rejected
    other_pb = _mk(client, h, "PriceBook", {"Name": "Other"})
    r = client.post("/api/sales/opportunity-line-items/from-pricebook",
                    headers=h, json={
                        "opportunity_id": opp, "pricebook_id": other_pb,
                        "items": [{"pricebookentry_id": pbe, "quantity": 2}]})
    assert r.status_code == 422
    r = client.post("/api/sales/opportunity-line-items/from-pricebook",
                    headers=h, json={
                        "opportunity_id": opp, "pricebook_id": pb,
                        "items": [{"pricebookentry_id": pbe, "quantity": 2,
                                   "discount": 20.0}]})
    assert r.status_code == 201, r.get_json()
    assert len(r.get_json()["created"]) == 1
    assert _amount(client, h, opp) == 80.0  # 2 * 50 * 0.8
    r = client.get(f"/api/sales/opportunity-line-items?opportunity_id={opp}",
                   headers=h)
    row = r.get_json()[0]
    assert row["ProductId"] == prod
    assert row["PriceBookEntryId"] == pbe


# ------------------------------------------------------------ schedules
def test_schedule_generate_even_split(client, h):
    opp = _opp(client, h)
    lid = _oli(client, h, opp, qty=3, price=100.0, disc=0.0)  # total 300
    r = client.post("/api/sales/revenue-schedules/generate", headers=h, json={
        "line_item_id": lid, "installments": 3, "start_date": "2026-10-01"})
    assert r.status_code == 201, r.get_json()
    scheds = r.get_json()["schedules"]
    assert [s["Amount"] for s in scheds] == [100.0, 100.0, 100.0]
    assert [s["Period"] for s in scheds] == ["2026-10-01", "2026-11-01",
                                            "2026-12-01"]


def test_schedule_generate_rounding(client, h):
    opp = _opp(client, h)
    lid = _oli(client, h, opp, qty=1, price=100.0, disc=0.0)
    r = client.post("/api/sales/revenue-schedules/generate", headers=h, json={
        "line_item_id": lid, "installments": 3, "start_date": "2026-10-01"})
    assert r.status_code == 201, r.get_json()
    amounts = [s["Amount"] for s in r.get_json()["schedules"]]
    assert amounts == [33.33, 33.33, 33.34]
    assert round(sum(amounts), 2) == 100.0


def test_schedule_generate_validation(client, h):
    r = client.post("/api/sales/revenue-schedules/generate", headers=h, json={
        "line_item_id": "nope", "installments": 2, "start_date": "2026-10-01"})
    assert r.status_code == 404
    opp = _opp(client, h)
    lid = _oli(client, h, opp)
    r = client.post("/api/sales/revenue-schedules/generate", headers=h, json={
        "line_item_id": lid, "installments": 0, "start_date": "2026-10-01"})
    assert r.status_code == 422
    r = client.post("/api/sales/revenue-schedules/generate", headers=h, json={
        "line_item_id": lid, "installments": 2, "start_date": "not-a-date"})
    assert r.status_code == 422


def test_schedules_included_in_line_item(client, h):
    opp = _opp(client, h)
    lid = _oli(client, h, opp, qty=1, price=200.0, disc=0.0)
    client.post("/api/sales/revenue-schedules/generate", headers=h, json={
        "line_item_id": lid, "installments": 2, "start_date": "2026-10-01"})
    r = client.get(f"/api/sales/opportunity-line-items/{lid}", headers=h)
    assert r.status_code == 200
    assert len(r.get_json()["schedules"]) == 2


# ------------------------------------------------------------ quote sync
def _quote_setup(client, h):
    opp = _opp(client, h)
    pb, _prod, pbe = _pb_setup(client, h)
    q = _mk(client, h, "Quote", {"Name": "Q1", "OpportunityId": opp,
                                 "PriceBookId": pb})
    q1 = _mk(client, h, "QuoteLineItem",
             {"QuoteId": q, "PriceBookEntryId": pbe,
              "Quantity": 2, "UnitPrice": 50.0})
    q2 = _mk(client, h, "QuoteLineItem",
             {"QuoteId": q, "Quantity": 1, "UnitPrice": 25.0})
    return opp, q, q1, q2


def test_quote_sync_copies_lines(client, h):
    opp, q, _q1, _q2 = _quote_setup(client, h)
    r = client.post(f"/api/sales/quotes/{q}/sync", headers=h)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["synced"] is True
    assert body["created"] == 2 and body["updated"] == 0
    r = client.get(f"/api/sales/opportunity-line-items?opportunity_id={opp}",
                   headers=h)
    olis = r.get_json()
    assert len(olis) == 2
    assert all(o["SourceQuoteId"] == q for o in olis)
    assert sorted(o["TotalPrice"] for o in olis) == [25.0, 100.0]
    assert _amount(client, h, opp) == 125.0
    r = client.get(f"/api/sales/quotes/{q}/sync-status", headers=h)
    assert r.get_json()["is_syncing"] is True


def test_quote_sync_idempotent(client, h):
    _opp, q, _q1, _q2 = _quote_setup(client, h)
    client.post(f"/api/sales/quotes/{q}/sync", headers=h)
    r = client.post(f"/api/sales/quotes/{q}/sync", headers=h)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["created"] == 0
    assert r.get_json()["updated"] == 2


def test_quote_sync_updates_and_deletes_to_match(client, h):
    opp, q, q1, q2 = _quote_setup(client, h)
    client.post(f"/api/sales/quotes/{q}/sync", headers=h)
    # change one line, drop the other
    r = client.patch(f"/api/sobjects/QuoteLineItem/{q1}", headers=h,
                     json={"Quantity": 4})
    assert r.status_code == 200, r.get_json()
    r = client.delete(f"/api/sobjects/QuoteLineItem/{q2}", headers=h)
    assert r.status_code == 200, r.get_json()
    r = client.post(f"/api/sales/quotes/{q}/sync", headers=h)
    body = r.get_json()
    assert body["updated"] == 1 and body["deleted"] == 1
    assert body["created"] == 0
    r = client.get(f"/api/sales/opportunity-line-items?opportunity_id={opp}",
                   headers=h)
    olis = r.get_json()
    assert len(olis) == 1
    assert olis[0]["TotalPrice"] == 200.0
    assert _amount(client, h, opp) == 200.0


def test_quote_sync_guard_one_per_opportunity(client, h):
    _opp, q1, _a, _b = _quote_setup(client, h)
    client.post(f"/api/sales/quotes/{q1}/sync", headers=h)
    q2 = _mk(client, h, "Quote",
             {"Name": "Q2", "OpportunityId": _opp})
    r = client.post(f"/api/sales/quotes/{q2}/sync", headers=h)
    assert r.status_code == 422


def test_quote_unsync_clears_flags_keeps_lines(client, h):
    opp, q, _q1, _q2 = _quote_setup(client, h)
    client.post(f"/api/sales/quotes/{q}/sync", headers=h)
    r = client.post(f"/api/sales/quotes/{q}/unsync", headers=h)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["synced"] is False
    r = client.get(f"/api/sales/quotes/{q}/sync-status", headers=h)
    assert r.get_json()["is_syncing"] is False
    r = client.get(f"/api/sales/opportunity-line-items?opportunity_id={opp}",
                   headers=h)
    olis = r.get_json()
    assert len(olis) == 2  # line items remain
    assert all(not o.get("SourceQuoteId") for o in olis)


def test_quote_sync_requires_opportunity(client, h):
    q = _mk(client, h, "Quote", {"Name": "Orphan"})
    r = client.post(f"/api/sales/quotes/{q}/sync", headers=h)
    assert r.status_code == 422
    r = client.post("/api/sales/quotes/nope/sync", headers=h)
    assert r.status_code == 404


# ------------------------------------------------------------ hierarchy
def test_account_hierarchy(client, h):
    a = _mk(client, h, "Account", {"Name": "Global"})
    b = _mk(client, h, "Account", {"Name": "Region", "ParentAccountId": a})
    c = _mk(client, h, "Account", {"Name": "Branch", "ParentAccountId": b})
    r = client.get(f"/api/sales/accounts/{b}/hierarchy", headers=h)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert [x["Name"] for x in body["ancestors"]] == ["Global"]
    assert len(body["children"]) == 1
    assert body["children"][0]["Name"] == "Branch"
    assert body["children"][0]["children"] == []
    # root view
    r = client.get(f"/api/sales/accounts/{a}/hierarchy", headers=h)
    assert r.get_json()["ancestors"] == []
    assert r.get_json()["children"][0]["children"][0]["Name"] == "Branch"
    # unknown account
    r = client.get("/api/sales/accounts/nope/hierarchy", headers=h)
    assert r.status_code == 404
    _ = c  # grandchild created above


def test_account_hierarchy_cycle_safe(client, h, app):
    a = _mk(client, h, "Account", {"Name": "A"})
    b = _mk(client, h, "Account", {"Name": "B", "ParentAccountId": a})
    c = _mk(client, h, "Account", {"Name": "C", "ParentAccountId": b})
    # write-time guard (Phase 2): the API must refuse to introduce a cycle
    r = client.patch(f"/api/sobjects/Account/{a}", headers=h,
                     json={"ParentAccountId": c})
    assert r.status_code == 422, r.get_json()
    # read-side guard: a cycle injected below the API (legacy data) must
    # still terminate the hierarchy endpoint
    app.mf_store.update("Account", a, {"ParentAccountId": c})
    r = client.get(f"/api/sales/accounts/{b}/hierarchy", headers=h)
    assert r.status_code == 200, r.get_json()  # terminates


# ------------------------------------------------------------ contact roles
def test_contact_roles_primary_enforced(client, h):
    opp = _opp(client, h)
    c1 = _mk(client, h, "Contact", {"LastName": "Alpha"})
    c2 = _mk(client, h, "Contact", {"LastName": "Beta"})
    r = client.post("/api/sales/opportunity-contact-roles", headers=h, json={
        "opportunity_id": opp, "contact_id": c1,
        "role": "Decision Maker", "is_primary": True})
    assert r.status_code in (200, 201), r.get_json()
    r1 = _id(r.get_json())
    r = client.post("/api/sales/opportunity-contact-roles", headers=h, json={
        "opportunity_id": opp, "contact_id": c2,
        "role": "Champion", "is_primary": True})
    assert r.status_code in (200, 201), r.get_json()
    r2 = _id(r.get_json())
    rows = {x["Id"]: x for x in
            client.get("/api/sales/opportunity-contact-roles"
                       f"?opportunity_id={opp}", headers=h).get_json()}
    assert not rows[r1]["IsPrimary"]
    assert rows[r2]["IsPrimary"]
    # related list embeds contact info, primary first
    r = client.get(f"/api/sales/opportunities/{opp}/contact-roles", headers=h)
    assert r.status_code == 200
    rel = r.get_json()
    assert rel[0]["contact"]["Name"] == "Beta"
    assert rel[0]["IsPrimary"]
    # delete
    r = client.delete(f"/api/sales/opportunity-contact-roles/{r2}", headers=h)
    assert r.status_code == 200


def test_contact_role_validation(client, h):
    opp = _opp(client, h)
    r = client.post("/api/sales/opportunity-contact-roles", headers=h, json={
        "opportunity_id": opp, "contact_id": "nope"})
    assert r.status_code == 404
    r = client.post("/api/sales/opportunity-contact-roles", headers=h, json={
        "opportunity_id": "nope",
        "contact_id": _mk(client, h, "Contact", {"LastName": "X"})})
    assert r.status_code == 404


# ------------------------------------------------------------ team + splits
def _member(client, h, opp, admin_id, role="Sales Rep"):
    r = client.post("/api/sales/opportunity-team-members", headers=h, json={
        "opportunity_id": opp, "user_id": admin_id, "team_role": role,
        "access_level": "Edit"})
    assert r.status_code in (200, 201), r.get_json()
    return _id(r.get_json())


def test_team_member_crud(client, h, admin_id):
    opp = _opp(client, h)
    r = client.post("/api/sales/opportunity-team-members", headers=h, json={
        "opportunity_id": opp, "user_id": "ghost"})
    assert r.status_code == 422
    mid = _member(client, h, opp, admin_id)
    r = client.get("/api/sales/opportunity-team-members"
                   f"?opportunity_id={opp}", headers=h)
    assert len(r.get_json()) == 1
    r = client.put(f"/api/sales/opportunity-team-members/{mid}", headers=h,
                   json={"team_role": "SE"})
    assert r.status_code == 200
    assert r.get_json()["TeamRole"] == "SE"
    r = client.get(f"/api/sales/opportunities/{opp}/team", headers=h)
    assert r.status_code == 200
    assert r.get_json()["members"][0]["username"] == "admin"


def test_split_sums_to_100(client, h, admin_id):
    opp = _opp(client, h)
    m1 = _member(client, h, opp, admin_id)
    m2 = _member(client, h, opp, admin_id, role="SE")

    def add(mid, pct, stype="Revenue"):
        return client.post("/api/sales/opportunity-splits", headers=h, json={
            "opportunity_id": opp, "team_member_id": mid,
            "split_type": stype, "split_percentage": pct})

    assert add(m1, 100).status_code in (200, 201)
    assert add(m2, 40).status_code == 422  # would total 140
    # atomic rebalance to 60/40
    r = client.post(f"/api/sales/opportunities/{opp}/splits/replace",
                    headers=h, json={"splits": [
                        {"team_member_id": m1, "split_type": "Revenue",
                         "split_percentage": 60},
                        {"team_member_id": m2, "split_type": "Revenue",
                         "split_percentage": 40}]})
    assert r.status_code == 201, r.get_json()
    # bad rebalance rejected
    r = client.post(f"/api/sales/opportunities/{opp}/splits/replace",
                    headers=h, json={"splits": [
                        {"team_member_id": m1, "split_type": "Revenue",
                         "split_percentage": 60},
                        {"team_member_id": m2, "split_type": "Revenue",
                         "split_percentage": 30}]})
    assert r.status_code == 422
    splits = {s["TeamMemberId"]: s for s in
              client.get("/api/sales/opportunity-splits"
                         f"?opportunity_id={opp}", headers=h).get_json()}
    # update breaking the sum is rejected
    r = client.put(f"/api/sales/opportunity-splits/{splits[m1]['Id']}",
                   headers=h, json={"split_percentage": 70})
    assert r.status_code == 422
    # deleting one of a pair is rejected (remainder would not total 100)
    r = client.delete(f"/api/sales/opportunity-splits/{splits[m2]['Id']}",
                      headers=h)
    assert r.status_code == 422
    # credit splits are tracked independently
    assert add(m1, 100, "Credit").status_code in (200, 201)
    r = client.get(f"/api/sales/opportunities/{opp}/team", headers=h)
    assert r.get_json()["split_totals"] == {"Revenue": 100.0, "Credit": 100.0}


def test_split_member_must_belong_to_opportunity(client, h, admin_id):
    o1, o2 = _opp(client, h, "D1"), _opp(client, h, "D2")
    m = _member(client, h, o2, admin_id)
    r = client.post("/api/sales/opportunity-splits", headers=h, json={
        "opportunity_id": o1, "team_member_id": m,
        "split_type": "Revenue", "split_percentage": 100})
    assert r.status_code == 422


def test_team_member_delete_blocked_with_splits(client, h, admin_id):
    opp = _opp(client, h)
    m = _member(client, h, opp, admin_id)
    r = client.post("/api/sales/opportunity-splits", headers=h, json={
        "opportunity_id": opp, "team_member_id": m,
        "split_type": "Revenue", "split_percentage": 100})
    assert r.status_code in (200, 201)
    r = client.delete(f"/api/sales/opportunity-team-members/{m}", headers=h)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Integration: generic /api/sobjects writes also keep Opportunity.Amount in sync
# ---------------------------------------------------------------------------

def test_generic_lineitem_create_recomputes_opp_amount(client, h):
    r = client.post("/api/sobjects/Opportunity", headers=h,
                    json={"Name": "Generic Rollup", "Stage": "Prospecting",
                          "CloseDate": "2026-12-31"})
    assert r.status_code == 201, r.get_json()
    oid = r.get_json()["Id"]
    r = client.post("/api/sobjects/OpportunityLineItem", headers=h,
                    json={"OpportunityId": oid, "Quantity": 2,
                          "UnitPrice": 50, "TotalPrice": 100})
    assert r.status_code == 201, r.get_json()
    r = client.get(f"/api/sobjects/Opportunity/{oid}", headers=h)
    assert r.get_json().get("Amount") == 100, r.get_json()


def test_generic_lineitem_delete_recomputes_opp_amount(client, h):
    r = client.post("/api/sobjects/Opportunity", headers=h,
                    json={"Name": "Generic Rollup 2", "Stage": "Prospecting",
                          "CloseDate": "2026-12-31"})
    oid = r.get_json()["Id"]
    r = client.post("/api/sobjects/OpportunityLineItem", headers=h,
                    json={"OpportunityId": oid, "Quantity": 1,
                          "UnitPrice": 25, "TotalPrice": 25})
    lid = r.get_json()["Id"]
    assert client.get(f"/api/sobjects/Opportunity/{oid}",
                      headers=h).get_json().get("Amount") == 25
    r = client.delete(f"/api/sobjects/OpportunityLineItem/{lid}", headers=h)
    assert r.status_code == 200, r.get_json()
    r = client.get(f"/api/sobjects/Opportunity/{oid}", headers=h)
    assert r.get_json().get("Amount") == 0, r.get_json()


def test_standard_user_can_manage_line_items(client, h):
    r = client.post("/api/admin/users", headers=h,
                    json={"username": "stdrep1", "name": "Std Rep",
                          "profile": "Standard User", "password": "RepPass1!"})
    assert r.status_code == 201, r.get_json()
    hs = login(client, username="stdrep1", password="RepPass1!")
    r = client.post("/api/sobjects/Opportunity", headers=hs,
                    json={"Name": "Std Opp", "Stage": "Prospecting",
                          "CloseDate": "2026-12-31"})
    assert r.status_code == 201, r.get_json()
    oid = r.get_json()["Id"]
    r = client.post("/api/sobjects/OpportunityLineItem", headers=hs,
                    json={"OpportunityId": oid, "Quantity": 1,
                          "UnitPrice": 10, "TotalPrice": 10})
    assert r.status_code == 201, r.get_json()
    lid = r.get_json()["Id"]
    r = client.get(f"/api/sobjects/OpportunityLineItem/{lid}", headers=hs)
    assert r.status_code == 200, r.get_json()
