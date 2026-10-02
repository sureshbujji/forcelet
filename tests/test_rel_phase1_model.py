"""Phase 1 model-layer tests for the relationship-gap fixes.

Covers: per-field delete behaviors (clear/block/cascade), recycle-bin
parent linkage, hierarchy cycle guard, PolymorphicLookup fields, and
indirect (external-ID) lookup resolution.
"""
import os
import sys
import tempfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers import login
from forcelet.api import create_app
from forcelet import datamodel
from forcelet.field_types import validate_polymorphic_definition, validate_value


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


def _fmap(tctx, obj_name):
    return tctx.registry.field_map(tctx.registry.get_object(obj_name))


# ------------------------------------------------------- delete behaviors
def test_delete_clear_nulls_fk(tctx):
    """Deleting an Account clears (not orphans) Contact.AccountId."""
    aid = _rid(tctx.client.post("/api/sobjects/Account", headers=tctx.h,
                                json={"Name": "Acme"}))
    cid = _rid(tctx.client.post("/api/sobjects/Contact", headers=tctx.h,
                                json={"LastName": "Smith", "AccountId": aid}))
    assert datamodel.get_delete_behavior(_fmap(tctx, "Contact")["AccountId"]) == "clear"
    r = tctx.client.delete(f"/api/sobjects/Account/{aid}", headers=tctx.h)
    assert r.status_code == 200, r.get_json()
    contact = tctx.client.get(f"/api/sobjects/Contact/{cid}", headers=tctx.h).get_json()
    assert contact["AccountId"] is None


def test_delete_block_required_lookup(tctx):
    """Deleting a Contact referenced by a required lookup is blocked."""
    cid = _rid(tctx.client.post("/api/sobjects/Contact", headers=tctx.h,
                                json={"LastName": "Smith"}))
    oid = _rid(tctx.client.post("/api/sobjects/Opportunity", headers=tctx.h,
                                json={"Name": "Opp", "Stage": "Prospecting",
                                      "CloseDate": "2026-12-01"}))
    _rid(tctx.client.post("/api/sobjects/OpportunityContactRole", headers=tctx.h,
                           json={"OpportunityId": oid, "ContactId": cid}))
    assert datamodel.get_delete_behavior(
        _fmap(tctx, "OpportunityContactRole")["ContactId"]) == "block"
    r = tctx.client.delete(f"/api/sobjects/Contact/{cid}", headers=tctx.h)
    assert r.status_code == 422, r.get_json()
    assert "related" in r.get_json()["error"]
    # contact still alive
    assert tctx.client.get(f"/api/sobjects/Contact/{cid}",
                           headers=tctx.h).status_code == 200


def test_delete_cascade_curated_parent(tctx):
    """Deleting a Campaign cascades its CampaignMembers (now master-detail)."""
    cid = _rid(tctx.client.post("/api/sobjects/Campaign", headers=tctx.h,
                                json={"Name": "Q4 Push"}))
    mid = _rid(tctx.client.post("/api/sobjects/CampaignMember", headers=tctx.h,
                                json={"CampaignId": cid}))
    r = tctx.client.delete(f"/api/sobjects/Campaign/{cid}", headers=tctx.h)
    assert r.status_code == 200, r.get_json()
    assert ("CampaignMember", mid) in [tuple(x) for x in r.get_json()["cascaded"]]
    assert tctx.client.get(f"/api/sobjects/CampaignMember/{mid}",
                           headers=tctx.h).status_code == 404


def test_get_delete_behavior_precedence(tctx):
    gb = datamodel.get_delete_behavior
    # explicit option wins over everything
    assert gb({"name": "X", "type": "Lookup", "required": True,
               "delete_behavior": "clear"}) == "clear"
    assert gb({"name": "X", "type": "Lookup", "delete_behavior": "BLOCK"}) == "block"
    # master-detail always cascades
    assert gb({"name": "X", "type": "MasterDetail"}) == "cascade"
    # curated L6 registry cascades (matched by field name)
    assert gb({"name": "OpportunityId", "type": "Lookup"}) == "cascade"
    # required lookups block
    assert gb({"name": "X", "type": "Lookup", "required": True}) == "block"
    # Salesforce default for optional lookups
    assert gb({"name": "X", "type": "Lookup"}) == "clear"
    assert gb({"name": "X", "type": "PolymorphicLookup",
               "reference_to": ["Contact"]}) == "clear"


