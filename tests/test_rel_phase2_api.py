"""Phase 2 API-layer tests for the relationship-gap fixes.

Covers: indirect (external-ID) lookup writes, polymorphic lookup writes,
hierarchy cycle guard wiring, cross-object formulas (+ save-time validation),
?select= / ?children= relationship queries, per-field delete behaviors
(block/clear) at the API, and recycle-bin restore with children.
"""
import os
import sys
import tempfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers import login
from forcelet.api import create_app


@pytest.fixture()
def tctx():
    db = tempfile.mktemp(suffix=".db")
    app = create_app(db)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield SimpleNamespace(app=app, client=c, store=app.mf_store,
                              registry=app.mf_registry, h=login(c, "admin"))
    for p in (db, db + "-wal", db + "-shm"):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


def _rid(resp):
    body = resp.get_json()
    assert resp.status_code in (200, 201), body
    return body.get("Id") or body.get("id")


def _mkobj(tctx, name):
    r = tctx.client.post("/api/admin/objects", headers=tctx.h,
                         json={"name": name, "label": name, "plural": name + "s"})
    assert r.status_code == 201, r.get_json()


def _addfield(tctx, obj, field):
    r = tctx.client.post(f"/api/admin/objects/{obj}/fields", headers=tctx.h,
                         json=field)
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def _acct(tctx, name):
    return _rid(tctx.client.post("/api/sobjects/Account", headers=tctx.h,
                                 json={"Name": name}))


def _contact(tctx, **kw):
    return _rid(tctx.client.post("/api/sobjects/Contact", headers=tctx.h,
                                 json={"LastName": "Smith", **kw}))


# ------------------------------------------------------- indirect lookups
def test_indirect_lookup_write_resolves(tctx):
    """A {"ExternalIdField": value} dict on a Lookup resolves to the Id."""
    aid = _acct(tctx, "Acme-Indirect")
    cid = _contact(tctx, AccountId={"Name": "Acme-Indirect"})
    got = tctx.client.get(f"/api/sobjects/Contact/{cid}", headers=tctx.h).get_json()
    assert got["AccountId"] == aid


def test_indirect_lookup_no_match_422(tctx):
    r = tctx.client.post("/api/sobjects/Contact", headers=tctx.h,
                         json={"LastName": "Nope", "AccountId": {"Name": "ZZZ-No-Such"}})
    assert r.status_code == 422, r.get_json()


def test_indirect_lookup_multi_match_422(tctx):
    _acct(tctx, "DupName")
    _acct(tctx, "DupName")
    r = tctx.client.post("/api/sobjects/Contact", headers=tctx.h,
                         json={"LastName": "Dup", "AccountId": {"Name": "DupName"}})
    assert r.status_code == 422, r.get_json()


def test_indirect_lookup_bad_field_422(tctx):
    r = tctx.client.post("/api/sobjects/Contact", headers=tctx.h,
                         json={"LastName": "Bad", "AccountId": {"NoSuchField": "x"}})
    assert r.status_code == 422, r.get_json()


def test_indirect_lookup_on_update(tctx):
    aid = _acct(tctx, "Acme-Upd")
    cid = _contact(tctx)
    r = tctx.client.patch(f"/api/sobjects/Contact/{cid}", headers=tctx.h,
                          json={"AccountId": {"Name": "Acme-Upd"}})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["AccountId"] == aid


# ------------------------------------------------------- polymorphic writes
@pytest.fixture()
def poly_obj(tctx):
    _mkobj(tctx, "PolyT")
    _addfield(tctx, "PolyT", {"name": "WhoRef", "label": "Who",
                              "type": "PolymorphicLookup",
                              "reference_to": ["Contact", "Lead"]})
    return "PolyT"


def test_polymorphic_write_valid_target(tctx, poly_obj):
    cid = _contact(tctx)
    rid = _rid(tctx.client.post("/api/sobjects/PolyT", headers=tctx.h,
                                json={"WhoRef": cid}))
    got = tctx.client.get(f"/api/sobjects/PolyT/{rid}", headers=tctx.h).get_json()
    assert got["WhoRef"] == cid


def test_polymorphic_write_wrong_target_422(tctx, poly_obj):
    aid = _acct(tctx, "Acme-Poly")
    r = tctx.client.post("/api/sobjects/PolyT", headers=tctx.h,
                         json={"WhoRef": aid})
    assert r.status_code == 422, r.get_json()


def test_polymorphic_write_missing_id_422(tctx, poly_obj):
    r = tctx.client.post("/api/sobjects/PolyT", headers=tctx.h,
                         json={"WhoRef": "deadbeef-dead-beef-dead-beefdeadbeef"})
    assert r.status_code == 422, r.get_json()


