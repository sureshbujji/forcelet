"""Tests for production hardening: secure sessions, brute-force protection,
rate limiting, security headers, health checks, backups, migrations, and
the scheduler single-runner lock."""
import os
from datetime import datetime, timedelta, timezone

import pytest

from helpers import login
from forcelet import scheduler as scheduler_mod
from forcelet.api import create_app
from forcelet.migrations import run_migrations
from forcelet.security import hash_token, reset_login_attempts


@pytest.fixture()
def app(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def _raw_login(client, username, password):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


# ------------------------------------------------------------------ sessions
def test_forged_session_token_rejected(client):
    body = _raw_login(client, "leo", "forcelet")
    forged = {"Authorization": "Bearer mf-" + body["user"]["id"]}
    assert client.get("/api/session", headers=forged).status_code == 401


def test_logout_revokes_session(client):
    h = login(client, "leo")
    assert client.get("/api/session", headers=h).status_code == 200
    assert client.post("/api/logout", headers=h).status_code == 200
    assert client.get("/api/session", headers=h).status_code == 401


def test_expired_session_rejected(app, client):
    h = login(client, "leo")
    token = h["Authorization"][len("Bearer "):]
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    app.mf_store._execute("UPDATE mf_sessions SET expires_at=? WHERE token_hash=?",
                          (past, hash_token(token)))
    app.mf_store._commit()
    assert client.get("/api/session", headers=h).status_code == 401


def test_change_password_kills_other_sessions(client):
    h1 = login(client, "leo")
    h2 = login(client, "leo", "TestPass123!")  # helper rotated the password
    assert client.get("/api/session", headers=h2).status_code == 200
    r = client.post("/api/change-password", headers=h1,
                    json={"current": "TestPass123!", "new": "AnotherPass1!"})
    assert r.status_code == 200, r.get_json()
    assert client.get("/api/session", headers=h2).status_code == 401
    assert client.get("/api/session", headers=h1).status_code == 200


def test_query_param_token_rejected_outside_streaming(client):
    h = login(client, "leo")
    token = h["Authorization"][len("Bearer "):]
    # query tokens are only honored on /api/streaming (EventSource)
    assert client.get("/api/session", query_string={"access_token": token}).status_code == 401
    assert client.get("/api/session", headers=h).status_code == 200


# ------------------------------------------------------- forced password change
def test_seeded_user_must_change_password(client):
    body = _raw_login(client, "leo", "forcelet")
    assert body["must_change_password"] is True
    h = {"Authorization": "Bearer " + body["token"]}
    r = client.get("/api/sobjects/Account", headers=h)
    assert r.status_code == 403
    assert r.get_json().get("must_change_password") is True
    # change-password itself is allowed on the limited session
    r = client.post("/api/change-password", headers=h,
                    json={"current": "forcelet", "new": "BrandNewPass1!"})
    assert r.status_code == 200, r.get_json()
    assert client.get("/api/sobjects/Account", headers=h).status_code == 200


# ------------------------------------------------------- brute force + rate limit
def test_brute_force_lockout(client):
    target = "no_such_user_xyz"
    for _ in range(5):
        r = client.post("/api/login", json={"username": target, "password": "wrong"})
        assert r.status_code == 401
    r = client.post("/api/login", json={"username": target, "password": "wrong"})
    assert r.status_code == 429
    reset_login_attempts("127.0.0.1", target)


def test_login_rate_limit(tmp_path):
    app = create_app(str(tmp_path / "rl.db"))  # TESTING not set: limits enforced
    c = app.test_client()
    statuses = set()
    for i in range(12):
        r = c.post("/api/login",
                   json={"username": f"ratelimit_user_{i}", "password": "wrong"})
        statuses.add(r.status_code)
    assert 429 in statuses  # the bucket (10 per 5 min) trips
    assert statuses <= {401, 429}


# ------------------------------------------------------- headers + health
def test_security_headers(client):
    r = client.get("/api/health")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert "default-src 'self'" in r.headers.get("Content-Security-Policy", "")
    assert "max-age=31536000" in r.headers.get("Strict-Transport-Security", "")


def test_health_endpoint(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["db"] == "ok"
    assert body["version"] and body["schema_version"] >= 1


# ------------------------------------------------------- request size cap
def test_oversized_upload_rejected(app, client, tmp_path, monkeypatch):
    monkeypatch.setenv("FORCELET_MAX_UPLOAD_MB", "0.0001")  # ~100 bytes
    tiny_app = create_app(str(tmp_path / "tiny.db"))
    tiny_app.config["TESTING"] = True
    c = tiny_app.test_client()
    r = c.post("/api/login", data="x" * 10000, content_type="application/json")
    assert r.status_code == 413


# ------------------------------------------------------- backups + migrations
def test_backup_create_and_list(client, tmp_path, monkeypatch):
    monkeypatch.setenv("FORCELET_BACKUP_DIR", str(tmp_path / "backups"))
    h = login(client, "admin")
    r = client.post("/api/admin/backups", headers=h)
    assert r.status_code == 201, r.get_json()
    name = r.get_json()["backup"]
    assert os.path.exists(tmp_path / "backups" / name)
    names = [b["name"] for b in client.get("/api/admin/backups", headers=h).get_json()]
    assert name in names


def test_migrations_baseline_version(app):
    assert app.mf_store.meta_kv_get("schema_version") == "3"
    assert run_migrations(app.mf_store) == 3


# ------------------------------------------------------- scheduler lock
def test_scheduler_lock_single_runner(app):
    store = app.mf_store
    assert scheduler_mod.acquire_lock(store) is True
    # fresh lock held by someone else: we must not take it
    fresh = (datetime.now(timezone.utc)).isoformat(timespec="seconds")
    store.meta_kv_set("scheduler_lock", f"other-holder|{fresh}")
    assert scheduler_mod.acquire_lock(store) is False
    # stale heartbeat: we take it over
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    store.meta_kv_set("scheduler_lock", f"other-holder|{stale}")
    assert scheduler_mod.acquire_lock(store) is True
