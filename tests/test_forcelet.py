"""Pytest suite for Forcelet: metadata, validation, security, sharing, API."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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


def test_login_rejects_bad_password(client):
    r = client.post("/api/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


@pytest.fixture()
def admin(client):
    return login(client, "admin")


@pytest.fixture()
def leo(client):
    return login(client, "leo")


@pytest.fixture()
def maya(client):
    return login(client, "maya")


@pytest.fixture()
def ana(client):
    return login(client, "ana")


# ------------------------------------------------------------ metadata
def test_standard_objects_seeded(client, admin):
    r = client.get("/api/objects", headers=admin)
    names = {o["name"] for o in r.get_json()}
    assert {"Account", "Contact", "Lead", "Opportunity", "Case"} <= names


def test_create_custom_object_and_fields(client, admin):
    r = client.post("/api/admin/objects", headers=admin,
                    json={"name": "Invoice", "label": "Invoice", "plural": "Invoices"})
    assert r.status_code == 201, r.get_json()
    r = client.post("/api/admin/objects/Invoice/fields", headers=admin,
                    json={"name": "Total", "label": "Total", "type": "Currency", "required": True})
    assert r.status_code == 201, r.get_json()
    r = client.post("/api/admin/objects/Invoice/fields", headers=admin,
                    json={"name": "Status", "label": "Status", "type": "Picklist",
                          "picklist_values": ["Draft", "Sent", "Paid"]})
    assert r.status_code == 201, r.get_json()
    r = client.get("/api/describe/Invoice", headers=admin)
    assert {f["name"] for f in r.get_json()["fields"]} == {"Total", "Status"}


def test_reject_bad_object_and_field_names(client, admin):
    r = client.post("/api/admin/objects", headers=admin, json={"name": "9Bad", "label": "x", "plural": "xs"})
    assert r.status_code == 422
    r = client.post("/api/admin/objects/Account/fields", headers=admin,
                    json={"name": "owner_id", "label": "x", "type": "Text"})
    assert r.status_code == 422  # reserved system field


# ------------------------------------------------------------ validation
def test_validation_errors(client, admin):
    # missing required LastName
    r = client.post("/api/sobjects/Contact", headers=admin, json={"FirstName": "No"})
    assert r.status_code == 422
    details = r.get_json()["details"]
    assert any("Last Name is required" in d for d in details)
    # bad picklist
    r = client.post("/api/sobjects/Lead", headers=admin,
                    json={"LastName": "X", "Company": "Y", "Status": "Bogus"})
    assert r.status_code == 422
    # bad email
    r = client.post("/api/sobjects/Contact", headers=admin,
                    json={"LastName": "X", "Email": "not-an-email"})
    assert r.status_code == 422


# ------------------------------------------------------------ permissions
def test_read_only_cannot_create(client, ana):
    r = client.post("/api/sobjects/Account", headers=ana, json={"Name": "Nope"})
    assert r.status_code in (403, 404)


def test_field_level_security_hides_amount(client, ana, admin):
    # a support-branch user owns the record so ana can see it via sharing
    r = client.post("/api/admin/users", headers=admin,
                    json={"username": "sam", "name": "Sam Support",
                          "profile": "Standard User", "role": "Support Agent"})
    assert r.status_code == 201, r.get_json()
    sam = login(client, "sam")
    r = client.post("/api/sobjects/Opportunity", headers=sam,
                    json={"Name": "Support Opp", "Amount": 9999, "Stage": "Prospecting"})
    assert r.status_code == 201, r.get_json()
    rid = r.get_json()["Id"]
    # owner with default field perms sees Amount
    r = client.get(f"/api/sobjects/Opportunity/{rid}", headers=sam)
    assert r.status_code == 200 and r.get_json()["Amount"] == 9999
    # ana (Read Only + explicit field deny) sees the record but not Amount
    r = client.get(f"/api/sobjects/Opportunity/{rid}", headers=ana)
    assert r.status_code == 200
    body = r.get_json()
    assert "Amount" not in body
    assert body["Name"] == "Support Opp"


def test_non_admin_cannot_use_admin_api(client, leo):
    r = client.post("/api/admin/objects", headers=leo, json={"name": "X", "label": "x", "plural": "xs"})
    assert r.status_code == 403


# ------------------------------------------------------------ sharing
def test_role_hierarchy_sharing(client, leo, maya, ana, admin):
    # leo (Sales Rep) creates an account
    r = client.post("/api/sobjects/Account", headers=leo, json={"Name": "Shared Acme"})
    assert r.status_code == 201
    rid = r.get_json()["Id"]
    # maya (Sales Manager, above leo) can see it
    r = client.get(f"/api/sobjects/Account/{rid}", headers=maya)
    assert r.status_code == 200
    # ana (Support branch) cannot
    r = client.get(f"/api/sobjects/Account/{rid}", headers=ana)
    assert r.status_code == 404
    # admin bypasses sharing
    r = client.get(f"/api/sobjects/Account/{rid}", headers=admin)
    assert r.status_code == 200


def test_roleless_user_sees_only_own_records(client, leo, admin):
    # Regression: _role_subtree(None) used to treat a role-less user as above
    # all root roles, so a user with no role could see everyone's records.
    r = client.post("/api/admin/users", headers=admin,
                    json={"username": "norole", "name": "No Role",
                          "profile": "Standard User"})
    assert r.status_code == 201, r.get_json()
    norole = login(client, "norole")
    # leo creates an account; the role-less user must not see it
    r = client.post("/api/sobjects/Account", headers=leo, json={"Name": "Leo Acme"})
    assert r.status_code == 201
    rid = r.get_json()["Id"]
    r = client.get(f"/api/sobjects/Account/{rid}", headers=norole)
    assert r.status_code == 404
    # but the role-less user sees records they own
    r = client.post("/api/sobjects/Account", headers=norole,
                    json={"Name": "Norole Acme"})
    assert r.status_code == 201
    own = r.get_json()["Id"]
    r = client.get(f"/api/sobjects/Account/{own}", headers=norole)
    assert r.status_code == 200


# ------------------------------------------------------------ layouts
def test_layout_resolution(client, leo):
    r = client.get("/api/layout/Opportunity", headers=leo)
    body = r.get_json()
    assert body["sections"], "default layout should have sections"
    assert any("Stage" in str(s) for s in body["sections"])


# ------------------------------------------------------------ api roundtrip
def test_full_crud_roundtrip(client, admin):
    r = client.post("/api/sobjects/Lead", headers=admin,
                    json={"LastName": "Tester", "Company": "TestCo", "Status": "New"})
    assert r.status_code == 201
    rid = r.get_json()["Id"]
    r = client.patch(f"/api/sobjects/Lead/{rid}", headers=admin, json={"Status": "Working"})
    assert r.get_json()["Status"] == "Working"
    r = client.delete(f"/api/sobjects/Lead/{rid}", headers=admin)
    assert r.get_json()["deleted"] is True
    r = client.get(f"/api/sobjects/Lead/{rid}", headers=admin)
    assert r.status_code == 404


def test_lookup_field(client, admin):
    r = client.post("/api/sobjects/Account", headers=admin, json={"Name": "LookupParent"})
    pid = r.get_json()["Id"]
    r = client.post("/api/sobjects/Contact", headers=admin,
                    json={"LastName": "Child", "AccountId": pid})
    assert r.status_code == 201
    assert r.get_json()["AccountId"] == pid


def test_user_and_role_management(client, admin):
    r = client.post("/api/admin/roles", headers=admin, json={"name": "Intern", "parent": "Sales Rep"})
    assert r.status_code == 201
    r = client.post("/api/admin/users", headers=admin,
                    json={"username": "zoe", "name": "Zoe Intern",
                          "profile": "Read Only", "role": "Intern"})
    assert r.status_code == 201
    assert r.get_json()["username"] == "zoe"
