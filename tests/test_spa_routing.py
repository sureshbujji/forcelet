"""Record deep-link URLs: the SPA is served at / and /r/<object>/<id>,
and /api/* routes are not shadowed by the SPA fallback."""
import pytest

from forcelet.api import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "spa.db"))
    app.config["TESTING"] = True
    return app.test_client()


def test_spa_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.content_type
    assert b"Forcelet" in r.data


def test_record_url_serves_spa(client):
    r = client.get("/r/Account/a_xxx001")
    assert r.status_code == 200
    assert "text/html" in r.content_type
    # same page as / — the frontend router renders the record
    assert r.data == client.get("/").data


def test_api_routes_not_shadowed(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_unknown_path_still_404s(client):
    assert client.get("/nope").status_code == 404
