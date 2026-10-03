"""Tests for the admin-domain fixes (issues 1-13 of the admin audit).

Covers: delegated-admin profile escalation guards, deactivated API keys,
TOTP challenge rate limiting, Setup tile/card alignment, change-set audit
entries, sandbox delete path safety, backup restore, profile DELETE,
change-set DELETE, org-wide default record access (OWD), permission-set
structural validation, and referential integrity on generic config deletes.
"""
import os
import re
import tempfile

import pytest

from forcelet import changesets as _changesets
from forcelet import devops as _devops
from forcelet.api import create_app
from forcelet.api import _shared as _shared
from helpers import login


@pytest.fixture(autouse=True)
def _clean_rate_buckets():
    _shared._RATE_BUCKETS.clear()
    yield
    _shared._RATE_BUCKETS.clear()


@pytest.fixture()
def app():
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_app(db)
    app.config["TESTING"] = True
    yield app
    for p in (db, db + "-wal", db + "-shm"):
        try:
            os.unlink(p)
        except OSError:
            pass  # already removed (e.g. basetemp cleanup); not a failure


@pytest.fixture()
def client(app):
    with app.test_client() as c:
        yield c


def make_user(client, h, username, password="UserPass1!",
              profile="Standard User", **kw):
    body = {"username": username, "name": username.replace("_", " ").title(),
            "email": f"{username}@example.com", "profile": profile,
            "password": password}
    body.update(kw)
    r = client.post("/api/admin/users", headers=h, json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def login_as(client, username, password="UserPass1!"):
    r = client.post("/api/login",
                    json={"username": username, "password": password})
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
    return {"Authorization": "Bearer " + body["token"]}


def make_delegated_user_admin(client, h, username, scope="users",
                              roles=("Support Agent",)):
    """Create a user and grant them a role-restricted delegated admin scope."""
    mgr = make_user(client, h, username)
    r = client.post("/api/admin/delegated-groups", headers=h,
                    json={"name": f"{username} group", "members": [mgr["id"]],
                          "scopes": [{"scope": scope, "roles": list(roles)}]})
    assert r.status_code == 201, r.get_json()
    return mgr


# ------------------------------------------------------------- fix 1: profile escalation on create
def test_delegated_admin_cannot_create_sysadmin(client):
    h = login(client)
    make_user(client, h, "agent_f1", role="Support Agent")
    make_delegated_user_admin(client, h, "mgr_f1")
    mh = login_as(client, "mgr_f1")
    r = client.post("/api/admin/users", headers=mh,
                    json={"username": "eviladmin", "name": "Evil",
                          "email": "evil@example.com",
                          "profile": "System Administrator",
                          "password": "UserPass1!", "role": "Support Agent"})
    assert r.status_code == 403, r.get_json()
    # In-scope non-admin profiles still work for the delegated admin.
    r = client.post("/api/admin/users", headers=mh,
                    json={"username": "okagent", "name": "Ok",
                          "email": "ok@example.com", "profile": "Standard User",
                          "password": "UserPass1!", "role": "Support Agent"})
    assert r.status_code == 201, r.get_json()
    # Full admins are unaffected.
    r = client.post("/api/admin/users", headers=h,
                    json={"username": "realadmin", "name": "Real",
                          "email": "real@example.com",
                          "profile": "System Administrator",
                          "password": "UserPass1!"})
    assert r.status_code == 201, r.get_json()


def test_create_user_unknown_profile_rejected(client):
    h = login(client)
    r = client.post("/api/admin/users", headers=h,
                    json={"username": "ghost", "name": "Ghost",
                          "email": "ghost@example.com", "profile": "Nope",
                          "password": "UserPass1!"})
    assert r.status_code == 422, r.get_json()


# ------------------------------------------------------------- fix 2: role retargeting on update
def test_delegated_admin_cannot_hijack_via_role_retarget(client):
    h = login(client)
    agent = make_user(client, h, "agent_f2", role="Support Agent")
    boss = make_user(client, h, "boss_f2", role="VP Sales")
    make_delegated_user_admin(client, h, "mgr_f2")
    mh = login_as(client, "mgr_f2")
    # Retargeting an out-of-scope user into scope is still blocked: the
    # check runs against the target's CURRENT role.
    r = client.put(f"/api/admin/users/{boss['id']}", headers=mh,
                   json={"role": "Support Agent"})
    assert r.status_code == 403, r.get_json()
    # Moving an in-scope user to an out-of-scope role is blocked too.
    r = client.put(f"/api/admin/users/{agent['id']}", headers=mh,
                   json={"role": "VP Sales"})
    assert r.status_code == 403, r.get_json()
    # Non-role edits on in-scope users still work.
    r = client.put(f"/api/admin/users/{agent['id']}", headers=mh,
                   json={"name": "Agent Renamed"})
    assert r.status_code == 200, r.get_json()
    # Profile escalation via update is blocked for delegated admins.
    r = client.put(f"/api/admin/users/{agent['id']}", headers=mh,
                   json={"profile": "System Administrator"})
    assert r.status_code == 403, r.get_json()
    # Full admin can still change profiles.
    r = client.put(f"/api/admin/users/{agent['id']}", headers=h,
                   json={"profile": "Read Only"})
    assert r.status_code == 200, r.get_json()


# ------------------------------------------------------------- fix 3: deactivated API keys
def test_deactivated_user_api_key_denied(client):
    h = login(client)
    make_user(client, h, "keyuser")
    kh = login_as(client, "keyuser")
    r = client.post("/api/api-keys", headers=kh, json={"name": "k1"})
    assert r.status_code == 201, r.get_json()
    key = r.get_json()["key"]
    keyh = {"Authorization": "Bearer " + key}
    r = client.get("/api/me", headers=keyh)
    assert r.status_code == 200, r.get_json()
    # Deactivate the user: the API key must stop working immediately.
    users = client.get("/api/admin/users", headers=h).get_json()
    uid = next(u["id"] for u in users if u["username"] == "keyuser")
    r = client.put(f"/api/admin/users/{uid}", headers=h,
                   json={"is_active": False})
    assert r.status_code == 200, r.get_json()
    r = client.get("/api/me", headers=keyh)
    assert r.status_code == 401, r.get_json()
    # Reactivation restores the key (no revocation needed beyond the check).
    r = client.put(f"/api/admin/users/{uid}", headers=h,
                   json={"is_active": True})
    assert r.status_code == 200, r.get_json()
    r = client.get("/api/me", headers=keyh)
    assert r.status_code == 200, r.get_json()


# ------------------------------------------------------------- fix 4: TOTP challenge rate limit
def test_totp_challenge_rate_limited():
    # Rate limiting is bypassed under TESTING, so run this app unflagged.
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_app(db)
    try:
        with app.test_client() as c:
            codes = [c.post("/api/login/totp",
                            json={"challenge": "bogus",
                                  "code": "000000"}).status_code
                     for _ in range(11)]
        assert 401 in codes
        assert codes[-1] == 429, codes
    finally:
        os.unlink(db)
        for ext in ("-wal", "-shm"):
            try:
                os.unlink(db + ext)
            except OSError:
                pass


# ------------------------------------------------------------- fix 5: Setup tile/card alignment
def _index_html():
    return open(os.path.join(os.path.dirname(__file__), "..", "web",
                             "index.html"), encoding="utf-8").read()


def test_setup_tiles_have_cards():
    html = _index_html()
    tiles = re.findall(r'\{section:"([^"]+)",card:"((?:[^"\\]|\\.)+)"', html)
    assert tiles, "no SETUP_TILES found"
    csm = re.search(r"const CARD_SECTION=\{(.*?)\};", html, re.S).group(1)
    card_section = dict(re.findall(r'"((?:[^"\\]|\\.)+)":"([^"]+)"', csm))
    for section, card in tiles:
        assert card in card_section, f"tile {card!r} missing from CARD_SECTION"
        assert card_section[card] == section, (
            f"tile {card!r}: tile section {section} != "
            f"CARD_SECTION {card_section[card]}")


def test_dead_tiles_fixed():
    html = _index_html()
    csm = re.search(r"const CARD_SECTION=\{(.*?)\};", html, re.S).group(1)
    card_section = dict(re.findall(r'"((?:[^"\\]|\\.)+)":"([^"]+)"', csm))
    for card, section in (("Stored roll-up rules", "datamodel"),
                          ("Lead field mappings", "automation"),
                          ("Web-to forms", "automation"),
                          ("SLA policy", "automation"),
                          ("Test data seeding", "devops")):
        assert card_section.get(card) == section, card
    assert "Case SLA policy" not in card_section  # stale key removed


# ------------------------------------------------------------- fix 6: change-set audit entries
def _audit_rows(client, h, action):
    r = client.get(f"/api/admin/audit-trail?action={action}&entity=change-set",
                   headers=h)
    assert r.status_code == 200, r.get_json()
    return r.get_json().get("rows", [])


def test_changeset_mutations_audited(client):
    h = login(client)
    r = client.post("/api/admin/change-sets", headers=h,
                    json={"name": "Audit CS"})
    assert r.status_code == 201, r.get_json()
    cs = r.get_json()
    assert _audit_rows(client, h, "create"), "create not audited"
    r = client.post(f"/api/admin/change-sets/{cs['id']}/components", headers=h,
                    json={"action": "add", "type": "validation_rule",
                          "ref": "v1"})
    assert r.status_code == 200, r.get_json()
    r = client.post(f"/api/admin/change-sets/{cs['id']}/components", headers=h,
                    json={"action": "remove", "type": "validation_rule",
                          "ref": "v1"})
    assert r.status_code == 200, r.get_json()
    assert _audit_rows(client, h, "add"), "add not audited"
    assert _audit_rows(client, h, "remove"), "remove not audited"
    r = client.post(f"/api/admin/change-sets/{cs['id']}/status", headers=h,
                    json={"status": "Outbound"})
    assert r.status_code == 200, r.get_json()
    assert _audit_rows(client, h, "status-change"), "status change not audited"
    doc = {"changeset_version": 1,
           "changeset": {"name": "Uploaded CS", "description": "",
                         "components": []},
           "package": None}
    r = client.post("/api/admin/change-sets/upload", headers=h,
                    json={"changeset": doc})
    assert r.status_code == 200, r.get_json()
    assert _audit_rows(client, h, "upload"), "upload not audited"
    r = client.post(f"/api/admin/change-sets/{cs['id']}/validate", headers=h)
    assert r.status_code == 200, r.get_json()
    assert _audit_rows(client, h, "validate"), "validate not audited"


# ------------------------------------------------------------- fix 7: sandbox delete path
def test_sandbox_delete_rejects_sibling_dir(app, tmp_path, monkeypatch):
    monkeypatch.setenv("FORCELET_SANDBOX_DIR", str(tmp_path / "sandboxes"))
    store = app.mf_store
    # A sibling directory that merely *contains* the root path as a
    # substring must never be removed.
    evil = tmp_path / "sandboxes_evil"
    evil.mkdir()
    (evil / "x.db").write_text("x")
    store.config_put(_devops.SANDBOX_TABLE,
                     {"id": "sb_evil", "name": "evil",
                      "db_path": str(evil / "x.db"), "status": "active"})
    assert _devops.delete_sandbox(store, "sb_evil") is True
    assert evil.exists() and (evil / "x.db").exists()
    # A real sandbox directory inside the root is still removed.
    real = tmp_path / "sandboxes" / "sb1"
    real.mkdir(parents=True)
    (real / "x.db").write_text("x")
    store.config_put(_devops.SANDBOX_TABLE,
                     {"id": "sb_real", "name": "real",
                      "db_path": str(real / "x.db"), "status": "active"})
    assert _devops.delete_sandbox(store, "sb_real") is True
    assert not real.exists()


# ------------------------------------------------------------- fix 8: backup restore
def test_backup_restore_requires_confirm(client, monkeypatch, tmp_path):
    monkeypatch.setenv("FORCELET_BACKUP_DIR", str(tmp_path))
    h = login(client)
    r = client.post("/api/admin/backups", headers=h)
    assert r.status_code == 201, r.get_json()
    name = r.get_json()["backup"]
    r = client.post(f"/api/admin/backups/{name}/restore", headers=h,
                    json={})
    assert r.status_code == 422, r.get_json()
    r = client.post(f"/api/admin/backups/{name}/restore", headers=h,
                    json={"confirm": True})
    assert r.status_code == 200, r.get_json()


def test_backup_restore_roundtrip(client, monkeypatch, tmp_path):
    monkeypatch.setenv("FORCELET_BACKUP_DIR", str(tmp_path))
    h = login(client)
    r = client.post("/api/admin/backups", headers=h)
    assert r.status_code == 201, r.get_json()
    baseline = r.get_json()["backup"]
    make_user(client, h, "restorevictim")
    users = client.get("/api/admin/users", headers=h).get_json()
    assert any(u["username"] == "restorevictim" for u in users)
    r = client.post(f"/api/admin/backups/{baseline}/restore", headers=h,
                    json={"confirm": True})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["restored"] == baseline
    snap = body["safety_snapshot"]
    assert snap != baseline and os.path.isfile(os.path.join(str(tmp_path),
                                                            snap))
    users = client.get("/api/admin/users", headers=h).get_json()
    assert not any(u["username"] == "restorevictim" for u in users)


def test_backup_restore_bad_id(client, monkeypatch, tmp_path):
    monkeypatch.setenv("FORCELET_BACKUP_DIR", str(tmp_path))
    h = login(client)
    r = client.post("/api/admin/backups/forcelet-20000101-000000.db/restore",
                    headers=h, json={"confirm": True})
    assert r.status_code == 404, r.get_json()
    r = client.post("/api/admin/backups/nope.db/restore", headers=h,
                    json={"confirm": True})
    assert r.status_code == 404, r.get_json()


# ------------------------------------------------------------- fix 9: profile DELETE
def test_profile_delete(client):
    h = login(client)
    r = client.post("/api/admin/profiles", headers=h,
                    json={"name": "Temp Profile", "object_permissions": {},
                          "field_permissions": {}})
    assert r.status_code == 201, r.get_json()
    r = client.delete("/api/admin/profiles/Temp Profile", headers=h)
    assert r.status_code == 200, r.get_json()
    r = client.get("/api/admin/profiles/Temp Profile", headers=h)
    assert r.status_code == 404, r.get_json()
    # Built-in profile is protected.
    r = client.delete("/api/admin/profiles/System Administrator", headers=h)
    assert r.status_code == 409, r.get_json()
    # Unknown profile.
    r = client.delete("/api/admin/profiles/Nope", headers=h)
    assert r.status_code == 404, r.get_json()


def test_profile_delete_blocked_when_assigned(client):
    h = login(client)
    r = client.post("/api/admin/profiles", headers=h,
                    json={"name": "In Use", "object_permissions": {},
                          "field_permissions": {}})
    assert r.status_code == 201, r.get_json()
    make_user(client, h, "profuser", profile="In Use")
    r = client.delete("/api/admin/profiles/In Use", headers=h)
    assert r.status_code == 409, r.get_json()
    assert "profuser" in r.get_json()["error"]


# ------------------------------------------------------------- fix 10: change-set DELETE
def test_changeset_delete(client):
    h = login(client)
    r = client.post("/api/admin/change-sets", headers=h,
                    json={"name": "Deletable CS"})
    cs = r.get_json()
    r = client.delete(f"/api/admin/change-sets/{cs['id']}", headers=h)
    assert r.status_code == 200, r.get_json()
    r = client.get(f"/api/admin/change-sets/{cs['id']}", headers=h)
    assert r.status_code == 404, r.get_json()
    r = client.delete("/api/admin/change-sets/ghost", headers=h)
    assert r.status_code == 404, r.get_json()
    assert _audit_rows(client, h, "delete"), "delete not audited"


def test_changeset_delete_blocked_with_deployments(app, client):
    h = login(client)
    r = client.post("/api/admin/change-sets", headers=h,
                    json={"name": "Deployed CS"})
    cs = r.get_json()
    _changesets._record_deployment(app.mf_store, cs["id"], "outbound",
                                  "Deployed", {}, "log", "admin")
    r = client.delete(f"/api/admin/change-sets/{cs['id']}", headers=h)
    assert r.status_code == 409, r.get_json()
    assert "deployment" in r.get_json()["error"].lower()
    # The change set itself still exists.
    r = client.get(f"/api/admin/change-sets/{cs['id']}", headers=h)
    assert r.status_code == 200, r.get_json()


# ------------------------------------------------------------- fix 11: org-wide default record access
def test_owd_default_is_private(app, client):
    h = login(client)
    store, security = app.mf_store, app.mf_security
    owner = make_user(client, h, "owdowner")
    viewer = make_user(client, h, "owdviewer")
    rid = store.insert("Account", {"Name": "OWD Test",
                                   "owner_id": owner["id"]})
    rec = store.get("Account", rid)
    assert security.can_see_record(viewer, rec, "Account") is False


def test_owd_public_read_only(app, client):
    h = login(client)
    store, security = app.mf_store, app.mf_security
    owner = make_user(client, h, "owdowner2")
    viewer = make_user(client, h, "owdviewer2")
    rid = store.insert("Account", {"Name": "OWD Test 2",
                                   "owner_id": owner["id"]})
    rec = store.get("Account", rid)
    r = client.put("/api/admin/settings/org", headers=h,
                   json={"default_record_access": "public_read_only"})
    assert r.status_code == 200, r.get_json()
    assert security.can_see_record(viewer, rec, "Account") is True
    # Back to private: baseline restored.
    r = client.put("/api/admin/settings/org", headers=h,
                   json={"default_record_access": "private"})
    assert r.status_code == 200, r.get_json()
    assert security.can_see_record(viewer, rec, "Account") is False


def test_owd_rejects_bad_value(client):
    h = login(client)
    r = client.put("/api/admin/settings/org", headers=h,
                   json={"default_record_access": "everyone"})
    assert r.status_code == 422, r.get_json()


# ------------------------------------------------------------- fix 12: permission-set validation
def test_permission_set_rejects_unknown_object(client):
    h = login(client)
    r = client.post("/api/admin/permission-sets", headers=h,
                    json={"name": "Bad PS",
                          "object_permissions": {"Nope": {"read": True}}})
    assert r.status_code == 422, r.get_json()
    assert "Nope" in r.get_json()["error"]


def test_permission_set_rejects_unknown_field(client):
    h = login(client)
    r = client.post("/api/admin/permission-sets", headers=h,
                    json={"name": "Bad PS 2",
                          "field_permissions": {"Lead": {"Nope": {"read": True}}}})
    assert r.status_code == 422, r.get_json()
    assert "Nope" in r.get_json()["error"]


def test_permission_set_valid_still_works(client):
    h = login(client)
    r = client.post("/api/admin/permission-sets", headers=h,
                    json={"name": "Good PS",
                          "object_permissions": {"Lead": {"delete": True}},
                          "field_permissions": {"Lead": {"Email": {"read": True}}}})
    assert r.status_code == 201, r.get_json()
    ps = r.get_json()
    r = client.patch(f"/api/admin/permission-sets/{ps['id']}", headers=h,
                     json={"object_permissions": {"Nope": {"read": True}}})
    assert r.status_code == 422, r.get_json()
    r = client.patch(f"/api/admin/permission-sets/{ps['id']}", headers=h,
                     json={"object_permissions": {"Account": {"read": True}}})
    assert r.status_code == 200, r.get_json()


# ------------------------------------------------------------- fix 13: generic delete referential integrity
def test_queue_delete_blocked_by_routing_config(client):
    h = login(client)
    r = client.post("/api/admin/queues", headers=h,
                    json={"name": "Doomed Queue", "members": []})
    assert r.status_code == 201, r.get_json()
    q = r.get_json()
    r = client.post("/api/admin/routing-configs", headers=h,
                    json={"name": "RC1", "queue_id": q["id"]})
    assert r.status_code == 201, r.get_json()
    rc = r.get_json()
    r = client.delete(f"/api/admin/queues/{q['id']}", headers=h)
    assert r.status_code == 409, r.get_json()
    assert "RC1" in " ".join(r.get_json()["dependents"])
    # Remove the dependent, then the delete succeeds.
    r = client.delete(f"/api/admin/routing-configs/{rc['id']}", headers=h)
    assert r.status_code == 200, r.get_json()
    r = client.delete(f"/api/admin/queues/{q['id']}", headers=h)
    assert r.status_code == 200, r.get_json()


def test_queue_delete_blocked_by_approval_step(client):
    h = login(client)
    r = client.post("/api/admin/queues", headers=h,
                    json={"name": "Approver Queue", "members": []})
    q = r.get_json()
    r = client.post("/api/admin/approval-processes", headers=h,
                    json={"name": "QProc", "object": "Case",
                          "steps": [{"name": "Step 1",
                                     "approver": {"type": "queue",
                                                 "id": q["id"]}}]})
    assert r.status_code == 201, r.get_json()
    proc = r.get_json()
    r = client.delete(f"/api/admin/queues/{q['id']}", headers=h)
    assert r.status_code == 409, r.get_json()
    assert "QProc" in " ".join(r.get_json()["dependents"])
    r = client.delete(f"/api/admin/approval-processes/{proc['id']}",
                      headers=h)
    assert r.status_code == 200, r.get_json()
    r = client.delete(f"/api/admin/queues/{q['id']}", headers=h)
    assert r.status_code == 200, r.get_json()


def test_flow_delete_blocked_by_subflow(client):
    h = login(client)
    r = client.post("/api/admin/flows", headers=h,
                    json={"name": "Sub Target", "object": "Case",
                          "trigger": "on_create", "actions": []})
    assert r.status_code == 201, r.get_json()
    target = r.get_json()
    r = client.post("/api/admin/flows", headers=h,
                    json={"name": "Sub Caller", "object": "Case",
                          "trigger": "on_create",
                          "actions": [{"type": "subflow",
                                       "flow": target["name"]}]})
    assert r.status_code == 201, r.get_json()
    caller = r.get_json()
    r = client.delete(f"/api/admin/flows/{target['id']}", headers=h)
    assert r.status_code == 409, r.get_json()
    assert "Sub Caller" in " ".join(r.get_json()["dependents"])
    r = client.delete(f"/api/admin/flows/{caller['id']}", headers=h)
    assert r.status_code == 200, r.get_json()
    r = client.delete(f"/api/admin/flows/{target['id']}", headers=h)
    assert r.status_code == 200, r.get_json()


def test_approval_process_delete_blocked_by_pending_request(app, client):
    h = login(client)
    r = client.post("/api/admin/approval-processes", headers=h,
                    json={"name": "PendProc", "object": "Case", "steps": []})
    assert r.status_code == 201, r.get_json()
    proc = r.get_json()
    app.mf_store.config_put("mf_approval_requests",
                            {"id": "apr1", "object": "Case",
                             "record_id": "r1", "process_id": proc["id"],
                             "process_name": "PendProc", "status": "Pending",
                             "current_step": 0, "steps": [],
                             "submitted_by": "u1", "submitted_at": "t",
                             "history": []})
    r = client.delete(f"/api/admin/approval-processes/{proc['id']}",
                      headers=h)
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["dependents"], "dependents should be listed"
    # A completed request does not block.
    app.mf_store.config_put("mf_approval_requests",
                            {"id": "apr1", "object": "Case",
                             "record_id": "r1", "process_id": proc["id"],
                             "process_name": "PendProc", "status": "Approved",
                             "current_step": 0, "steps": [],
                             "submitted_by": "u1", "submitted_at": "t",
                             "history": []})
    r = client.delete(f"/api/admin/approval-processes/{proc['id']}",
                      headers=h)
    assert r.status_code == 200, r.get_json()
