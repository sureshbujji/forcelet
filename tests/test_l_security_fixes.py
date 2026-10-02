"""Tests for the L1/L2/L3 critical security fixes (2026-10-01).

L1: SSE streaming (/api/streaming) must enforce record sharing + FLS per
    subscriber — a restricted user must not receive another user's records,
    and snapshots must mask unreadable fields.
L2: CDC (/api/change-events) must enforce the same filtering.
L3: Sandbox create/refresh must sanitize live credentials (sessions, API
    keys, password hashes, TOTP secrets, named-credential secrets).
"""
import json
import sqlite3

import pytest

from forcelet import devops
from forcelet.api import create_app
from forcelet.security import verify_password
from helpers import login


@pytest.fixture()
def app(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def _clean_broker():
    # The streaming broker is process-global; isolate tests from each other.
    devops.broker._buffer.clear()
    yield
    devops.broker._buffer.clear()


def _mk_profile(client, h, name, field_perms=None):
    body = {"name": name,
            "object_permissions": {"Account": {"read": True, "create": True,
                                               "edit": True, "delete": True}},
            "field_permissions": field_perms or {}}
    r = client.post("/api/admin/profiles", headers=h, json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def _mk_user(client, h, username, profile):
    r = client.post("/api/admin/users", headers=h,
                    json={"username": username, "name": username.title(),
                          "profile": profile, "password": "UserPass1!"})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


class _Break(Exception):
    """Raised out of broker.wait() so the infinite SSE generator stops."""


def _sse_text(app, url, headers, monkeypatch):
    """Drive the /api/streaming view directly and return streamed text.

    The generator is consumed inside the request context because event
    filtering needs the app/request context on every chunk.
    """
    monkeypatch.setattr(devops.broker, "wait",
                        lambda timeout: (_ for _ in ()).throw(_Break()))
    out = []
    with app.test_request_context(url, headers=headers):
        resp = app.view_functions["streaming"]()
        assert resp.status_code == 200
        try:
            for _ in range(200):
                out.append(next(resp.response))
        except (_Break, StopIteration):
            pass
    return "".join(out)


# ------------------------------------------------------------------ L1: SSE
def test_sse_hides_records_subscriber_cannot_see(client, app, monkeypatch):
    h_admin = login(client)
    _mk_profile(client, h_admin, "L1Prof")
    _mk_user(client, h_admin, "l1bob", "L1Prof")
    h_bob = login(client, "l1bob", password="UserPass1!")

    r = client.post("/api/sobjects/Account", headers=h_admin,
                    json={"Name": "Admin Only Acct", "AnnualRevenue": 5000})
    assert r.status_code == 201, r.get_json()
    acct_id = r.get_json()["Id"]

    # Bob (no role -> sees only his own records) must not get the event.
    text = _sse_text(app, "/api/streaming?topics=/data/AccountChangeEvent",
                     h_bob, monkeypatch)
    assert acct_id not in text

    # Admin sees it (positive control).
    text = _sse_text(app, "/api/streaming?topics=/data/AccountChangeEvent",
                     h_admin, monkeypatch)
    assert acct_id in text


def test_sse_masks_unreadable_fields(client, app, monkeypatch):
    h_admin = login(client)
    _mk_profile(client, h_admin, "L1NoRev",
                field_perms={"Account": {"AnnualRevenue": {"read": False}}})
    _mk_user(client, h_admin, "l1carol", "L1NoRev")
    h_carol = login(client, "l1carol", password="UserPass1!")

    # Carol's own account: she sees the record but not AnnualRevenue.
    r = client.post("/api/sobjects/Account", headers=h_carol,
                    json={"Name": "Carol Acct", "AnnualRevenue": 7777})
    assert r.status_code == 201, r.get_json()
    acct_id = r.get_json()["Id"]

    text = _sse_text(app, "/api/streaming?topics=/data/AccountChangeEvent",
                     h_carol, monkeypatch)
    assert acct_id in text  # event delivered (she owns the record)
    assert "7777" not in text  # but the restricted field is masked
    assert "AnnualRevenue" not in text


def test_sse_platform_event_embeds_are_filtered(client, app, monkeypatch):
    h_admin = login(client)
    _mk_profile(client, h_admin, "L1P2")
    _mk_user(client, h_admin, "l1dave", "L1P2")
    h_dave = login(client, "l1dave", password="UserPass1!")

    r = client.post("/api/sobjects/Account", headers=h_admin,
                    json={"Name": "Hidden Acct"})
    acct_id = r.get_json()["Id"]
    rec = app.mf_store.get("Account", acct_id)

    # Platform event embedding a record Dave cannot see -> dropped for Dave.
    r = client.post("/api/streaming/events", headers=h_admin,
                    json={"name": "AcctAlert",
                          "payload": {"object_name": "Account",
                                      "record_id": acct_id,
                                      "record": dict(rec)}})
    assert r.status_code == 201, r.get_json()
    text = _sse_text(app, "/api/streaming?topics=/event/AcctAlert__e",
                     h_dave, monkeypatch)
    assert acct_id not in text
    text = _sse_text(app, "/api/streaming?topics=/event/AcctAlert__e",
                     h_admin, monkeypatch)
    assert acct_id in text


# ------------------------------------------------------------------ L2: CDC
def test_cdc_hides_records_caller_cannot_see(client):
    h_admin = login(client)
    _mk_profile(client, h_admin, "L2Prof")
    _mk_user(client, h_admin, "l2bob", "L2Prof")
    h_bob = login(client, "l2bob", password="UserPass1!")

    r = client.post("/api/sobjects/Account", headers=h_admin,
                    json={"Name": "Admin Only CDC"})
    acct_id = r.get_json()["Id"]

    r = client.get("/api/change-events?object=Account", headers=h_bob)
    assert r.status_code == 200
    assert all(e["record_id"] != acct_id for e in r.get_json())

    r = client.get("/api/change-events?object=Account", headers=h_admin)
    assert any(e["record_id"] == acct_id for e in r.get_json())


def test_cdc_masks_unreadable_fields_and_delete_fallback(client):
    h_admin = login(client)
    _mk_profile(client, h_admin, "L2NoRev",
                field_perms={"Account": {"AnnualRevenue": {"read": False}}})
    _mk_user(client, h_admin, "l2carol", "L2NoRev")
    h_carol = login(client, "l2carol", password="UserPass1!")

    r = client.post("/api/sobjects/Account", headers=h_carol,
                    json={"Name": "Carol CDC", "AnnualRevenue": 4321})
    acct_id = r.get_json()["Id"]

    # Admin deletes it; Carol's delete event must come from the snapshot
    # (record is gone) and still be field-masked.
    r = client.delete(f"/api/sobjects/Account/{acct_id}", headers=h_admin)
    assert r.status_code == 200, r.get_json()

    r = client.get("/api/change-events?object=Account", headers=h_carol)
    assert r.status_code == 200
    mine = [e for e in r.get_json() if e["record_id"] == acct_id]
    assert mine, "carol should see events for her own (deleted) record"
    for e in mine:
        assert "AnnualRevenue" not in (e.get("snapshot") or {})
        assert "AnnualRevenue" not in (e.get("changed_fields") or [])
        assert "4321" not in json.dumps(e.get("snapshot") or {})


# ------------------------------------------------------------------ L3: sandbox sanitization
def _seed_credentials(client, app):
    """Seed every credential kind in the prod DB; return the admin user."""
    h = login(client)  # creates a live session row
    store, security = app.mf_store, app.mf_security
    admin = security.get_user_by_username("admin")
    store.put_api_key("ab" * 32, "ci key", admin["id"])
    admin["totp_secret"] = "JBSWY3DPEHPK3PXP"
    store.meta_put("mf_users", admin["id"], admin)
    r = client.post("/api/admin/named-credentials", headers=h,
                    json={"name": "NC-Secret", "url": "https://example.com",
                          "auth_type": "basic", "username": "u",
                          "secret": "s3cr3t-value"})
    assert r.status_code == 201, r.get_json()
    return admin, h


def _assert_sanitized(db_path):
    con = sqlite3.connect(db_path)
    try:
        assert con.execute("SELECT COUNT(*) FROM mf_sessions").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM mf_api_keys").fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM mf_refresh_tokens").fetchone()[0] == 0
        users = con.execute("SELECT id, definition FROM mf_users").fetchall()
        assert users, "sandbox must still have users"
        for _uid, definition in users:
            u = json.loads(definition)
            assert u["password_hash"].startswith("!"), u["password_hash"]
            assert not verify_password("forcelet", u["password_hash"])
            assert not verify_password("UserPass1!", u["password_hash"])
            assert "totp_secret" not in u
            assert u.get("must_change_password") is True
        creds = con.execute(
            "SELECT id, definition FROM mf_named_credentials").fetchall()
        assert creds, "sandbox must still have the named credential"
        for _rid, definition in creds:
            c = json.loads(definition)
            assert "secret_enc" not in c and "secret" not in c
            assert c.get("name") == "NC-Secret"  # name/endpoint kept
            assert c.get("url") == "https://example.com"
    finally:
        con.close()


def test_sandbox_full_copy_is_sanitized(client, app, tmp_path, monkeypatch):
    monkeypatch.setenv("FORCELET_SANDBOX_DIR", str(tmp_path / "sbx"))
    admin, _h = _seed_credentials(client, app)
    store = app.mf_store
    store.insert("Account", {"Name": "Keep Me"})
    store._commit()

    sb = devops.create_sandbox(store, admin, "sani-full", kind="full")
    _assert_sanitized(sb["db_path"])

    # Business data survives sanitization.
    con = sqlite3.connect(sb["db_path"])
    n = con.execute('SELECT COUNT(*) FROM "sobj_Account"').fetchone()[0]
    con.close()
    assert n >= 1


def test_sandbox_developer_and_refresh_are_sanitized(client, app, tmp_path,
                                                    monkeypatch):
    monkeypatch.setenv("FORCELET_SANDBOX_DIR", str(tmp_path / "sbx"))
    admin, _h = _seed_credentials(client, app)
    store = app.mf_store

    # Developer sandboxes keep config tables too -> must be sanitized as well.
    sb = devops.create_sandbox(store, admin, "sani-dev", kind="developer")
    _assert_sanitized(sb["db_path"])

    # Refresh re-copies production -> sanitization must run again.
    sb = devops.refresh_sandbox(store, sb["id"])
    _assert_sanitized(sb["db_path"])


def test_sandbox_no_sanitize_escape_hatch(client, app, tmp_path, monkeypatch):
    monkeypatch.setenv("FORCELET_SANDBOX_DIR", str(tmp_path / "sbx"))
    admin, h_seed = _seed_credentials(client, app)
    store = app.mf_store

    sb = devops.create_sandbox(store, admin, "raw-copy", kind="full",
                               sanitize=False)
    con = sqlite3.connect(sb["db_path"])
    try:
        # Escape hatch honored: credentials survive.
        assert con.execute("SELECT COUNT(*) FROM mf_sessions").fetchone()[0] >= 1
        assert con.execute("SELECT COUNT(*) FROM mf_api_keys").fetchone()[0] >= 1
        row = con.execute(
            "SELECT definition FROM mf_users WHERE id=?",
            (admin["id"],)).fetchone()
        assert not json.loads(row[0])["password_hash"].startswith("!")
    finally:
        con.close()

    # And via the API (default True sanitizes).
    r = client.post("/api/admin/sandboxes", headers=h_seed,
                    json={"name": "api-sani", "kind": "full"})
    assert r.status_code == 201, r.get_json()
    _assert_sanitized(r.get_json()["db_path"])
