"""Shared test helpers for the Forcelet suite."""


def login(client, username="admin", password="forcelet"):
    """Log in and return an Authorization header dict.

    Completes the forced password-change flow when the account still has
    seeded/default credentials, so tests exercise the production login path.
    """
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    if body.get("must_change_password"):
        h = {"Authorization": "Bearer " + body["token"]}
        r2 = client.post("/api/change-password", headers=h,
                         json={"current": password, "new": "TestPass123!"})
        assert r2.status_code == 200, r2.get_json()
        r = client.post("/api/login",
                        json={"username": username, "password": "TestPass123!"})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert not body.get("must_change_password")
    return {"Authorization": "Bearer " + body["token"]}
