"""Tests for PATCH /api/admin/<kind>/<rid> (edit + activate/deactivate)."""
import os
import tempfile

import pytest

from forcelet.api import create_app


@pytest.fixture()
def client():
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_app(db)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    os.unlink(db)


def login(client):
    r = client.post("/api/login", json={"username": "admin", "password": "forcelet"})
    assert r.status_code == 200
    return {"Authorization": "Bearer " + r.get_json()["token"]}


def test_patch_toggle_active(client):
    h = login(client)
    r = client.post("/api/admin/validation-rules",
                    json={"name": "VR1", "object": "Opportunity",
                          "message": "bad", "condition": {}, "active": True},
                    headers=h)
    assert r.status_code == 201
    rid = r.get_json()["id"]

    r = client.patch(f"/api/admin/validation-rules/{rid}",
                     json={"active": False}, headers=h)
    assert r.status_code == 200
    body = r.get_json()
    assert body["active"] is False
    assert body["name"] == "VR1"  # other fields preserved

    r = client.get("/api/admin/validation-rules", headers=h)
    mine = [x for x in r.get_json() if x["id"] == rid][0]
    assert mine["active"] is False


def test_patch_edit_fields(client):
    h = login(client)
    r = client.post("/api/admin/flows",
                    json={"name": "F1", "object": "Lead", "trigger": "on_create",
                          "condition": {}, "actions": [], "active": True},
                    headers=h)
    rid = r.get_json()["id"]
    r = client.patch(f"/api/admin/flows/{rid}",
                     json={"name": "F1 renamed", "actions": [{"type": "log"}]},
                     headers=h)
    assert r.status_code == 200
    body = r.get_json()
    assert body["name"] == "F1 renamed"
    assert body["actions"] == [{"type": "log"}]
    assert body["object"] == "Lead"


def test_patch_unknown_kind_or_id(client):
    h = login(client)
    r = client.patch("/api/admin/nope/x", json={"active": False}, headers=h)
    assert r.status_code == 404
    r = client.patch("/api/admin/flows/does-not-exist",
                     json={"active": False}, headers=h)
    assert r.status_code == 404


def test_patch_requires_admin(client):
    # non-admin cannot patch
    r = client.post("/api/login", json={"username": "ana", "password": "forcelet"})
    h = {"Authorization": "Bearer " + r.get_json()["token"]}
    ha = login(client)
    r = client.post("/api/admin/triggers",
                    json={"name": "T1", "object": "Lead", "events": ["before_insert"],
                          "code": "pass", "active": True},
                    headers=ha)
    rid = r.get_json()["id"]
    r = client.patch(f"/api/admin/triggers/{rid}", json={"active": False}, headers=h)
    assert r.status_code == 403