def test_polymorphic_bad_definition_rejected(tctx):
    _mkobj(tctx, "PolyBad")
    r = tctx.client.post("/api/admin/objects/PolyBad/fields", headers=tctx.h,
                         json={"name": "Bad", "label": "Bad",
                               "type": "PolymorphicLookup",
                               "reference_to": "Contact"})
    assert r.status_code == 422, r.get_json()


# ------------------------------------------------------- hierarchy guard
def test_hierarchy_cycle_update_422(tctx):
    a = _acct(tctx, "Hier-A")
    b = _rid(tctx.client.post("/api/sobjects/Account", headers=tctx.h,
                              json={"Name": "Hier-B", "ParentAccountId": a}))
    r = tctx.client.patch(f"/api/sobjects/Account/{a}", headers=tctx.h,
                          json={"ParentAccountId": b})
    assert r.status_code == 422, r.get_json()
    assert "circular" in r.get_json()["details"][0].lower()


def test_hierarchy_self_reference_422(tctx):
    a = _acct(tctx, "Hier-Self")
    r = tctx.client.patch(f"/api/sobjects/Account/{a}", headers=tctx.h,
                          json={"ParentAccountId": a})
    assert r.status_code == 422, r.get_json()


def test_hierarchy_valid_chain_201(tctx):
    a = _acct(tctx, "Chain-A")
    b = _rid(tctx.client.post("/api/sobjects/Account", headers=tctx.h,
                              json={"Name": "Chain-B", "ParentAccountId": a}))
    c = _rid(tctx.client.post("/api/sobjects/Account", headers=tctx.h,
                              json={"Name": "Chain-C", "ParentAccountId": b}))
    assert c


# ------------------------------------------------------- cross-object formulas
@pytest.fixture()
def form_obj(tctx):
    _mkobj(tctx, "FormT")
    _addfield(tctx, "FormT", {"name": "AccountId", "label": "Account",
                              "type": "Lookup", "reference_to": "Account"})
    _addfield(tctx, "FormT", {"name": "AcctName", "label": "Acct Name",
                              "type": "Formula", "return_type": "Text",
                              "formula": {"field": "Account.Name"}})
    return "FormT"


def test_cross_object_formula(tctx, form_obj):
    aid = _acct(tctx, "Acme-Formula")
    rid = _rid(tctx.client.post("/api/sobjects/FormT", headers=tctx.h,
                                json={"AccountId": aid}))
    got = tctx.client.get(f"/api/sobjects/FormT/{rid}", headers=tctx.h).get_json()
    assert got["AcctName"] == "Acme-Formula"


def test_cross_object_formula_null_fk(tctx, form_obj):
    rid = _rid(tctx.client.post("/api/sobjects/FormT", headers=tctx.h, json={}))
    got = tctx.client.get(f"/api/sobjects/FormT/{rid}", headers=tctx.h).get_json()
    assert got["AcctName"] is None


def test_cross_object_formula_multilevel(tctx):
    _mkobj(tctx, "FormM")
    _addfield(tctx, "FormM", {"name": "AccountId", "label": "Account",
                              "type": "Lookup", "reference_to": "Account"})
    _addfield(tctx, "FormM", {"name": "GrandName", "label": "Grand Name",
                              "type": "Formula", "return_type": "Text",
                              "formula": {"field": "Account.ParentAccount.Name"}})
    gp = _acct(tctx, "GrandParent")
    p = _rid(tctx.client.post("/api/sobjects/Account", headers=tctx.h,
                              json={"Name": "Parent", "ParentAccountId": gp}))
    rid = _rid(tctx.client.post("/api/sobjects/FormM", headers=tctx.h,
                                json={"AccountId": p}))
    got = tctx.client.get(f"/api/sobjects/FormM/{rid}", headers=tctx.h).get_json()
    assert got["GrandName"] == "GrandParent"


def test_formula_bad_dotted_path_422(tctx):
    _mkobj(tctx, "FormBad")
    _addfield(tctx, "FormBad", {"name": "AccountId", "label": "Account",
                                "type": "Lookup", "reference_to": "Account"})
    r = tctx.client.post("/api/admin/objects/FormBad/fields", headers=tctx.h,
                         json={"name": "Bad", "label": "Bad",
                               "type": "Formula", "return_type": "Text",
                               "formula": {"field": "Account.NoSuchField"}})
    assert r.status_code == 422, r.get_json()


def test_formula_bad_plain_field_422(tctx):
    _mkobj(tctx, "FormBad2")
    r = tctx.client.post("/api/admin/objects/FormBad2/fields", headers=tctx.h,
                         json={"name": "Bad", "label": "Bad",
                               "type": "Formula", "return_type": "Text",
                               "formula": {"field": "NoSuchField"}})
    assert r.status_code == 422, r.get_json()


