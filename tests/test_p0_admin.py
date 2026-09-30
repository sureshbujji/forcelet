"""Tests for the P0 admin-experience gaps: profile detail/update API, user
lifecycle (edit/deactivate/reset-password/unlock/delete), admin settings
blobs (org/security/portal/chatter), login history, storage dashboard,
TOTP enrollment when 2FA is required, and settings-driven lockout/policy/
expiry behavior."""
import os
import tempfile

import pytest

from forcelet.api import create_app
from forcelet import security as _security
from forcelet import totp_util
from helpers import login


@pytest.fixture(autouse=True)
def _clean_login_attempts():
    """Isolate brute-force tracking: the attempt store is module-global, so
    failed logins from other test files in the same process would otherwise
    trip the IP-wide lockout rule inside this file's lockout tests."""
    _security._LOGIN_ATTEMPTS.clear()
    yield
    _security._LOGIN_ATTEMPTS.clear()


@pytest.fixture()
def client():
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_app(db)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    os.unlink(db)


def make_user(client, h, username, password="UserPass1!", profile="Standard User",
              **kw):
    body = {"username": username, "name": username.replace("_", " ").title(),
            "email": f"{username}@example.com", "profile": profile,
            "password": password}
    body.update(kw)
    r = client.post("/api/admin/users", headers=h, json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def login_as(client, username, password):
    """Log in, completing the forced password change; returns (headers, body)."""
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    if body.get("must_change_password"):
        hh = {"Authorization": "Bearer " + body["token"]}
        r2 = client.post("/api/change-password", headers=hh,
                         json={"current": password, "new": "ChangedPass1!"})
        assert r2.status_code == 200, r2.get_json()
        r = client.post("/api/login",
                        json={"username": username, "password": "ChangedPass1!"})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert not body.get("must_change_password")
    return {"Authorization": "Bearer " + body["token"]}, body


def raw_login(client, username, password):
    r = client.post("/api/login", json={"username": username, "password": password})
    return r.status_code, r.get_json()


# ------------------------------------------------------------- profiles
def test_profile_get_one(client):
    h = login(client)
    r = client.get("/api/admin/profiles/Standard%20User", headers=h)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["name"] == "Standard User"
    assert isinstance(body["object_permissions"], dict)


def test_profile_get_unknown(client):
    h = login(client)
    r = client.get("/api/admin/profiles/Nope", headers=h)
    assert r.status_code == 404


def test_profile_update_object_permissions(client):
    h = login(client)
    perms = {"Account": {"create": True, "read": True, "edit": False, "delete": False}}
    r = client.put("/api/admin/profiles/Standard%20User", headers=h,
                   json={"object_permissions": perms})
    assert r.status_code == 200, r.get_json()
    r = client.get("/api/admin/profiles/Standard%20User", headers=h)
    assert r.get_json()["object_permissions"]["Account"]["read"] is True
    assert r.get_json()["object_permissions"]["Account"]["delete"] is False


def test_profile_update_unknown_object_rejected(client):
    h = login(client)
    r = client.put("/api/admin/profiles/Standard%20User", headers=h,
                   json={"object_permissions": {"Nope__c": {"read": True}}})
    assert r.status_code == 422, r.get_json()


def test_profile_update_unknown_field_rejected(client):
    h = login(client)
    r = client.put("/api/admin/profiles/Standard%20User", headers=h,
                   json={"field_permissions": {"Lead": {"Nope__c": {"read": True}}}})
    assert r.status_code == 422, r.get_json()


def test_profile_update_rejects_rename(client):
    h = login(client)
    r = client.put("/api/admin/profiles/Standard%20User", headers=h,
                   json={"name": "Renamed"})
    assert r.status_code == 422


def test_profile_update_forbidden_for_nonadmin(client):
    h = login(client)
    make_user(client, h, "stduser1")
    uh, _ = login_as(client, "stduser1", "UserPass1!")
    r = client.put("/api/admin/profiles/Standard%20User", headers=uh,
                   json={"object_permissions": {}})
    assert r.status_code == 403


# ------------------------------------------------------- user lifecycle
def test_create_user_with_email(client):
    h = login(client)
    u = make_user(client, h, "emailuser", password="EmailPass1!")
    assert u["email"] == "emailuser@example.com"
    assert "password_hash" not in u
    assert u["is_active"] is True


def test_create_user_rejects_weak_password(client):
    h = login(client)
    r = client.post("/api/admin/users", headers=h,
                    json={"username": "weakuser", "name": "Weak",
                          "profile": "Standard User", "password": "short"})
    assert r.status_code == 422, r.get_json()


def test_create_user_rejects_password_equal_to_username(client):
    h = login(client)
    r = client.post("/api/admin/users", headers=h,
                    json={"username": "samepass1", "name": "Same",
                          "profile": "Standard User", "password": "samepass1"})
    assert r.status_code == 422, r.get_json()


def test_update_user(client):
    h = login(client)
    u = make_user(client, h, "edituser")
    r = client.put(f"/api/admin/users/{u['id']}", headers=h,
                   json={"name": "Edited Name", "email": "edited@example.com",
                         "profile": "Read Only"})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["name"] == "Edited Name"
    assert body["email"] == "edited@example.com"
    assert body["profile"] == "Read Only"


def test_update_user_unknown_profile_rejected(client):
    h = login(client)
    u = make_user(client, h, "edituser2")
    r = client.put(f"/api/admin/users/{u['id']}", headers=h,
                   json={"profile": "Nope"})
    assert r.status_code == 422


def test_deactivate_blocks_login_and_kills_sessions(client):
    h = login(client)
    u = make_user(client, h, "deactuser")
    uh, _ = login_as(client, "deactuser", "UserPass1!")
    # deactivation takes effect immediately
    r = client.put(f"/api/admin/users/{u['id']}", headers=h, json={"is_active": False})
    assert r.status_code == 200
    r = client.get("/api/objects", headers=uh)
    assert r.status_code == 401
    status, _ = raw_login(client, "deactuser", "ChangedPass1!")
    assert status == 403
    # reactivation restores access
    r = client.put(f"/api/admin/users/{u['id']}", headers=h, json={"is_active": True})
    assert r.status_code == 200
    status, _ = raw_login(client, "deactuser", "ChangedPass1!")
    assert status == 200


def test_cannot_deactivate_self(client):
    h = login(client)
    me = client.get("/api/me", headers=h).get_json()
    r = client.put(f"/api/admin/users/{me['id']}", headers=h, json={"is_active": False})
    assert r.status_code == 422


def test_reset_password_flow(client):
    h = login(client)
    u = make_user(client, h, "resetuser")
    uh, _ = login_as(client, "resetuser", "UserPass1!")
    r = client.post(f"/api/admin/users/{u['id']}/reset-password", headers=h)
    assert r.status_code == 200, r.get_json()
    temp = r.get_json()["temporary_password"]
    assert temp and len(temp) >= 12
    # old session is revoked
    r = client.get("/api/objects", headers=uh)
    assert r.status_code == 401
    # temporary password works but forces a change
    status, body = raw_login(client, "resetuser", temp)
    assert status == 200
    assert body.get("must_change_password") is True


def test_cannot_reset_own_password(client):
    h = login(client)
    me = client.get("/api/me", headers=h).get_json()
    r = client.post(f"/api/admin/users/{me['id']}/reset-password", headers=h)
    assert r.status_code == 422


def test_unlock_after_lockout(client):
    h = login(client)
    r = client.put("/api/admin/settings/security", headers=h,
                   json={"lockout_threshold": 2, "lockout_window_minutes": 15,
                         "lockout_duration_minutes": 15})
    assert r.status_code == 200
    make_user(client, h, "lockuser", password="LockPass1!")
    for _ in range(2):
        status, _ = raw_login(client, "lockuser", "wrong-password")
        assert status == 401
    status, body = raw_login(client, "lockuser", "LockPass1!")
    assert status == 429, body  # locked out even with the right password
    u = client.application.mf_security.get_user_by_username("lockuser")
    r = client.post(f"/api/admin/users/{u['id']}/unlock", headers=h)
    assert r.status_code == 200
    status, _ = raw_login(client, "lockuser", "LockPass1!")
    assert status == 200


def test_cannot_unlock_self(client):
    h = login(client)
    me = client.get("/api/me", headers=h).get_json()
    r = client.post(f"/api/admin/users/{me['id']}/unlock", headers=h)
    assert r.status_code == 422


def test_delete_user_never_logged_in(client):
    h = login(client)
    u = make_user(client, h, "deluser")
    r = client.delete(f"/api/admin/users/{u['id']}", headers=h)
    assert r.status_code == 200, r.get_json()
    remaining = client.get("/api/admin/users", headers=h).get_json()
    assert all(x["id"] != u["id"] for x in remaining)
    status, _ = raw_login(client, "deluser", "UserPass1!")
    assert status == 401


def test_delete_user_with_login_history_rejected(client):
    h = login(client)
    u = make_user(client, h, "deluser2")
    login_as(client, "deluser2", "UserPass1!")
    r = client.delete(f"/api/admin/users/{u['id']}", headers=h)
    assert r.status_code == 422, r.get_json()
    # deactivation is the supported path instead
    r = client.put(f"/api/admin/users/{u['id']}", headers=h, json={"is_active": False})
    assert r.status_code == 200


def test_cannot_delete_self(client):
    h = login(client)
    me = client.get("/api/me", headers=h).get_json()
    r = client.delete(f"/api/admin/users/{me['id']}", headers=h)
    assert r.status_code == 422


def test_user_admin_endpoints_forbidden_for_nonadmin(client):
    h = login(client)
    make_user(client, h, "stduser2")
    uh, _ = login_as(client, "stduser2", "UserPass1!")
    assert client.get("/api/admin/users", headers=uh).status_code == 403
    assert client.get("/api/admin/login-history", headers=uh).status_code == 403
    assert client.get("/api/admin/storage", headers=uh).status_code == 403


# --------------------------------------------------------------- settings
def test_security_settings_defaults(client):
    h = login(client)
    r = client.get("/api/admin/settings/security", headers=h)
    assert r.status_code == 200
    d = r.get_json()
    assert d["password_min_length"] == 8
    assert d["lockout_threshold"] == 5
    assert d["session_timeout_minutes"] == 720
    assert d["session_max_hours"] == 168
    assert d["totp_required"] == "none"
    assert d["password_expiry_days"] == 0


def test_security_settings_update_and_validation(client):
    h = login(client)
    r = client.put("/api/admin/settings/security", headers=h,
                   json={"password_min_length": 12, "lockout_threshold": 3,
                         "password_require_digit": True})
    assert r.status_code == 200, r.get_json()
    r = client.get("/api/admin/settings/security", headers=h)
    d = r.get_json()
    assert d["password_min_length"] == 12
    assert d["lockout_threshold"] == 3
    assert d["password_require_digit"] is True
    # invalid values are rejected
    assert client.put("/api/admin/settings/security", headers=h,
                      json={"password_min_length": 0}).status_code == 422
    assert client.put("/api/admin/settings/security", headers=h,
                      json={"bogus_key": 1}).status_code == 422
    assert client.put("/api/admin/settings/security", headers=h,
                      json={"totp_required": "sometimes"}).status_code == 422
    assert client.put("/api/admin/settings/security", headers=h,
                      json={"totp_required": "profiles"}).status_code == 422  # needs profiles


def test_settings_unknown_area_404(client):
    h = login(client)
    assert client.get("/api/admin/settings/nope", headers=h).status_code == 404
    assert client.put("/api/admin/settings/nope", headers=h, json={}).status_code == 404


def test_settings_forbidden_for_nonadmin(client):
    h = login(client)
    make_user(client, h, "stduser3")
    uh, _ = login_as(client, "stduser3", "UserPass1!")
    assert client.get("/api/admin/settings/security", headers=uh).status_code == 403
    assert client.put("/api/admin/settings/org", headers=uh,
                      json={"org_name": "X"}).status_code == 403


def test_org_settings_roundtrip(client):
    h = login(client)
    r = client.put("/api/admin/settings/org", headers=h,
                   json={"org_name": "Acme Corp", "default_currency": "EUR",
                         "fiscal_year_start_month": 7})
    assert r.status_code == 200, r.get_json()
    d = client.get("/api/admin/settings/org", headers=h).get_json()
    assert d["org_name"] == "Acme Corp"
    assert d["default_currency"] == "EUR"
    assert d["fiscal_year_start_month"] == 7
    assert client.put("/api/admin/settings/org", headers=h,
                      json={"fiscal_year_start_month": 13}).status_code == 422


def test_password_policy_enforced_from_settings(client):
    h = login(client)
    r = client.put("/api/admin/settings/security", headers=h,
                   json={"password_min_length": 12})
    assert r.status_code == 200
    r = client.post("/api/admin/users", headers=h,
                    json={"username": "poluser", "name": "Pol",
                          "profile": "Standard User", "password": "short1"})
    assert r.status_code == 422, r.get_json()
    u = make_user(client, h, "poluser2", password="LongEnoughPass1!")
    uh, _ = login_as(client, "poluser2", "LongEnoughPass1!")
    r = client.post("/api/change-password", headers=uh,
                    json={"current": "ChangedPass1!", "new": "short"})
    assert r.status_code == 422, r.get_json()


def test_password_expiry_forces_change(client):
    h = login(client)
    r = client.put("/api/admin/settings/security", headers=h,
                   json={"password_expiry_days": 30})
    assert r.status_code == 200
    u = make_user(client, h, "expuser", password="ExpiryPass1!")
    login_as(client, "expuser", "ExpiryPass1!")
    # age the password beyond the expiry window
    store = client.application.mf_store
    sec = client.application.mf_security
    fresh = sec.get_user_by_username("expuser")
    fresh["password_set_at"] = "2020-01-01T00:00:00+00:00"
    store.meta_put("mf_users", fresh["id"], fresh)
    status, body = raw_login(client, "expuser", "ChangedPass1!")
    assert status == 200
    assert body.get("must_change_password") is True


def test_session_timeouts_follow_settings(client):
    from forcelet.security import session_timeouts
    h = login(client)
    store = client.application.mf_store
    ttl, max_age = session_timeouts(store)
    assert (ttl, max_age) == (720 * 60, 168 * 3600)
    r = client.put("/api/admin/settings/security", headers=h,
                   json={"session_timeout_minutes": 30, "session_max_hours": 48})
    assert r.status_code == 200
    assert session_timeouts(store) == (30 * 60, 48 * 3600)


# ----------------------------------------------------------- login history
def test_login_history_records_and_filters(client):
    h = login(client)
    raw_login(client, "admin", "wrong-password")
    raw_login(client, "nosuchuser", "whatever")
    rows = client.get("/api/admin/login-history", headers=h).get_json()
    assert rows["total"] >= 3
    assert {r["username"] for r in rows["rows"]} >= {"admin", "nosuchuser"}
    sample = rows["rows"][0]
    assert {"username", "success", "at", "ip"} <= set(sample)
    failed = client.get("/api/admin/login-history?success=0", headers=h).get_json()
    assert failed["total"] >= 2
    assert all(not r["success"] for r in failed["rows"])
    ok = client.get("/api/admin/login-history?success=1", headers=h).get_json()
    assert ok["total"] >= 1
    assert all(r["success"] for r in ok["rows"])
    only = client.get("/api/admin/login-history?username=nosuchuser", headers=h).get_json()
    assert only["total"] >= 1
    assert all(r["username"] == "nosuchuser" for r in only["rows"])
    paged = client.get("/api/admin/login-history?limit=1&offset=0", headers=h).get_json()
    assert len(paged["rows"]) == 1
    assert paged["limit"] == 1


# ---------------------------------------------------------------- storage
def test_storage_dashboard(client):
    h = login(client)
    r = client.get("/api/admin/storage", headers=h)
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert isinstance(d["objects"], list) and d["objects"]
    assert d["database_bytes"] > 0
    assert d["total_records"] >= 0
    assert isinstance(d["backups"], list)
    names = [o["object"] for o in d["objects"]]
    assert "Account" in names


# ------------------------------------------------------- TOTP enrollment
def test_totp_required_all_enrollment_flow(client):
    h = login(client)
    r = client.put("/api/admin/settings/security", headers=h,
                   json={"totp_required": "all"})
    assert r.status_code == 200
    make_user(client, h, "totpuser", password="TotpPass1!")
    # login: must change password AND enroll in 2FA -> limited session
    status, body = raw_login(client, "totpuser", "TotpPass1!")
    assert status == 200
    assert body.get("must_change_password") is True
    assert body.get("totp_setup_required") is True
    limited = {"Authorization": "Bearer " + body["token"]}
    # limited session cannot use the app yet
    r = client.get("/api/objects", headers=limited)
    assert r.status_code == 403
    # forced password change keeps the session limited (2FA still pending)
    r = client.post("/api/change-password", headers=limited,
                    json={"current": "TotpPass1!", "new": "TotpPass2!"})
    assert r.status_code == 200
    assert r.get_json()["totp_setup_required"] is True
    assert client.get("/api/objects", headers=limited).status_code == 403
    # enrollment via the limited session
    r = client.post("/api/me/totp/setup", headers=limited)
    assert r.status_code == 200, r.get_json()
    secret = r.get_json()["secret"]
    assert secret
    r = client.post("/api/me/totp/enable", headers=limited, json={"code": "000000"})
    assert r.status_code == 422
    r = client.post("/api/me/totp/enable", headers=limited,
                    json={"code": totp_util.current_code(secret)})
    assert r.status_code == 200, r.get_json()
    # session is now fully trusted
    assert client.get("/api/objects", headers=limited).status_code == 200
    # and the next login demands a TOTP code (challenge flow)
    status, body = raw_login(client, "totpuser", "TotpPass2!")
    assert status == 200
    assert body.get("totp_required") is True


def test_totp_required_profiles_mode(client):
    h = login(client)
    r = client.put("/api/admin/settings/security", headers=h,
                   json={"totp_required": "profiles",
                         "totp_required_profiles": ["Standard User"]})
    assert r.status_code == 200, r.get_json()
    make_user(client, h, "totpstd", password="TotpPass1!", profile="Standard User")
    make_user(client, h, "totpro", password="TotpPass1!", profile="Read Only")
    _, std_body = raw_login(client, "totpstd", "TotpPass1!")
    assert std_body.get("totp_setup_required") is True
    _, ro_body = raw_login(client, "totpro", "TotpPass1!")
    assert ro_body.get("totp_setup_required") is not True


# ------------------------------------------------------------------ portal
def test_portal_settings_roundtrip_and_disable(client):
    h = login(client)
    r = client.put("/api/admin/settings/portal", headers=h,
                   json={"enabled": True, "title": "Acme Portal",
                         "welcome_message": "Welcome!"})
    assert r.status_code == 200, r.get_json()
    pub = client.get("/api/portal/settings").get_json()
    assert pub["title"] == "Acme Portal"
    assert pub["welcome_message"] == "Welcome!"
    assert pub["enabled"] is True
    # disabling blocks portal auth but the public settings stay readable
    r = client.put("/api/admin/settings/portal", headers=h, json={"enabled": False})
    assert r.status_code == 200
    assert client.get("/api/portal/settings").get_json()["enabled"] is False
    r = client.post("/api/portal/login", json={"username": "x", "password": "y"})
    assert r.status_code == 503


# ------------------------------------------------------------------ chatter
def test_chatter_disable_blocks_feed(client):
    h = login(client)
    r = client.put("/api/admin/settings/chatter", headers=h,
                   json={"feed_enabled": False})
    assert r.status_code == 200, r.get_json()
    r = client.get("/api/feed", headers=h)
    assert r.status_code == 503
    assert "disabled" in r.get_json()["error"].lower()
    r = client.post("/api/feed", headers=h, json={"body": "hello"})
    assert r.status_code == 503


def test_mentions_toggle_controls_notifications(client):
    h = login(client)
    make_user(client, h, "mentioner", password="MentionPass1!")
    uh, _ = login_as(client, "mentioner", "MentionPass1!")

    def mention_count():
        notifs = client.get("/api/notifications", headers=h).get_json()
        return sum(1 for n in notifs if n.get("ntype") == "mention")

    before = mention_count()
    r = client.post("/api/feed", headers=uh, json={"body": "Hello @admin, see this"})
    assert r.status_code == 201, r.get_json()
    assert mention_count() == before + 1

    r = client.put("/api/admin/settings/chatter", headers=h,
                   json={"mentions_enabled": False})
    assert r.status_code == 200
    r = client.post("/api/feed", headers=uh, json={"body": "Hello again @admin"})
    assert r.status_code == 201, r.get_json()
    assert mention_count() == before + 1  # no new notification