# ------------------------------------------------------- recycle linkage
def test_recycle_children_links_cascade(tctx):
    cid = _rid(tctx.client.post("/api/sobjects/Campaign", headers=tctx.h,
                                json={"Name": "Q4 Push"}))
    mid = _rid(tctx.client.post("/api/sobjects/CampaignMember", headers=tctx.h,
                                json={"CampaignId": cid}))
    r = tctx.client.delete(f"/api/sobjects/Campaign/{cid}", headers=tctx.h)
    assert r.status_code == 200, r.get_json()
    children = tctx.store.recycle_children("Campaign", cid)
    assert len(children) == 1
    assert children[0]["object_name"] == "CampaignMember"
    assert children[0]["record_id"] == mid
    assert children[0]["parent_object"] == "Campaign"
    assert children[0]["parent_id"] == cid
    # the parent's own entry is not its child
    assert all(e["record_id"] != cid for e in children)


def test_recycle_put_backward_compatible(tctx):
    bid = tctx.store.recycle_put("Account", {"id": "x1", "Name": "N"}, "u1")
    entry = tctx.store.recycle_get(bid)
    assert entry["parent_object"] is None and entry["parent_id"] is None
    assert tctx.store.recycle_children("Account", "x1") == []


# ------------------------------------------------------- hierarchy guard
def test_hierarchy_cycle_rejected(tctx):
    fdef = _fmap(tctx, "Account")["ParentAccountId"]
    a = _rid(tctx.client.post("/api/sobjects/Account", headers=tctx.h,
                              json={"Name": "A"}))
    b = _rid(tctx.client.post("/api/sobjects/Account", headers=tctx.h,
                              json={"Name": "B"}))
    tctx.store.update("Account", b, {"ParentAccountId": a})
    # A -> B would close the loop B -> A
    with pytest.raises(ValueError):
        datamodel.validate_hierarchy_no_cycle(tctx.store, tctx.registry, "Account",
                                              a, fdef, b)
    # self-parent rejected
    with pytest.raises(ValueError):
        datamodel.validate_hierarchy_no_cycle(tctx.store, tctx.registry, "Account",
                                              a, fdef, a)
    # a clean parent is fine (B's chain is B -> A -> None)
    c = _rid(tctx.client.post("/api/sobjects/Account", headers=tctx.h,
                              json={"Name": "C"}))
    datamodel.validate_hierarchy_no_cycle(tctx.store, tctx.registry, "Account",
                                           c, fdef, b)
    # non-self-referencing fields are a no-op
    other = _fmap(tctx, "Contact")["AccountId"]
    datamodel.validate_hierarchy_no_cycle(tctx.store, tctx.registry, "Contact",
                                           None, other, a)


def test_hierarchy_new_record_parent_chain(tctx):
    fdef = _fmap(tctx, "Account")["ParentAccountId"]
    a = _rid(tctx.client.post("/api/sobjects/Account", headers=tctx.h,
                              json={"Name": "A"}))
    # new record under A: fine
    datamodel.validate_hierarchy_no_cycle(tctx.store, tctx.registry, "Account",
                                           None, fdef, a)
    # None parent: fine
    datamodel.validate_hierarchy_no_cycle(tctx.store, tctx.registry, "Account",
                                           None, fdef, None)


# ------------------------------------------------------- polymorphic lookup
def test_polymorphic_resolve_accepts_listed_object(tctx):
    cid = _rid(tctx.client.post("/api/sobjects/Contact", headers=tctx.h,
                                json={"LastName": "Smith"}))
    who = _fmap(tctx, "Task")["WhoId"]
    assert who["type"] == "PolymorphicLookup"
    assert datamodel.resolve_lookup_value(tctx.store, tctx.registry, "Task",
                                           who, cid) == cid


