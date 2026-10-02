"""Tests for the report/dashboard security fixes (Phase 1, 2026-10-02).

Gap A: run_dashboard must not expose reports the effective user cannot see
       (e.g. a report in a private folder) via dashboard widgets.
Gap B: _enrich_related (parent-lookup columns like Account.Name) must enforce
       record sharing (can_see_record) and field-level security on the parent
       row/field; failing either blanks the cell, never the child row.
"""
import pytest

from forcelet.api import create_app
from helpers import login


@pytest.fixture()
def app(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def _mk_profile(client, h, name, obj_perms, field_perms=None):
    body = {"name": name, "object_permissions": obj_perms,
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


def _mk_folder(client, h, name, kind="report", visibility="private"):
    r = client.post("/api/folders", headers=h,
                    json={"name": name, "kind": kind, "visibility": visibility})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def _mk_report(client, h, name, obj, columns, folder_id=None):
    r = client.post("/api/admin/reports", headers=h,
                    json={"name": name, "object": obj, "columns": columns,
                          "folder_id": folder_id})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def _mk_dashboard(client, h, name, widgets, folder_id=None):
    r = client.post("/api/dashboards", headers=h,
                    json={"name": name, "widgets": widgets,
                          "folder_id": folder_id})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


@pytest.fixture()
def env(client):
    """Admin + restricted user bob (Account/Contact read). Returns dict."""
    h_admin = login(client)
    prof = _mk_profile(client, h_admin, "repsec", {
        "Account": {"read": True, "create": True, "edit": True},
        "Contact": {"read": True, "create": True, "edit": True},
        # A Contact trigger auto-creates a follow-up Task.
        "Task": {"read": True, "create": True},
    })
    _mk_user(client, h_admin, "repsecbob", prof["name"])
    h_bob = login(client, "repsecbob", password="UserPass1!")
    return {"admin": h_admin, "bob": h_bob}


# ------------------------------------------------------------------ Gap A


def test_dashboard_widget_hides_private_report(client, env):
    h_admin, h_bob = env["admin"], env["bob"]
    # Admin-owned private folder; report lives inside it.
    fid = _mk_folder(client, h_admin, "PrivReps")
    aid = client.post("/api/sobjects/Account", headers=h_admin,
                      json={"Name": "Acme"}).get_json()["Id"]
    rep_id = _mk_report(client, h_admin, "Priv Report", "Account", ["Name"],
                        folder_id=fid)
    # Dashboard itself is visible to everyone (no folder).
    did = _mk_dashboard(client, h_admin, "Dash",
                        [{"report_id": rep_id, "type": "table"}])
    r = client.post(f"/api/dashboards/{did}/run", headers=h_bob, json={})
    assert r.status_code == 200, r.get_json()
    widgets = r.get_json()["widgets"]
    assert len(widgets) == 1
    w = widgets[0]
    assert w.get("error"), "widget must error, not leak data"
    assert "data" not in w, "no report data may be exposed"


def test_dashboard_widget_runs_visible_report(client, env):
    h_admin, h_bob = env["admin"], env["bob"]
    # Bob owns the Account so he can see it (private sharing).
    client.post("/api/sobjects/Account", headers=h_bob,
                json={"Name": "Acme"})
    rep_id = _mk_report(client, h_admin, "Open Report", "Account", ["Name"])
    did = _mk_dashboard(client, h_admin, "Dash",
                        [{"report_id": rep_id, "type": "table"}])
    r = client.post(f"/api/dashboards/{did}/run", headers=h_bob, json={})
    assert r.status_code == 200, r.get_json()
    w = r.get_json()["widgets"][0]
    assert "error" not in w
    assert w["data"]["row_count"] >= 1


def test_admin_sees_private_report_widget(client, env):
    h_admin = env["admin"]
    fid = _mk_folder(client, h_admin, "PrivReps2")
    client.post("/api/sobjects/Account", headers=h_admin,
                json={"Name": "Acme"})
    rep_id = _mk_report(client, h_admin, "Priv Report 2", "Account", ["Name"],
                        folder_id=fid)
    did = _mk_dashboard(client, h_admin, "Dash2",
                        [{"report_id": rep_id, "type": "table"}])
    r = client.post(f"/api/dashboards/{did}/run", headers=h_admin, json={})
    assert r.status_code == 200, r.get_json()
    w = r.get_json()["widgets"][0]
    assert "error" not in w
    assert w["data"]["row_count"] >= 1


# ------------------------------------------------------------------ Gap B


def _contact_report(client, env):
    """Admin-owned Account + bob-owned Contact pointing at it; returns ids."""
    h_admin, h_bob = env["admin"], env["bob"]
    aid = client.post("/api/sobjects/Account", headers=h_admin,
                      json={"Name": "Acme"}).get_json()["Id"]
    cid = client.post("/api/sobjects/Contact", headers=h_bob,
                      json={"LastName": "Smith", "AccountId": aid}).get_json()["Id"]
    rep_id = _mk_report(client, h_admin, "Contacts+Acct", "Contact",
                        ["LastName", "Account.Name"])
    return aid, cid, rep_id


def test_parent_sharing_blanks_cell_not_row(client, env):
    h_bob = env["bob"]
    _, _, rep_id = _contact_report(client, env)
    r = client.get(f"/api/reports/{rep_id}/run", headers=h_bob)
    assert r.status_code == 200, r.get_json()
    rows = [x for x in r.get_json()["rows"] if x.get("LastName") == "Smith"]
    assert rows, "child row must still appear"
    # Bob cannot see admin's Account -> parent cell blanked.
    assert rows[0].get("Account.Name") is None


def test_parent_fls_blanks_cell(client, env):
    h_admin = env["admin"]
    # Profile denying read on Account.Phone (non-required, so the user can
    # still create Accounts). Carol owns the Account (sharing passes); admin
    # populates the restricted field afterwards.
    prof = _mk_profile(client, h_admin, "repsecfls", {
        "Account": {"read": True, "create": True, "edit": True},
        "Contact": {"read": True, "create": True, "edit": True},
        # A Contact trigger auto-creates a follow-up Task.
        "Task": {"read": True, "create": True},
    }, field_perms={"Account": {"Phone": {"read": False}}})
    _mk_user(client, h_admin, "repseccarol", prof["name"])
    h_carol = login(client, "repseccarol", password="UserPass1!")
    aid = client.post("/api/sobjects/Account", headers=h_carol,
                      json={"Name": "Acme"}).get_json()["Id"]
    r = client.patch(f"/api/sobjects/Account/{aid}", headers=h_admin,
                     json={"Phone": "555-1234"})
    assert r.status_code == 200, r.get_json()
    client.post("/api/sobjects/Contact", headers=h_carol,
                json={"LastName": "Jones", "AccountId": aid})
    rep_id = _mk_report(client, h_admin, "Contacts+Acct2", "Contact",
                        ["LastName", "Account.Name", "Account.Phone"])
    r = client.get(f"/api/reports/{rep_id}/run", headers=h_carol)
    assert r.status_code == 200, r.get_json()
    rows = [x for x in r.get_json()["rows"] if x.get("LastName") == "Jones"]
    assert rows, "child row must still appear"
    # Carol sees the Account (owner) and may read Name, but Phone is
    # FLS-restricted -> blanked.
    assert rows[0].get("Account.Name") == "Acme"
    assert rows[0].get("Account.Phone") is None


def test_admin_sees_parent_columns(client, env):
    h_admin = env["admin"]
    _, _, rep_id = _contact_report(client, env)
    r = client.get(f"/api/reports/{rep_id}/run", headers=h_admin)
    assert r.status_code == 200, r.get_json()
    rows = [x for x in r.get_json()["rows"] if x.get("LastName") == "Smith"]
    assert rows
    assert rows[0].get("Account.Name") == "Acme"


def test_null_fk_parent_column_stays_none(client, env):
    h_admin, h_bob = env["admin"], env["bob"]
    client.post("/api/sobjects/Contact", headers=h_bob,
                json={"LastName": "NoParent"})
    rep_id = _mk_report(client, h_admin, "Contacts+Acct3", "Contact",
                        ["LastName", "Account.Name"])
    r = client.get(f"/api/reports/{rep_id}/run", headers=h_bob)
    assert r.status_code == 200, r.get_json()
    rows = [x for x in r.get_json()["rows"] if x.get("LastName") == "NoParent"]
    assert rows
    assert rows[0].get("Account.Name") is None
