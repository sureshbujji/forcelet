"""Tests for the /api/users/names endpoint (owner-name resolution for the UI)."""
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
    try:
        os.unlink(db)
    except OSError:
        pass


def test_user_names_requires_auth(client):
    r = client.get("/api/users/names")
    assert r.status_code == 401


def test_user_names_returns_id_to_name_map(client):
    h = login(client)
    r = client.get("/api/users/names", headers=h)
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, dict)
    assert data, "expected at least the seeded admin user"
    for uid, name in data.items():
        assert isinstance(name, str) and name.strip()
        assert "password" not in name.lower()


def test_user_names_no_password_hash_leak(client):
    h = login(client)
    r = client.get("/api/users/names", headers=h)
    body = r.get_data(as_text=True)
    assert "password_hash" not in body
