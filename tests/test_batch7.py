"""Tests for Batch B: recycle bin, merge duplicates, OpenAPI, AI assistant."""
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


def _get(client, h, obj, rid):
    return client.get(f"/api/sobjects/{obj}/{rid}", headers=h).get_json()


# ------------------------------------------------------------ recycle bin
def test_delete_goes_to_recycle_bin_and_restores(client, admin):
    r = client.post("/api/sobjects/Account", headers=admin, json={"Name": "BinCo"})
    rid = r.get_json()["Id"]
    assert client.delete(f"/api/sobjects/Account/{rid}", headers=admin).status_code == 200
    assert client.get(f"/api/sobjects/Account/{rid}", headers=admin).status_code == 404

    bin_items = client.get("/api/recycle-bin", headers=admin).get_json()
    entry = next(b for b in bin_items if b["record_id"] == rid)
    r = client.post(f"/api/recycle-bin/{entry['id']}/restore", headers=admin)
    assert r.status_code == 200, r.get_json()
    assert _get(client, admin, "Account", rid)["Name"] == "BinCo"


def test_empty_recycle_bin(client, admin):
    r = client.post("/api/sobjects/Account", headers=admin, json={"Name": "PurgeCo"})
    rid = r.get_json()["Id"]
    client.delete(f"/api/sobjects/Account/{rid}", headers=admin)
    assert client.delete("/api/recycle-bin", headers=admin).status_code == 200
    assert client.get("/api/recycle-bin", headers=admin).get_json() == []


# ------------------------------------------------------------ merge duplicates
def test_merge_reparents_and_recycles(client, admin):
    a1 = client.post("/api/sobjects/Account", headers=admin,
                     json={"Name": "MergeCorp", "Phone": "111"}).get_json()["Id"]
    a2 = client.post("/api/sobjects/Account", headers=admin,
                     json={"Name": "MergeCorp", "Phone": "222"},
                     query_string={"allow_duplicates": "true"}).get_json()["Id"]
    task = client.post("/api/sobjects/Task", headers=admin,
                       json={"Subject": "T", "AccountId": a2}).get_json()["Id"]

    r = client.post(f"/api/sobjects/Account/{a1}/merge", headers=admin,
                    json={"merge_ids": [a2], "fields": {"Phone": "999"}})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["merged_ids"] == [a2]
    assert body["reparented"] >= 1
    assert _get(client, admin, "Account", a1)["Phone"] == "999"
    assert _get(client, admin, "Task", task)["AccountId"] == a1
    assert client.get(f"/api/sobjects/Account/{a2}", headers=admin).status_code == 404
    bin_ids = [b["record_id"] for b in
               client.get("/api/recycle-bin", headers=admin).get_json()]
    assert a2 in bin_ids


def test_duplicates_endpoint(client, admin):
    r = client.post("/api/sobjects/Lead", headers=admin,
                    json={"LastName": "Dup", "Company": "DupCo",
                          "Email": "dup@example.com"}).get_json()
    client.post("/api/sobjects/Lead", headers=admin,
                json={"LastName": "Dup", "Company": "DupCo", "Email": "dup@example.com"},
                query_string={"allow_duplicates": "true"})
    r = client.get(f"/api/sobjects/Lead/{r['Id']}/duplicates", headers=admin)
    assert r.status_code == 200 and len(r.get_json()) >= 1


# ------------------------------------------------------------ OpenAPI
def test_openapi_spec(client, admin):
    r = client.get("/api/openapi.json")
    assert r.status_code == 200, r.get_json()
    spec = r.get_json()
    assert spec["openapi"].startswith("3.0")
    assert spec["info"]["title"] == "Forcelet API"
    assert "/api/sobjects/{obj_name}/{rid}" in spec["paths"]
    assert "post" in spec["paths"]["/api/assistant"]


# ------------------------------------------------------------ assistant
def test_assistant_pipeline_and_counts(client, admin):
    r = client.post("/api/assistant", headers=admin, json={"message": "help"})
    assert "pipeline" in r.get_json()["reply"].lower()
    r = client.post("/api/assistant", headers=admin,
                    json={"message": "how many accounts"})
    assert "account" in r.get_json()["reply"].lower()


def test_assistant_create_task(client, admin):
    r = client.post("/api/assistant", headers=admin,
                    json={"message": "create task: call the fleet team"})
    assert r.status_code == 200, r.get_json()
    assert "created task" in r.get_json()["reply"].lower()
    tid = r.get_json()["data"]["ids"][0]
    assert _get(client, admin, "Task", tid)["Subject"] == "call the fleet team"


def test_assistant_summarize(client, admin):
    r = client.post("/api/assistant", headers=admin,
                    json={"message": "summarize order ORD-000001"})
    assert r.status_code == 200, r.get_json()
    assert "ORD-000001" in r.get_json()["reply"]