# ------------------------------------------------------- ?select= / ?children=
def test_select_dotted_parent(tctx):
    aid = _acct(tctx, "Acme-Select")
    _contact(tctx, AccountId=aid)
    r = tctx.client.get("/api/sobjects/Contact?select=LastName,Account.Name",
                        headers=tctx.h)
    assert r.status_code == 200, r.get_json()
    rows = r.get_json()
    row = next(x for x in rows if x.get("LastName") == "Smith")
    assert row["Account"] == {"Name": "Acme-Select"}
    assert "AccountId" not in row  # projection, not the full serialization


def test_select_unknown_field_dropped(tctx):
    _contact(tctx)
    r = tctx.client.get("/api/sobjects/Contact?select=LastName,BogusField",
                        headers=tctx.h)
    assert r.status_code == 200
    row = r.get_json()[0]
    assert "BogusField" not in row
    assert row["LastName"]


def test_children_subquery(tctx):
    aid = _acct(tctx, "Acme-Kids")
    cid = _contact(tctx, AccountId=aid)
    r = tctx.client.get(f"/api/sobjects/Account/{aid}?children=Contacts",
                        headers=tctx.h)
    assert r.status_code == 200, r.get_json()
    kids = r.get_json()["Contacts"]
    assert any(k["Id"] == cid for k in kids)


def test_children_on_list(tctx):
    aid = _acct(tctx, "Acme-Kids2")
    cid = _contact(tctx, AccountId=aid)
    r = tctx.client.get("/api/sobjects/Account?children=Contacts",
                        headers=tctx.h)
    assert r.status_code == 200, r.get_json()
    row = next(x for x in r.get_json() if x["Id"] == aid)
    assert any(k["Id"] == cid for k in row["Contacts"])


# ------------------------------------------------------- delete behaviors
def test_delete_block_422_and_survives(tctx):
    """OrderItem.OrderId is a required lookup -> deleting the Order blocks."""
    aid = _acct(tctx, "Acme-Block")
    oid = _rid(tctx.client.post("/api/sobjects/Order", headers=tctx.h,
                                json={"AccountId": aid}))
    _rid(tctx.client.post("/api/sobjects/OrderItem", headers=tctx.h,
                          json={"OrderId": oid, "Quantity": 2, "UnitPrice": 9.5}))
    r = tctx.client.delete(f"/api/sobjects/Order/{oid}", headers=tctx.h)
    assert r.status_code == 422, r.get_json()
    assert tctx.client.get(f"/api/sobjects/Order/{oid}", headers=tctx.h).status_code == 200


def test_delete_clear_nulls_fk(tctx):
    """Contact.AccountId clears (never orphans) when the Account is deleted."""
    aid = _acct(tctx, "Acme-Clear")
    cid = _contact(tctx, AccountId=aid)
    r = tctx.client.delete(f"/api/sobjects/Account/{aid}", headers=tctx.h)
    assert r.status_code == 200, r.get_json()
    got = tctx.client.get(f"/api/sobjects/Contact/{cid}", headers=tctx.h).get_json()
    assert got["AccountId"] is None


# ------------------------------------------------------- restore with children
def test_restore_parent_restores_children(tctx):
    oid = _rid(tctx.client.post(
        "/api/sobjects/Opportunity", headers=tctx.h,
        json={"Name": "Big Deal", "Stage": "Prospecting"}))
    oli = _rid(tctx.client.post(
        "/api/sobjects/OpportunityLineItem", headers=tctx.h,
        json={"OpportunityId": oid, "Quantity": 3, "UnitPrice": 100}))
    r = tctx.client.delete(f"/api/sobjects/Opportunity/{oid}", headers=tctx.h)
    assert r.status_code == 200, r.get_json()
    assert tctx.client.get(f"/api/sobjects/OpportunityLineItem/{oli}",
                           headers=tctx.h).status_code == 404
    bin_ = tctx.client.get("/api/recycle-bin", headers=tctx.h).get_json()
    parent = next(e for e in bin_ if e["object_name"] == "Opportunity")
    r = tctx.client.post(f"/api/recycle-bin/{parent['id']}/restore", headers=tctx.h)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["restored_count"] == 2
    assert tctx.client.get(f"/api/sobjects/Opportunity/{oid}",
                           headers=tctx.h).status_code == 200
    got = tctx.client.get(f"/api/sobjects/OpportunityLineItem/{oli}",
                          headers=tctx.h).get_json()
    assert got["OpportunityId"] == oid
