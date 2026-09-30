"""Tests for the app data-model batch: master-detail relationships, person
accounts, territory management, big objects + archival, and the Geolocation /
Address / Time field types."""
import sqlite3

import pytest

from forcelet.api import create_app


@pytest.fixture()
def app(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, username="admin", password="forcelet"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.get_json()
    return {"Authorization": "Bearer " + r.get_json()["token"]}


def make_md_pair(client, h):
    assert client.post("/api/admin/objects", headers=h,
                       json={"name": "DMParent__c", "label": "DM Parent"}).status_code == 201
    assert client.post("/api/admin/objects", headers=h,
                       json={"name": "DMChild__c", "label": "DM Child"}).status_code == 201
    for obj in ("DMParent__c", "DMChild__c"):
        r = client.post(f"/api/admin/objects/{obj}/fields", headers=h, json={
            "name": "Name", "label": "Name", "type": "Text"})
        assert r.status_code == 201, r.get_json()
    r = client.post("/api/admin/objects/DMChild__c/fields", headers=h, json={
        "name": "Parent__c", "label": "Parent", "type": "MasterDetail",
        "reference_to": "DMParent__c"})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["required"] is True


# ------------------------------------------------------- master-detail
def test_master_detail_create_and_cascade_delete(client):
    h = login(client)
    make_md_pair(client, h)
    p = client.post("/api/sobjects/DMParent__c", headers=h, json={"Name": "P"}).get_json()
    c1 = client.post("/api/sobjects/DMChild__c", headers=h,
                     json={"Name": "C1", "Parent__c": p["Id"]}).get_json()
    client.post("/api/sobjects/DMChild__c", headers=h,
                json={"Name": "C2", "Parent__c": p["Id"]})
    r = client.delete(f"/api/sobjects/DMParent__c/{p['Id']}", headers=h)
    assert r.status_code == 200
    cascaded = r.get_json()["cascaded"]
    assert {c for c, _ in cascaded} == {"DMChild__c"}
    assert client.get(f"/api/sobjects/DMChild__c/{c1['Id']}", headers=h).status_code == 404
    # cascade-deleted children land in the recycle bin
    names = [e["object_name"] for e in
             client.get("/api/recycle-bin", headers=h).get_json()]
    assert "DMChild__c" in names


def test_master_detail_requires_parent_and_validates_it(client):
    h = login(client)
    make_md_pair(client, h)
    r = client.post("/api/sobjects/DMChild__c", headers=h, json={"Name": "orphan"})
    assert r.status_code == 422
    p = client.post("/api/sobjects/DMParent__c", headers=h, json={"Name": "P"}).get_json()
    r = client.post("/api/sobjects/DMChild__c", headers=h,
                    json={"Name": "x", "Parent__c": "nope123"})
    assert r.status_code == 422
    assert "does not exist" in r.get_json()["details"][0]


def test_master_detail_rejects_self_and_cycles(client):
    h = login(client)
    make_md_pair(client, h)
    r = client.post("/api/admin/objects/DMChild__c/fields", headers=h, json={
        "name": "Self__c", "label": "Self", "type": "MasterDetail",
        "reference_to": "DMChild__c"})
    assert r.status_code == 422
    r = client.post("/api/admin/objects/DMParent__c/fields", headers=h, json={
        "name": "Back__c", "label": "Back", "type": "MasterDetail",
        "reference_to": "DMChild__c"})
    assert r.status_code == 422
    assert "cycle" in r.get_json()["error"]


def test_master_detail_reparenting_rule(client):
    h = login(client)
    make_md_pair(client, h)
    r = client.post("/api/admin/objects", headers=h,
                    json={"name": "DMFixed__c", "label": "DM Fixed"})
    assert r.status_code == 201
    client.post("/api/admin/objects/DMFixed__c/fields", headers=h, json={
        "name": "Name", "label": "Name", "type": "Text"})
    r = client.post("/api/admin/objects/DMFixed__c/fields", headers=h, json={
        "name": "Parent__c", "label": "Parent", "type": "MasterDetail",
        "reference_to": "DMParent__c", "reparentable": False})
    assert r.status_code == 201
    p1 = client.post("/api/sobjects/DMParent__c", headers=h, json={"Name": "P1"}).get_json()
    p2 = client.post("/api/sobjects/DMParent__c", headers=h, json={"Name": "P2"}).get_json()
    rec = client.post("/api/sobjects/DMFixed__c", headers=h,
                      json={"Name": "F", "Parent__c": p1["Id"]}).get_json()
    r = client.patch(f"/api/sobjects/DMFixed__c/{rec['Id']}", headers=h,
                     json={"Parent__c": p2["Id"]})
    assert r.status_code == 422
    assert "reparentable" in r.get_json()["error"]
    # reparentable (default) fields allow the move
    c = client.post("/api/sobjects/DMChild__c", headers=h,
                    json={"Name": "C", "Parent__c": p1["Id"]}).get_json()
    r = client.patch(f"/api/sobjects/DMChild__c/{c['Id']}", headers=h,
                     json={"Parent__c": p2["Id"]})
    assert r.status_code == 200


def test_master_detail_sharing_inheritance(client):
    h = login(client)
    make_md_pair(client, h)
    r = client.post("/api/admin/users", headers=h, json={
        "username": "dmrep", "name": "DM Rep", "profile": "Standard User",
        "role": "Sales Rep", "password": "x"})
    assert r.status_code == 201, r.get_json()
    uid = r.get_json()["id"]
    # grant the rep object access via a permission set (standard profiles
    # have no rights on custom objects by default)
    ps = client.post("/api/admin/permission-sets", headers=h, json={
        "name": "DM Reader",
        "object_permissions": {
            "DMParent__c": {"read": True, "create": True, "edit": True},
            "DMChild__c": {"read": True, "create": True, "edit": True}}}).get_json()
    client.post(f"/api/admin/users/{uid}/permission-sets", headers=h,
                json={"permission_set": ps["id"]})
    p = client.post("/api/sobjects/DMParent__c", headers=h, json={"Name": "P"}).get_json()
    c = client.post("/api/sobjects/DMChild__c", headers=h,
                    json={"Name": "C", "Parent__c": p["Id"]}).get_json()
    # share the parent with the rep via a sharing rule; child must follow
    client.post("/api/admin/sharing-rules", headers=h, json={
        "name": "share parents", "object": "DMParent__c", "active": True,
        "criteria": {"==": [{"field": "Name"}, "P"]},
        "share_with": {"type": "user", "id": uid}})
    h2 = login(client, "dmrep", "x")
    assert client.get(f"/api/sobjects/DMParent__c/{p['Id']}", headers=h2).status_code == 200
    assert client.get(f"/api/sobjects/DMChild__c/{c['Id']}", headers=h2).status_code == 200


def test_relationships_listing(client):
    h = login(client)
    make_md_pair(client, h)
    rows = client.get("/api/admin/relationships", headers=h).get_json()
    md = [r for r in rows if r["type"] == "MasterDetail"]
    assert any(r["object"] == "DMChild__c" and r["target"] == "DMParent__c" for r in md)


# ------------------------------------------------------- person accounts
def test_person_accounts_enable_and_create(client):
    h = login(client)
    assert client.get("/api/admin/person-accounts", headers=h).get_json() == {"enabled": False}
    r = client.post("/api/admin/person-accounts/enable", headers=h)
    assert r.status_code == 200
    assert r.get_json()["enabled"] is True
    assert client.get("/api/admin/person-accounts", headers=h).get_json() == {"enabled": True}
    pa = client.post("/api/sobjects/Account", headers=h, json={
        "FirstName": "Jane", "LastName": "Doe", "IsPersonAccount": True,
        "PersonEmail": "jane@example.com", "RecordType": "PersonAccount"})
    assert pa.status_code == 201, pa.get_json()
    assert pa.get_json()["Name"] == "Jane Doe"
    got = client.get(f"/api/sobjects/Account/{pa.get_json()['Id']}", headers=h).get_json()
    assert got["Name"] == "Jane Doe"
    assert got["IsPersonAccount"] in (1, True)


# ------------------------------------------------------- territories
def test_territory_assignment_and_sharing(client):
    h = login(client)
    west = client.post("/api/admin/territories", headers=h,
                       json={"name": "West"}).get_json()
    norcal = client.post("/api/admin/territories", headers=h, json={
        "name": "NorCal", "parent_id": west["id"]}).get_json()
    tree = client.get("/api/admin/territories", headers=h).get_json()
    assert tree[0]["name"] == "West"
    assert tree[0]["children"][0]["name"] == "NorCal"
    rule = client.post("/api/admin/territory-rules", headers=h, json={
        "name": "CA rule", "territory_id": norcal["id"],
        "criteria": {"==": [{"field": "BillingState"}, "CA"]}}).get_json()
    acc = client.post("/api/sobjects/Account", headers=h, json={
        "Name": "Acme", "BillingState": "CA"}).get_json()
    other = client.post("/api/sobjects/Account", headers=h, json={
        "Name": "Globex", "BillingState": "NY"}).get_json()
    run = client.post("/api/admin/territory-rules/run", headers=h, json={}).get_json()
    assert run["accounts_assigned"] == 1
    terrs = client.get(f"/api/sobjects/Account/{acc['Id']}/territories",
                       headers=h).get_json()
    assert [t["name"] for t in terrs] == ["NorCal"]
    # rep in the parent territory sees the CA account via hierarchy, not the NY one
    r = client.post("/api/admin/users", headers=h, json={
        "username": "trep", "name": "T Rep", "profile": "Standard User",
        "role": "Sales Rep", "password": "x"})
    uid = r.get_json()["id"]
    client.post(f"/api/admin/territories/{west['id']}/users", headers=h,
                json={"user_id": uid})
    h2 = login(client, "trep", "x")
    assert client.get(f"/api/sobjects/Account/{acc['Id']}", headers=h2).status_code == 200
    assert client.get(f"/api/sobjects/Account/{other['Id']}", headers=h2).status_code == 404
    # hierarchy cycle protection
    r = client.patch(f"/api/admin/territories/{west['id']}", headers=h,
                     json={"parent_id": norcal["id"]})
    assert r.status_code == 422


def test_territory_delete_guards(client):
    h = login(client)
    t = client.post("/api/admin/territories", headers=h, json={"name": "Solo"}).get_json()
    child = client.post("/api/admin/territories", headers=h, json={
        "name": "Kid", "parent_id": t["id"]}).get_json()
    r = client.delete(f"/api/admin/territories/{t['id']}", headers=h)
    assert r.status_code == 422
    assert client.delete(f"/api/admin/territories/{child['id']}", headers=h).status_code == 200
    assert client.delete(f"/api/admin/territories/{t['id']}", headers=h).status_code == 200


# ------------------------------------------------------- big objects + archival
def test_big_object_is_append_only(client):
    h = login(client)
    r = client.post("/api/admin/big-objects", headers=h,
                    json={"name": "Click__b", "label": "Click"})
    assert r.status_code == 201, r.get_json()
    r = client.post("/api/admin/big-objects", headers=h, json={"name": "Clicks"})
    assert r.status_code == 422  # must end with __b
    client.post("/api/admin/objects/Click__b/fields", headers=h,
                json={"name": "Msg__c", "label": "Msg", "type": "Text"})
    rec = client.post("/api/sobjects/Click__b", headers=h,
                      json={"Msg__c": "hi"}).get_json()
    assert client.patch(f"/api/sobjects/Click__b/{rec['Id']}", headers=h,
                        json={"Msg__c": "x"}).status_code == 422
    assert client.delete(f"/api/sobjects/Click__b/{rec['Id']}",
                         headers=h).status_code == 422
    r = client.post("/api/bulk/jobs", headers=h,
                    json={"object": "Click__b", "operation": "update"})
    assert r.status_code == 422
    listed = client.get("/api/admin/big-objects", headers=h).get_json()
    assert any(b["name"] == "Click__b" and b["records"] == 1 for b in listed)


def test_archive_rule_moves_old_records(client, tmp_path):
    h = login(client)
    acc = client.post("/api/sobjects/Account", headers=h,
                      json={"Name": "OldCo"}).get_json()
    db_path = str(tmp_path / "t.db")
    con = sqlite3.connect(db_path)
    con.execute("UPDATE sobj_Account SET created_date='2020-01-01T00:00:00+00:00' WHERE id=?",
                (acc["Id"],))
    con.commit()
    con.close()
    rule = client.post("/api/admin/archive-rules", headers=h, json={
        "name": "Old accounts", "object": "Account", "age_days": 365}).get_json()
    assert rule["target"] == "AccountArchive__b"
    run = client.post(f"/api/admin/archive-rules/{rule['id']}/run",
                      headers=h).get_json()
    assert run["moved"] == 1
    assert client.get(f"/api/sobjects/Account/{acc['Id']}",
                      headers=h).status_code == 404
    archived = client.get("/api/sobjects/AccountArchive__b", headers=h).get_json()
    assert len(archived) == 1
    assert archived[0]["OriginalId"] == acc["Id"]
    assert archived[0]["Name"] == "OldCo"


# ------------------------------------------------------- new field types
def test_geolocation_address_time_fields(client):
    h = login(client)
    client.post("/api/admin/objects", headers=h, json={"name": "Loc__c", "label": "Loc"})
    for name, ftype in (("Geo__c", "Geolocation"), ("Addr__c", "Address"),
                        ("At__c", "Time")):
        r = client.post("/api/admin/objects/Loc__c/fields", headers=h,
                        json={"name": name, "label": name, "type": ftype})
        assert r.status_code == 201, r.get_json()
    r = client.post("/api/sobjects/Loc__c", headers=h, json={
        "Name": "HQ", "Geo__c": "37.77;-122.41",
        "Addr__c": {"street": "1 Market", "city": "SF", "country": "USA"},
        "At__c": "9:30"})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["Geo__c"] == "37.77;-122.41"
    assert body["At__c"] == "09:30:00"
    assert '"city": "SF"' in body["Addr__c"]
    # invalid values rejected
    assert client.post("/api/sobjects/Loc__c", headers=h, json={
        "Name": "bad", "Geo__c": "999;999"}).status_code == 422
    assert client.post("/api/sobjects/Loc__c", headers=h, json={
        "Name": "bad", "At__c": "25:00"}).status_code == 422
    assert client.post("/api/sobjects/Loc__c", headers=h, json={
        "Name": "bad", "Addr__c": "nowhere"}).status_code == 422
    # dict-style geolocation also accepted
    r = client.post("/api/sobjects/Loc__c", headers=h, json={
        "Name": "D", "Geo__c": {"latitude": 40.7, "longitude": -74.0}})
    assert r.status_code == 201


# ------------------------------------------------- app UI support
def test_big_object_sobjects_crud_semantics(client):
    """The app UI browses big objects through the standard sobjects API:
    insert/list/describe work, update/delete are rejected (append-only)."""
    h = login(client)
    r = client.post("/api/admin/big-objects", headers=h,
                    json={"label": "UI Log", "name": "UILog__b"})
    assert r.status_code == 201, r.get_json()
    r = client.post("/api/admin/objects/UILog__b/fields", headers=h, json={
        "name": "Msg", "label": "Message", "type": "Text"})
    assert r.status_code == 201, r.get_json()
    rec = client.post("/api/sobjects/UILog__b", headers=h,
                      json={"Msg": "hello"}).get_json()
    assert rec["Msg"] == "hello"
    rows = client.get("/api/sobjects/UILog__b", headers=h).get_json()
    assert [x["Id"] for x in rows] == [rec["Id"]]
    desc = client.get("/api/describe/UILog__b", headers=h).get_json()
    assert desc["label"] == "UI Log"
    assert "Msg" in {f["name"] for f in desc["fields"]}
    # append-only: updates and deletes rejected
    r = client.patch(f"/api/sobjects/UILog__b/{rec['Id']}", headers=h,
                     json={"Msg": "changed"})
    assert r.status_code == 422
    r = client.delete(f"/api/sobjects/UILog__b/{rec['Id']}", headers=h)
    assert r.status_code == 422


def test_describe_exposes_relationship_metadata_for_related_lists(client):
    """Related lists need field type + reference_to to render Master-Detail
    vs Lookup badges and parent links."""
    h = login(client)
    make_md_pair(client, h)
    desc = client.get("/api/describe/DMChild__c", headers=h).get_json()
    md = next(f for f in desc["fields"] if f["name"] == "Parent__c")
    assert md["type"] == "MasterDetail"
    assert md["reference_to"] == "DMParent__c"
    assert md["required"] is True


def test_person_account_fields_in_describe(client):
    """The app UI Person card reads these fields from describe + record."""
    h = login(client)
    r = client.post("/api/admin/person-accounts/enable", headers=h)
    assert r.status_code == 200
    desc = client.get("/api/describe/Account", headers=h).get_json()
    names = {f["name"] for f in desc["fields"]}
    assert {"FirstName", "LastName", "PersonEmail", "PersonPhone",
            "IsPersonAccount"} <= names
    rec = client.post("/api/sobjects/Account", headers=h, json={
        "RecordType": "PersonAccount", "FirstName": "Ada", "LastName": "Lovelace",
        "PersonEmail": "ada@example.com"}).get_json()
    assert rec["IsPersonAccount"] in (True, 1)
    assert rec["Name"] == "Ada Lovelace"
    got = client.get(f"/api/sobjects/Account/{rec['Id']}", headers=h).get_json()
    assert got["PersonEmail"] == "ada@example.com"
    assert got["Name"] == "Ada Lovelace"