def test_polymorphic_resolve_rejects_unlisted_object(tctx):
    aid = _rid(tctx.client.post("/api/sobjects/Account", headers=tctx.h,
                                json={"Name": "Acme"}))
    who = _fmap(tctx, "Task")["WhoId"]  # Contact/Lead only
    with pytest.raises(ValueError):
        datamodel.resolve_lookup_value(tctx.store, tctx.registry, "Task", who, aid)


def test_polymorphic_definition_validation():
    validate_polymorphic_definition({"name": "WhoId", "type": "PolymorphicLookup",
                                     "reference_to": ["Contact", "Lead"]})
    for bad in (None, [], "Contact", [""]):
        with pytest.raises(ValueError):
            validate_polymorphic_definition({"name": "WhoId",
                                             "type": "PolymorphicLookup",
                                             "reference_to": bad})


def test_polymorphic_validate_value_shape():
    f = {"name": "WhoId", "label": "Who", "type": "PolymorphicLookup",
         "reference_to": ["Contact", "Lead"]}
    ok, v, err = validate_value(f, "abc123")
    assert (ok, v, err) == (True, "abc123", None)
    ok, v, err = validate_value(f, None)
    assert (ok, v) == (True, None)


def test_relationship_fields_includes_polymorphic(tctx):
    names = {f["name"] for f in
             datamodel.relationship_fields(tctx.registry.get_object("Task"))}
    assert {"WhoId", "WhatId", "AccountId"} <= names


# ------------------------------------------------------- indirect lookup
def test_indirect_lookup_single_match(tctx):
    _rid(tctx.client.post("/api/sobjects/Contact", headers=tctx.h,
                           json={"LastName": "Smith", "Email": "smith@example.com"}))
    who = _fmap(tctx, "Task")["WhoId"]
    cid = _rid(tctx.client.post("/api/sobjects/Contact", headers=tctx.h,
                                json={"LastName": "Jones", "Email": "jones@example.com"}))
    got = datamodel.resolve_lookup_value(tctx.store, tctx.registry, "Task", who,
                                          {"Email": "jones@example.com"})
    assert got == cid


def test_indirect_lookup_no_match_raises(tctx):
    who = _fmap(tctx, "Task")["WhoId"]
    with pytest.raises(ValueError):
        datamodel.resolve_lookup_value(tctx.store, tctx.registry, "Task", who,
                                        {"Email": "nobody@example.com"})


def test_indirect_lookup_multiple_matches_raise(tctx):
    for ln in ("Smith", "Jones"):
        _rid(tctx.client.post("/api/sobjects/Contact", headers=tctx.h,
                               json={"LastName": ln, "Email": "dup@example.com"}))
    who = _fmap(tctx, "Task")["WhoId"]
    with pytest.raises(ValueError):
        datamodel.resolve_lookup_value(tctx.store, tctx.registry, "Task", who,
                                        {"Email": "dup@example.com"})


def test_indirect_lookup_bad_field_raises(tctx):
    who = _fmap(tctx, "Task")["WhoId"]
    with pytest.raises(ValueError):
        datamodel.resolve_lookup_value(tctx.store, tctx.registry, "Task", who,
                                        {"NoSuchField": "x"})


def test_resolve_none_and_required(tctx):
    opt = _fmap(tctx, "Contact")["AccountId"]
    assert datamodel.resolve_lookup_value(tctx.store, tctx.registry, "Contact",
                                           opt, None) is None
    req = _fmap(tctx, "OpportunityContactRole")["ContactId"]
    with pytest.raises(ValueError):
        datamodel.resolve_lookup_value(tctx.store, tctx.registry,
                                        "OpportunityContactRole", req, None)
    # plain Id existence check
    with pytest.raises(ValueError):
        datamodel.resolve_lookup_value(tctx.store, tctx.registry, "Contact",
                                        opt, "nonexistent-id")
