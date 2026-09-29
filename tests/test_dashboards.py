"""Tests for /api/dashboards layout CRUD."""
import pytest

from forcelet.api import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    app.config["TESTING"] = True
    return app.test_client()


def login(client, username, password="forcelet"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.get_json()
    return {"Authorization": "Bearer " + r.get_json()["token"]}


def test_dashboard_crud(client):
    h = login(client, "admin")
    assert client.get("/api/dashboards", headers=h).get_json() == []
    r = client.post("/api/dashboards", headers=h,
                    json={"name": "Main", "widgets": [{"report_id": "r1", "type": "bar", "w": 1}]})
    assert r.status_code == 201
    did = r.get_json()["id"]
    assert r.get_json()["widgets"][0]["type"] == "bar"
    r = client.put(f"/api/dashboards/{did}", headers=h,
                   json={"name": "Main 2", "widgets": []})
    assert r.status_code == 200
    assert r.get_json()["name"] == "Main 2"
    assert client.get("/api/dashboards", headers=h).get_json()[0]["id"] == did
    assert client.delete(f"/api/dashboards/{did}", headers=h).status_code == 200
    assert client.get("/api/dashboards", headers=h).get_json() == []


def test_dashboard_404_and_forbidden(client):
    admin = login(client, "admin")
    assert client.put("/api/dashboards/nope", headers=admin, json={}).status_code == 404
    assert client.delete("/api/dashboards/nope", headers=admin).status_code == 404
    # standard user can read but not write
    r = client.post("/api/login", json={"username": "leo", "password": "forcelet"})
    assert r.status_code == 200, r.get_json()
    uh = {"Authorization": "Bearer " + r.get_json()["token"]}
    assert client.get("/api/dashboards", headers=uh).status_code == 200
    assert client.post("/api/dashboards", headers=uh, json={"name": "x"}).status_code in (401, 403)
