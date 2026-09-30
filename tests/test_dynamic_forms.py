"""Tests for Dynamic Forms: conditional field visibility rules."""
import pytest

from helpers import login
from forcelet import dynamic_forms as df
from forcelet.api import create_app


@pytest.fixture()
def app(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def admin(client):
    return login(client, "admin")


@pytest.fixture()
def leo(client):
    return login(client, "leo")


def _rule(name="R1", when=None, then=None, active=True):
    return {"name": name,
            "when": when or {"field": "Description", "operator": "equals",
                             "value": "HideSubj"},
            "then": then or {"action": "hide", "fields": ["Subject"]},
            "active": active}


def _mkrule(client, h, obj="Case", **kw):
    r = client.post(f"/api/dynamic-forms/{obj}/rules", headers=h,
                    json=_rule(**kw))
    assert r.status_code == 201, r.get_json()
    return r.get_json()


# ------------------------------------------------------------- rule CRUD/auth


def test_get_rules_requires_auth(client):
    r = client.get("/api/dynamic-forms/Case")
    assert r.status_code == 401


def test_get_rules_unknown_object(client, admin):
    r = client.get("/api/dynamic-forms/Nope", headers=admin)
    assert r.status_code == 404


def test_rule_crud_admin_only(client, admin, leo):
    body = _rule()
    for method, url in (("post", "/api/dynamic-forms/Case/rules"),):
        r = getattr(client, method)(url, headers=leo, json=body)
        assert r.status_code == 403, (method, r.get_json())
    rid = _mkrule(client, admin)["id"]
    r = client.put(f"/api/dynamic-forms/Case/rules/{rid}", headers=leo,
                   json={"active": False})
    assert r.status_code == 403
    r = client.delete(f"/api/dynamic-forms/Case/rules/{rid}", headers=leo)
    assert r.status_code == 403


def test_rule_crud_roundtrip(client, admin):
    assert client.get("/api/dynamic-forms/Case",
                      headers=admin).get_json() == {"rules": []}
    rid = _mkrule(client, admin)["id"]
    rules = client.get("/api/dynamic-forms/Case",
                       headers=admin).get_json()["rules"]
    assert [x["id"] for x in rules] == [rid]
    r = client.put(f"/api/dynamic-forms/Case/rules/{rid}", headers=admin,
                   json={"active": False})
    assert r.status_code == 200 and r.get_json()["active"] is False
    r = client.delete(f"/api/dynamic-forms/Case/rules/{rid}", headers=admin)
    assert r.status_code == 200
    assert client.get("/api/dynamic-forms/Case",
                      headers=admin).get_json() == {"rules": []}
    r = client.delete(f"/api/dynamic-forms/Case/rules/{rid}", headers=admin)
    assert r.status_code == 404


def test_rule_validation(client, admin):
    bad = dict(_rule(), when={"field": "Subject", "operator": "fuzzy",
                              "value": "x"})
    r = client.post("/api/dynamic-forms/Case/rules", headers=admin, json=bad)
    assert r.status_code == 422
    bad = dict(_rule(), when={"field": "Nope", "operator": "equals",
                              "value": "x"})
    r = client.post("/api/dynamic-forms/Case/rules", headers=admin, json=bad)
    assert r.status_code == 422
    bad = dict(_rule(), then={"action": "hide", "fields": ["Nope"]})
    r = client.post("/api/dynamic-forms/Case/rules", headers=admin, json=bad)
    assert r.status_code == 422
    bad = dict(_rule(), when={"field": "Subject", "operator": "equals"})
    r = client.post("/api/dynamic-forms/Case/rules", headers=admin, json=bad)
    assert r.status_code == 422  # value required for equals
    ok = dict(_rule(), when={"field": "Subject", "operator": "is_blank"})
    r = client.post("/api/dynamic-forms/Case/rules", headers=admin, json=ok)
    assert r.status_code == 201  # no value needed for is_blank
    r = client.post("/api/dynamic-forms/Nope/rules", headers=admin,
                    json=_rule())
    assert r.status_code == 422


# ------------------------------------------------------- evaluator semantics


def test_evaluator_operators():
    c = df.evaluate_condition
    assert c({"field": "A", "operator": "equals", "value": "x"}, {"A": "x"})
    assert c({"field": "A", "operator": "equals", "value": "true"}, {"A": True})
    assert not c({"field": "A", "operator": "equals", "value": "false"},
                 {"A": True})
    assert c({"field": "N", "operator": "greater_than", "value": "5"},
             {"N": 7})
    assert not c({"field": "N", "operator": "greater_than", "value": "5"},
                 {"N": "abc"})
    assert c({"field": "S", "operator": "contains", "value": "arr"},
             {"S": "Warranty"})
    assert c({"field": "S", "operator": "is_blank"}, {"S": "  "})
    assert c({"field": "S", "operator": "is_not_blank"}, {"S": "x"})
    assert c({"field": "A", "operator": "not_equals", "value": "x"},
             {"A": "y"})


def test_hidden_fields_semantics(client, app, admin):
    _mkrule(client, admin, name="hide",
            when={"field": "Subject", "operator": "equals", "value": "H"},
            then={"action": "hide", "fields": ["Status"]})
    _mkrule(client, admin, name="show",
            when={"field": "Subject", "operator": "contains", "value": "vip"},
            then={"action": "show", "fields": ["Description"]})
    with app.app_context():
        store = app.mf_store
        # hide-rule true + show-rule false -> both hidden
        assert df.hidden_fields(store, "Case",
                                {"Subject": "H"}) == {"Status", "Description"}
        # show-rule false -> Description hidden
        assert df.hidden_fields(store, "Case",
                                {"Subject": "x"}) == {"Description"}
        assert df.hidden_fields(store, "Case",
                                {"Subject": "vip client"}) == set()


# ------------------------------------------------- save-time required logic


def test_hidden_required_field_does_not_block_save(client, admin):
    _mkrule(client, admin)  # Description == "HideSubj" -> hide required Subject
    r = client.post("/api/sobjects/Case", headers=admin,
                    json={"Description": "HideSubj", "Status": "New"})
    assert r.status_code == 201, r.get_json()


def test_visible_required_field_still_required(client, admin):
    _mkrule(client, admin)  # Description == "HideSubj" -> hide required Subject
    r = client.post("/api/sobjects/Case", headers=admin,
                    json={"Description": "Something else", "Status": "New"})
    assert r.status_code == 422
    assert any("Subject" in d for d in r.get_json().get("details", []))


def test_show_rule_gates_required_field(client, admin):
    _mkrule(client, admin, name="gate",
            when={"field": "Description", "operator": "contains",
                  "value": "vip"},
            then={"action": "show", "fields": ["Subject"]})
    # condition false -> Subject hidden -> save succeeds without it
    r = client.post("/api/sobjects/Case", headers=admin,
                    json={"Description": "routine question", "Status": "New"})
    assert r.status_code == 201, r.get_json()
    # condition true -> Subject visible -> still required
    r = client.post("/api/sobjects/Case", headers=admin,
                    json={"Description": "vip escalation", "Status": "New"})
    assert r.status_code == 422


def test_inactive_rule_ignored_on_save(client, admin):
    _mkrule(client, admin, active=False)
    r = client.post("/api/sobjects/Case", headers=admin,
                    json={"Description": "HideSubj", "Status": "New"})
    assert r.status_code == 422
