"""Tests for the P1 admin-experience gaps: object/field manager, page-layout
editor API, condition-builder coverage (approval + duplicate-rule criteria),
audit-trail filters/pagination/CSV, server-side pagination, role hierarchy
(rename/reparent/delete), permission sets (edit/unassign/per-user view),
email-log filters/pagination, and CSV export options."""
import os
import tempfile

import pytest

from forcelet.api import create_app
from forcelet.duplicate_rules import _criteria_matches
from helpers import login


@pytest.fixture()
def app_client():
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_app(db)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, app
    for suf in ("", "-wal", "-shm"):
        try:
            os.unlink(db + suf)
        except OSError:
            pass


@pytest.fixture()
def admin(app_client):
    c, _app = app_client
    return login(c, "admin")


def _make_object(c, h, name="Widget__c", label="Widget"):
    r = c.post("/api/admin/objects", headers=h,
               json={"name": name, "label": label, "plural": label + "s"})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def _add_field(c, h, obj, name, ftype="Text", **kw):
    body = {"name": name, "label": name, "type": ftype}
    body.update(kw)
    r = c.post(f"/api/admin/objects/{obj}/fields", headers=h, json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()


# ------------------------------------------------- 1. object/field manager
def test_field_update_label(app_client, admin):
    c, _app = app_client
    _make_object(c, admin)
    _add_field(c, admin, "Widget__c", "Color__c")
    r = c.put("/api/admin/objects/Widget__c/fields/Color__c", headers=admin,
              json={"label": "Colour", "help_text": "Pick a colour"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["label"] == "Colour"
    r = c.get("/api/admin/objects/Widget__c/fields", headers=admin)
    fld = next(f for f in r.get_json() if f["name"] == "Color__c")
    assert fld["label"] == "Colour"


def test_field_update_refuses_type_change(app_client, admin):
    c, _app = app_client
    _make_object(c, admin)
    _add_field(c, admin, "Widget__c", "Color__c")
    r = c.put("/api/admin/objects/Widget__c/fields/Color__c", headers=admin,
              json={"type": "Number"})
    assert r.status_code == 422


def test_field_update_standard_object_forbidden(app_client, admin):
    c, _app = app_client
    r = c.put("/api/admin/objects/Account/fields/Name", headers=admin,
              json={"label": "Nope"})
    assert r.status_code == 403


def test_field_deactivate_hides_from_describe(app_client, admin):
    c, _app = app_client
    _make_object(c, admin)
    _add_field(c, admin, "Widget__c", "Color__c")
    r = c.put("/api/admin/objects/Widget__c/fields/Color__c", headers=admin,
              json={"active": False})
    assert r.status_code == 200
    r = c.get("/api/describe/Widget__c", headers=admin)
    assert "Color__c" not in [f["name"] for f in r.get_json()["fields"]]
    # re-activate restores it
    r = c.put("/api/admin/objects/Widget__c/fields/Color__c", headers=admin,
              json={"active": True})
    assert r.status_code == 200
    r = c.get("/api/describe/Widget__c", headers=admin)
    assert "Color__c" in [f["name"] for f in r.get_json()["fields"]]


def test_field_delete_guarded_when_values_exist(app_client, admin):
    c, _app = app_client
    _make_object(c, admin)
    _add_field(c, admin, "Widget__c", "Color__c")
    r = c.post("/api/sobjects/Widget__c", headers=admin,
               json={"Name": "W1", "Color__c": "red"})
    assert r.status_code == 201, r.get_json()
    r = c.delete("/api/admin/objects/Widget__c/fields/Color__c", headers=admin)
    assert r.status_code == 409
    assert "1 record(s)" in r.get_json()["error"]


def test_field_delete_empty_ok(app_client, admin):
    c, _app = app_client
    _make_object(c, admin)
    _add_field(c, admin, "Widget__c", "Color__c")
    r = c.delete("/api/admin/objects/Widget__c/fields/Color__c", headers=admin)
    assert r.status_code == 200, r.get_json()
    r = c.get("/api/admin/objects/Widget__c/fields", headers=admin)
    assert "Color__c" not in [f["name"] for f in r.get_json()]


def test_object_delete_guarded_when_records_exist(app_client, admin):
    c, _app = app_client
    _make_object(c, admin)
    r = c.post("/api/sobjects/Widget__c", headers=admin, json={"Name": "W1"})
    assert r.status_code == 201
    r = c.delete("/api/admin/objects/Widget__c", headers=admin)
    assert r.status_code == 409
    assert "1 record(s)" in r.get_json()["error"]


def test_object_delete_empty_ok(app_client, admin):
    c, _app = app_client
    _make_object(c, admin)
    r = c.delete("/api/admin/objects/Widget__c", headers=admin)
    assert r.status_code == 200, r.get_json()
    r = c.get("/api/objects", headers=admin)
    assert "Widget__c" not in [o["name"] for o in r.get_json()]


# ------------------------------------------------- 2. page-layout editor
def test_layout_list_and_delete(app_client, admin):
    c, _app = app_client
    r = c.post("/api/admin/layouts", headers=admin,
               json={"object": "Account", "profile": "Standard User",
                     "sections": [{"title": "S", "columns": [["Name"]]}]})
    assert r.status_code in (200, 201), r.get_json()
    r = c.get("/api/admin/layouts", headers=admin)
    assert r.status_code == 200
    rows = r.get_json()
    assert any(x["object"] == "Account" for x in rows)
    r = c.delete("/api/admin/layouts?object=Account&profile=Standard+User",
                 headers=admin)
    assert r.status_code == 200, r.get_json()
    r = c.get("/api/admin/layouts", headers=admin)
    assert not any(x["object"] == "Account" and
                   x.get("profile") == "Standard User" for x in r.get_json())


# ------------------------------------------------- 3. condition builder
def test_criteria_matches_unit():
    assert _criteria_matches(None, {"Status": "New"}) is True
    assert _criteria_matches("", {"Status": "New"}) is True
    assert _criteria_matches("not json", {"Status": "New"}) is False
    assert _criteria_matches({"==": [{"field": "Status"}, "New"]}, {"Status": "New"}) is True
    assert _criteria_matches({"==": [{"field": "Status"}, "New"]}, {"Status": "Old"}) is False


def test_duplicate_rule_criteria_blocks_only_matching(app_client, admin):
    c, _app = app_client
    _make_object(c, admin, "Gadget__c", "Gadget")
    _add_field(c, admin, "Gadget__c", "Serial__c")
    _add_field(c, admin, "Gadget__c", "Status__c")
    mid = c.post("/api/platform/matching-rules", headers=admin, json={
        "Name": "M", "ObjectName": "Gadget__c", "Fields": "Serial__c",
        "MatchType": "Exact", "IsActive": True}).get_json()["Id"]
    r = c.post("/api/platform/duplicate-rules", headers=admin, json={
        "Name": "D", "ObjectName": "Gadget__c", "MatchingRuleId": mid,
        "Action": "Block", "AppliesOn": "Both", "IsActive": True,
        "Criteria": {"==": [{"field": "Status__c"}, "New"]}})
    assert r.status_code == 201, r.get_json()
    base = {"Name": "G1", "Serial__c": "S-1"}
    r = c.post("/api/sobjects/Gadget__c", headers=admin,
               json=dict(base, Status__c="New"))
    assert r.status_code == 201, r.get_json()
    # same serial + matching criteria -> blocked
    r = c.post("/api/sobjects/Gadget__c", headers=admin,
               json=dict(base, Status__c="New"))
    assert r.status_code == 409, r.get_json()
    # same serial but criteria does not match -> allowed
    r = c.post("/api/sobjects/Gadget__c", headers=admin,
               json=dict(base, Status__c="Old"))
    assert r.status_code == 201, r.get_json()


# ------------------------------------------------- 4. audit trail
def test_audit_trail_envelope_and_filters(app_client, admin):
    c, _app = app_client
    c.post("/api/admin/validation-rules", headers=admin,
           json={"name": "P1 probe", "object": "Account",
                 "condition": {}, "message": "x"})
    r = c.get("/api/admin/audit-trail", headers=admin)
    assert r.status_code == 200
    body = r.get_json()
    assert {"rows", "total", "limit", "offset"} <= set(body)
    assert body["total"] >= 1
    assert all("at" in e for e in body["rows"])
    # username filter
    r = c.get("/api/admin/audit-trail", headers=admin,
              query_string={"username": "admin"})
    assert all(e["username"] == "admin" for e in r.get_json()["rows"])
    # action filter
    r = c.get("/api/admin/audit-trail", headers=admin,
              query_string={"action": "create", "entity": "P1 probe"})
    rows = r.get_json()["rows"]
    assert rows and all(e["action"] == "create" for e in rows)
    # date filter (today)
    from datetime import date
    r = c.get("/api/admin/audit-trail", headers=admin,
              query_string={"date_from": date.today().isoformat(),
                            "date_to": date.today().isoformat()})
    assert r.get_json()["total"] >= 1


def test_audit_trail_pagination(app_client, admin):
    c, _app = app_client
    for i in range(3):
        c.post("/api/admin/validation-rules", headers=admin,
               json={"name": f"Page probe {i}", "object": "Account",
                     "condition": {}, "message": "x"})
    r = c.get("/api/admin/audit-trail", headers=admin,
              query_string={"limit": 2, "offset": 0})
    b1 = r.get_json()
    assert b1["limit"] == 2 and b1["offset"] == 0 and len(b1["rows"]) == 2
    r = c.get("/api/admin/audit-trail", headers=admin,
              query_string={"limit": 2, "offset": 2})
    b2 = r.get_json()
    assert b2["offset"] == 2
    assert b1["total"] == b2["total"]
    ids1 = {e["id"] for e in b1["rows"]}
    ids2 = {e["id"] for e in b2["rows"]}
    assert not ids1 & ids2


def test_audit_trail_export_csv(app_client, admin):
    c, _app = app_client
    c.post("/api/admin/validation-rules", headers=admin,
           json={"name": "CSV probe", "object": "Account",
                 "condition": {}, "message": "x"})
    r = c.get("/api/admin/audit-trail/export", headers=admin,
              query_string={"entity": "CSV probe"})
    assert r.status_code == 200
    assert "text/csv" in r.headers["Content-Type"]
    text = r.get_data(as_text=True)
    assert "User" in text.splitlines()[0]
    assert "CSV probe" in text


# ------------------------------------------------- 5. server-side pagination
def test_list_records_paginated_envelope(app_client, admin):
    c, _app = app_client
    for i in range(3):
        c.post("/api/sobjects/Lead", headers=admin,
               json={"LastName": f"Page{i}", "Company": "Acme"})
    r = c.get("/api/sobjects/Lead", headers=admin,
              query_string={"limit": 2, "offset": 0})
    body = r.get_json()
    assert {"rows", "total", "limit", "offset"} <= set(body)
    assert body["total"] >= 3 and len(body["rows"]) == 2
    r = c.get("/api/sobjects/Lead", headers=admin,
              query_string={"limit": 2, "offset": 2})
    assert r.get_json()["offset"] == 2


def test_list_records_legacy_array_without_params(app_client, admin):
    c, _app = app_client
    c.post("/api/sobjects/Lead", headers=admin,
           json={"LastName": "Legacy", "Company": "Acme"})
    r = c.get("/api/sobjects/Lead", headers=admin)
    assert isinstance(r.get_json(), list)


def test_admin_config_paginated_and_single(app_client, admin):
    c, _app = app_client
    for i in range(3):
        c.post("/api/admin/validation-rules", headers=admin,
               json={"name": f"Cfg {i}", "object": "Account",
                     "condition": {}, "message": "x"})
    r = c.get("/api/admin/validation-rules", headers=admin,
              query_string={"limit": 2, "offset": 0})
    body = r.get_json()
    assert {"rows", "total"} <= set(body) and len(body["rows"]) == 2
    rid = body["rows"][0]["id"]
    r = c.get(f"/api/admin/validation-rules/{rid}", headers=admin)
    assert r.status_code == 200 and r.get_json()["id"] == rid
    r = c.get("/api/admin/validation-rules", headers=admin)
    assert isinstance(r.get_json(), list)


# ------------------------------------------------- 6. role hierarchy
def test_role_rename_cascades(app_client, admin):
    c, _app = app_client
    c.post("/api/admin/roles", headers=admin,
           json={"name": "CEO", "parent": None})
    c.post("/api/admin/roles", headers=admin,
           json={"name": "Mgr", "parent": "CEO"})
    c.post("/api/admin/roles", headers=admin,
           json={"name": "Rep", "parent": "Mgr"})
    r = c.post("/api/admin/users", headers=admin,
               json={"username": "rep1", "name": "Rep One",
                     "email": "rep1@example.com", "profile": "Standard User",
                     "password": "UserPass1!", "role": "Rep"})
    assert r.status_code == 201, r.get_json()
    r = c.put("/api/admin/roles/Mgr", headers=admin,
              json={"name": "Manager", "parent": "CEO"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["name"] == "Manager"
    roles = {x["name"]: x for x in
             c.get("/api/admin/roles", headers=admin).get_json()}
    assert "Mgr" not in roles and roles["Rep"]["parent"] == "Manager"


def test_role_reparent_cycle_rejected(app_client, admin):
    c, _app = app_client
    c.post("/api/admin/roles", headers=admin, json={"name": "Top"})
    c.post("/api/admin/roles", headers=admin,
           json={"name": "Bottom", "parent": "Top"})
    r = c.put("/api/admin/roles/Top", headers=admin,
              json={"parent": "Bottom"})
    assert r.status_code == 422
    r = c.put("/api/admin/roles/Top", headers=admin,
              json={"parent": "Top"})
    assert r.status_code == 422


def test_role_delete_guards(app_client, admin):
    c, _app = app_client
    c.post("/api/admin/roles", headers=admin, json={"name": "Chief"})
    c.post("/api/admin/roles", headers=admin,
           json={"name": "Worker", "parent": "Chief"})
    r = c.post("/api/admin/users", headers=admin,
               json={"username": "w1", "name": "W One",
                     "email": "w1@example.com", "profile": "Standard User",
                     "password": "UserPass1!", "role": "Worker"})
    assert r.status_code == 201
    # users assigned -> 409
    r = c.delete("/api/admin/roles/Worker", headers=admin)
    assert r.status_code == 409
    # child roles exist -> 409
    r = c.delete("/api/admin/roles/Chief", headers=admin)
    assert r.status_code == 409
    # unknown -> 404
    r = c.delete("/api/admin/roles/Nope", headers=admin)
    assert r.status_code == 404


def test_role_delete_leaf_ok(app_client, admin):
    c, _app = app_client
    c.post("/api/admin/roles", headers=admin, json={"name": "Solo"})
    r = c.delete("/api/admin/roles/Solo", headers=admin)
    assert r.status_code == 200
    roles = c.get("/api/admin/roles", headers=admin).get_json()
    assert "Solo" not in [x["name"] for x in roles]


# ------------------------------------------------- 7. permission sets
def _make_ps(c, h, name="ps1"):
    r = c.post("/api/admin/permission-sets", headers=h,
               json={"name": name, "label": name.upper(),
                     "object_permissions": {"Lead": {"delete": True}},
                     "field_permissions": {}})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def _make_user(c, h, username):
    r = c.post("/api/admin/users", headers=h,
               json={"username": username, "name": username,
                     "email": f"{username}@example.com",
                     "profile": "Standard User", "password": "UserPass1!"})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def test_permission_set_edit_via_patch(app_client, admin):
    c, _app = app_client
    ps = _make_ps(c, admin)
    r = c.patch(f"/api/admin/permission-sets/{ps['id']}", headers=admin,
                json={"label": "PS One Renamed",
                      "object_permissions": {"Lead": {"delete": False}}})
    assert r.status_code == 200, r.get_json()
    r = c.get(f"/api/admin/permission-sets/{ps['id']}", headers=admin)
    assert r.get_json()["label"] == "PS One Renamed"


def test_permission_set_assign_list_unassign(app_client, admin):
    c, _app = app_client
    ps = _make_ps(c, admin)
    u = _make_user(c, admin, "psuser1")
    r = c.post(f"/api/admin/users/{u['id']}/permission-sets", headers=admin,
               json={"permission_set": ps["id"]})
    assert r.status_code == 200, r.get_json()
    r = c.get(f"/api/admin/users/{u['id']}/permission-sets", headers=admin)
    assert r.status_code == 200
    assigned = r.get_json()
    assert [a["id"] for a in assigned] == [ps["id"]]
    assert assigned[0]["label"] == "PS1"
    r = c.delete(f"/api/admin/users/{u['id']}/permission-sets/{ps['id']}",
                 headers=admin)
    assert r.status_code == 200
    r = c.get(f"/api/admin/users/{u['id']}/permission-sets", headers=admin)
    assert r.get_json() == []
    # unassign again -> 422
    r = c.delete(f"/api/admin/users/{u['id']}/permission-sets/{ps['id']}",
                 headers=admin)
    assert r.status_code == 422
    # unknown user -> 404
    r = c.get("/api/admin/users/nonexistent/permission-sets", headers=admin)
    assert r.status_code == 404


# ------------------------------------------------- 8. email log
def _seed_emails(app, n=3):
    store = app.mf_store
    admin_user = store.meta_get("mf_users",
                                [u["id"] for u in store.meta_all("mf_users")
                                 if u.get("username") == "admin"][0])
    for i in range(n):
        store.log_email("Lead", f"rec{i}", f"user{i}@example.com",
                        f"Subject {i}", "body", "Welcome" if i % 2 == 0 else "Nurture",
                        admin_user)


def test_email_log_envelope_and_pagination(app_client, admin):
    c, app = app_client
    _seed_emails(app, 5)
    r = c.get("/api/admin/email-log", headers=admin,
              query_string={"limit": 2, "offset": 0})
    body = r.get_json()
    assert {"rows", "total", "limit", "offset"} <= set(body)
    assert body["total"] == 5 and len(body["rows"]) == 2
    r = c.get("/api/admin/email-log", headers=admin,
              query_string={"limit": 2, "offset": 4})
    assert len(r.get_json()["rows"]) == 1


def test_email_log_filters(app_client, admin):
    c, app = app_client
    _seed_emails(app, 4)
    r = c.get("/api/admin/email-log", headers=admin,
              query_string={"to": "user1@", "offset": 0})
    rows = r.get_json()["rows"]
    assert rows and all("user1@" in e["recipient"] for e in rows)
    r = c.get("/api/admin/email-log", headers=admin,
              query_string={"template": "Welcome", "offset": 0})
    rows = r.get_json()["rows"]
    assert rows and all(e["template"] == "Welcome" for e in rows)
    from datetime import date
    r = c.get("/api/admin/email-log", headers=admin,
              query_string={"date_from": "2000-01-01",
                            "date_to": date.today().isoformat(),
                            "offset": 0})
    assert r.get_json()["total"] == 4


def test_email_log_legacy_array(app_client, admin):
    c, app = app_client
    _seed_emails(app, 2)
    r = c.get("/api/admin/email-log", headers=admin)
    assert isinstance(r.get_json(), list)
    assert len(r.get_json()) == 2


# ------------------------------------------------- 9. CSV export options
def test_export_fields_param(app_client, admin):
    c, _app = app_client
    c.post("/api/sobjects/Lead", headers=admin,
           json={"LastName": "Export", "Company": "Acme",
                 "Email": "exp@example.com"})
    r = c.get("/api/sobjects/Lead/export", headers=admin,
              query_string={"fields": "LastName"})
    assert r.status_code == 200
    header = r.get_data(as_text=True).splitlines()[0]
    assert header == "Id,RecordType,LastName"


def test_export_filename(app_client, admin):
    c, _app = app_client
    r = c.get("/api/sobjects/Lead/export", headers=admin,
              query_string={"filename": "my-leads"})
    assert r.status_code == 200
    assert "my-leads.csv" in r.headers["Content-Disposition"]


def test_export_applies_view_filter(app_client, admin):
    c, _app = app_client
    c.post("/api/sobjects/Lead", headers=admin,
           json={"LastName": "VF1", "Company": "Acme", "Status": "New"})
    c.post("/api/sobjects/Lead", headers=admin,
           json={"LastName": "VF2", "Company": "Acme", "Status": "Working"})
    r = c.post("/api/list-views", headers=admin,
               json={"object": "Lead", "name": "New only",
                     "columns": ["LastName"],
                     "filters": {"==": [{"field": "Status"}, "New"]}})
    assert r.status_code in (200, 201), r.get_json()
    vid = r.get_json()["id"]
    r = c.get("/api/sobjects/Lead/export", headers=admin,
              query_string={"view": vid, "fields": "LastName,Status"})
    lines = r.get_data(as_text=True).strip().splitlines()
    assert len(lines) >= 2, lines  # header + matching rows (seed data exists)
    assert any("VF1" in ln for ln in lines[1:])
    assert not any("VF2" in ln for ln in lines[1:])
