"""Tests for the P2 admin-experience gaps: divisions (CRUD, visibility, record
moves), sandbox seeding templates, the import wizard (duplicate handling and
error retry), the translation workbench, login branding & post-login flows,
and delegated administration groups."""
import io
import os
import tempfile

import pytest

from forcelet.api import create_app
from helpers import login


@pytest.fixture()
def client():
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_app(db)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    os.unlink(db)
    for ext in ("-wal", "-shm"):
        try:
            os.unlink(db + ext)
        except OSError:
            pass


def make_user(client, h, username, password="UserPass1!", profile="Standard User",
              **kw):
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


def make_division(client, h, name="EMEA", **kw):
    body = {"name": name, "description": kw.get("description", "")}
    r = client.post("/api/admin/divisions", headers=h, json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def import_csv(client, h, obj, rows, **params):
    buf = io.StringIO()
    headers = list(dict.fromkeys(k for row in rows for k in row.keys()))
    buf.write(",".join(headers) + "\n")
    for row in rows:
        buf.write(",".join(str(row.get(k, "")) for k in headers) + "\n")
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return client.post(f"/api/admin/import/{obj}?{qs}", headers=h,
                       data={"file": (io.BytesIO(buf.getvalue().encode()),
                                      "data.csv")})


def make_matching_rule(client, h):
    r = client.post("/api/admin/matching-rules", headers=h,
                    json={"name": "acct-name", "object": "Account",
                          "fields": ["Name"], "active": True})
    assert r.status_code == 201, r.get_json()


# ------------------------------------------------------- divisions (item 4)
def test_division_crud_and_name_uniqueness(client):
    h = login(client)
    d = make_division(client, h, "EMEA")
    assert d["name"] == "EMEA"
    # Case-insensitive duplicate name is rejected.
    r = client.post("/api/admin/divisions", headers=h,
                    json={"name": "emea"})
    assert r.status_code == 422
    r = client.patch(f"/api/admin/divisions/{d['id']}", headers=h,
                     json={"description": "Europe"})
    assert r.status_code == 200
    assert r.get_json()["description"] == "Europe"
    r = client.delete(f"/api/admin/divisions/{d['id']}", headers=h)
    assert r.status_code == 200


def test_division_delete_blocked_when_records_assigned(client):
    h = login(client)
    d = make_division(client, h, "APAC")
    r = client.post("/api/sobjects/Account", headers=h, json={"Name": "DivCo"})
    aid = r.get_json()["Id"]
    r = client.put(f"/api/sobjects/Account/{aid}/division", headers=h,
                   json={"division_id": d["id"]})
    assert r.status_code == 200
    assert r.get_json()["division"] == "APAC"
    r = client.delete(f"/api/admin/divisions/{d['id']}", headers=h)
    assert r.status_code == 409
    # Move the record back to global, then delete works.
    r = client.put(f"/api/sobjects/Account/{aid}/division", headers=h,
                   json={"division_id": ""})
    assert r.status_code == 200
    r = client.delete(f"/api/admin/divisions/{d['id']}", headers=h)
    assert r.status_code == 200


def test_division_visibility_and_filter(client):
    h = login(client)
    d = make_division(client, h, "West")
    # Standard User profile -> default division West.
    r = client.put("/api/admin/profiles/Standard%20User", headers=h,
                   json={"default_division": d["id"]})
    assert r.status_code == 200
    # A second profile with Account access but no division.
    r = client.post("/api/admin/profiles", headers=h, json={"name": "DivTester"})
    assert r.status_code == 201
    r = client.put("/api/admin/profiles/DivTester", headers=h,
                   json={"object_permissions":
                         {"Account": {"create": True, "read": True,
                                      "edit": True, "delete": False}}})
    assert r.status_code == 200
    # sam manages pam in the role hierarchy, so sharing lets him see her
    # records; divisions then partition on top.
    sam = make_user(client, h, "westsam", role="VP Support")
    pam = make_user(client, h, "plainpam", profile="DivTester",
                    role="Support Agent")
    uh = login_as(client, "westsam")
    ph = login_as(client, "plainpam")
    g = client.post("/api/sobjects/Account", headers=ph,
                    json={"Name": "GlobalCo"}).get_json()
    w = client.post("/api/sobjects/Account", headers=ph,
                    json={"Name": "WestCo"}).get_json()
    r = client.put(f"/api/sobjects/Account/{w['Id']}/division", headers=h,
                   json={"division_id": d["id"]})
    assert r.status_code == 200

    # User with a division sees that division's records plus global ones.
    r = client.get("/api/sobjects/Account", headers=uh)
    assert r.status_code == 200
    assert {x["Name"] for x in r.get_json()} == {"GlobalCo", "WestCo"}
    # Explicit division filter narrows the list.
    r = client.get(f"/api/sobjects/Account?division={d['id']}", headers=uh)
    assert {x["Name"] for x in r.get_json()} == {"WestCo"}
    r = client.get("/api/sobjects/Account?division=global", headers=uh)
    assert {x["Name"] for x in r.get_json()} == {"GlobalCo"}

    # A user with no division sees only global records.
    r = client.get("/api/sobjects/Account", headers=ph)
    assert {x["Name"] for x in r.get_json()} == {"GlobalCo"}

    # Admins see everything (superset: seed data may add more).
    r = client.get("/api/sobjects/Account", headers=h)
    assert {"GlobalCo", "WestCo"} <= {x["Name"] for x in r.get_json()}


def test_record_set_division_requires_admin(client):
    h = login(client)
    d = make_division(client, h, "North")
    aid = client.post("/api/sobjects/Account", headers=h,
                      json={"Name": "NC"}).get_json()["Id"]
    make_user(client, h, "regular1")
    uh = login_as(client, "regular1")
    r = client.put(f"/api/sobjects/Account/{aid}/division", headers=uh,
                   json={"division_id": d["id"]})
    assert r.status_code == 403
    r = client.put(f"/api/sobjects/Account/{aid}/division", headers=h,
                   json={"division_id": "nope"})
    assert r.status_code == 422
    r = client.put(f"/api/sobjects/Account/{aid}/division", headers=h,
                   json={"division_id": d["id"]})
    assert r.status_code == 200


def test_division_usage_and_move(client):
    h = login(client)
    d1 = make_division(client, h, "D1")
    d2 = make_division(client, h, "D2")
    a1 = client.post("/api/sobjects/Account", headers=h,
                     json={"Name": "A1"}).get_json()["Id"]
    a2 = client.post("/api/sobjects/Account", headers=h,
                     json={"Name": "A2"}).get_json()["Id"]
    r = client.post(f"/api/admin/divisions/{d1['id']}/move", headers=h,
                    json={"records": [{"object": "Account", "id": a1},
                                      {"object": "Account", "id": a2}]})
    assert r.status_code == 200
    assert r.get_json()["moved"] == 2
    r = client.get("/api/admin/divisions/usage", headers=h)
    assert r.get_json()[d1["id"]] == 2
    assert r.get_json()[d2["id"]] == 0
    # Move one record to global.
    r = client.post("/api/admin/divisions/global/move", headers=h,
                    json={"records": [{"object": "Account", "id": a1}]})
    assert r.get_json()["moved"] == 1
    r = client.get("/api/admin/divisions/usage", headers=h)
    assert r.get_json()[d1["id"]] == 1


# --------------------------------------------- sandbox seeding (item 5)
def test_seed_template_validation(client):
    h = login(client)
    r = client.post("/api/admin/seed-templates", headers=h,
                    json={"name": "bad", "object": "Nope", "count": 5,
                          "field_rules": []})
    assert r.status_code == 422
    r = client.post("/api/admin/seed-templates", headers=h,
                    json={"name": "zero", "object": "Account", "count": 0,
                          "field_rules": []})
    assert r.status_code == 422
    r = client.post("/api/admin/seed-templates", headers=h,
                    json={"name": "badfield", "object": "Account", "count": 5,
                          "field_rules": [{"field": "Nope", "mode": "fixed"}]})
    assert r.status_code == 422
    r = client.post("/api/admin/seed-templates", headers=h,
                    json={"name": "badmode", "object": "Account", "count": 5,
                          "field_rules": [{"field": "Name", "mode": "nope"}]})
    assert r.status_code == 422


def test_seed_run_requires_confirm_and_creates(client):
    h = login(client)
    r = client.post("/api/admin/seed-templates", headers=h,
                    json={"name": "accts", "object": "Account", "count": 3,
                          "field_rules": [
                              {"field": "Name", "mode": "pattern",
                               "pattern": "SeedCo {seq}"},
                              {"field": "Industry", "mode": "picklist_random"}]})
    assert r.status_code == 201
    tid = r.get_json()["id"]
    r = client.post(f"/api/admin/seed-templates/{tid}/run", headers=h,
                    json={})
    assert r.status_code == 422
    r = client.post(f"/api/admin/seed-templates/{tid}/run", headers=h,
                    json={"confirm": True})
    assert r.status_code == 200
    assert r.get_json()["created"] == 3
    r = client.get("/api/sobjects/Account?search=SeedCo", headers=h)
    names = {x["Name"] for x in r.get_json()}
    assert names == {"SeedCo 1", "SeedCo 2", "SeedCo 3"}
    # last_run is stamped on the template.
    r = client.get("/api/admin/seed-templates", headers=h)
    tpl = next(t for t in r.get_json() if t["id"] == tid)
    assert tpl["last_run"]["created"] == 3


# --------------------------------------------- import wizard (item 6)
def test_import_on_duplicate_skip(client):
    h = login(client)
    make_matching_rule(client, h)
    client.post("/api/sobjects/Account", headers=h, json={"Name": "Dup Inc"})
    r = import_csv(client, h, "Account",
                   [{"Name": "Dup Inc"}, {"Name": "Fresh Inc"}],
                   mode="insert", on_duplicate="skip")
    d = r.get_json()
    assert d["skipped"] == 1
    assert d["created"] == 1
    assert d["failed"] == 0
    r = client.get("/api/sobjects/Account?search=Dup Inc", headers=h)
    assert len(r.get_json()) == 1


def test_import_on_duplicate_update(client):
    h = login(client)
    make_matching_rule(client, h)
    aid = client.post("/api/sobjects/Account", headers=h,
                      json={"Name": "Upd Inc"}).get_json()["Id"]
    r = import_csv(client, h, "Account",
                   [{"Name": "Upd Inc", "Phone": "555-0100"}],
                   mode="insert", on_duplicate="update")
    d = r.get_json()
    assert d["updated"] == 1
    assert d["created"] == 0
    r = client.get(f"/api/sobjects/Account/{aid}", headers=h)
    assert r.get_json()["Phone"] == "555-0100"


def test_import_on_duplicate_report_writes_nothing(client):
    h = login(client)
    make_matching_rule(client, h)
    client.post("/api/sobjects/Account", headers=h, json={"Name": "Rep Inc"})
    r = import_csv(client, h, "Account", [{"Name": "Rep Inc"}],
                   mode="insert", on_duplicate="report")
    d = r.get_json()
    assert d["failed"] == 1
    assert d["created"] == 0
    r = client.get("/api/sobjects/Account?search=Rep Inc", headers=h)
    assert len(r.get_json()) == 1


def test_import_run_persist_and_retry(client):
    h = login(client)
    # Second row has an invalid picklist value -> fails validation.
    r = import_csv(client, h, "Account",
                   [{"Name": "Good Inc"},
                    {"Name": "Bad Inc", "Industry": "NotARealIndustry"}],
                   mode="insert")
    d = r.get_json()
    assert d["created"] == 1
    assert d["failed"] == 1
    assert d["run_id"]
    assert d["error_csv_url"].endswith("/errors.csv")
    rid = d["run_id"]
    r = client.get("/api/admin/import-runs", headers=h)
    runs = r.get_json()
    assert any(x["id"] == rid for x in runs)
    r = client.get(f"/api/admin/import-runs/{rid}", headers=h)
    assert r.status_code == 200
    assert r.get_json()["failed"] == 1
    r = client.get(f"/api/admin/import-runs/{rid}/errors.csv", headers=h)
    assert r.status_code == 200
    assert "text/csv" in r.headers["Content-Type"]
    assert "Good Inc" not in r.get_data(as_text=True)
    # Retry: the row still fails, and the attempt is recorded.
    r = client.post(f"/api/admin/import-runs/{rid}/retry", headers=h)
    assert r.status_code == 200
    assert r.get_json()["failed"] == 1
    r = client.get(f"/api/admin/import-runs/{rid}", headers=h)
    assert len(r.get_json()["retries"]) == 1


# --------------------------------------- translation workbench (item 7)
def test_i18n_pack_and_fallback(client):
    r = client.get("/api/i18n/es")
    assert r.status_code == 200
    assert r.get_json()["login.signin"] == "Iniciar sesión"
    r = client.get("/api/i18n/xx")
    assert r.get_json()["login.signin"] == "Sign in"


def test_label_and_translation_crud(client):
    h = login(client)
    r = client.post("/api/admin/labels", headers=h,
                    json={"key": "badkey", "default_text": "x"})
    assert r.status_code == 422
    r = client.post("/api/admin/labels", headers=h,
                    json={"key": "app.greeting", "default_text": "Hello",
                          "category": "app"})
    assert r.status_code == 201
    lid = r.get_json()["id"]
    # Renaming a key is blocked.
    r = client.put(f"/api/admin/labels/{lid}", headers=h,
                   json={"key": "app.other"})
    assert r.status_code == 422
    r = client.post(f"/api/admin/labels/{lid}/translations", headers=h,
                    json={"language": "fr", "text": "Bonjour"})
    assert r.status_code == 201
    r = client.get("/api/i18n/fr")
    assert r.get_json()["app.greeting"] == "Bonjour"
    # Upsert the same language again (no duplicate rows).
    r = client.post(f"/api/admin/labels/{lid}/translations", headers=h,
                    json={"language": "fr", "text": "Salut"})
    assert r.status_code == 200
    r = client.get("/api/i18n/fr")
    assert r.get_json()["app.greeting"] == "Salut"
    r = client.get(f"/api/admin/labels/{lid}/translations", headers=h)
    assert len(r.get_json()) == 1
    tid = r.get_json()[0]["id"]
    r = client.delete(f"/api/admin/translations/{tid}", headers=h)
    assert r.status_code == 200
    r = client.delete(f"/api/admin/labels/{lid}", headers=h)
    assert r.status_code == 200


# ------------------------------ login branding + post-login flows (item 8)
def test_login_settings_validation(client):
    h = login(client)
    r = client.put("/api/admin/settings/login", headers=h,
                   json={"primary_color": "red"})
    assert r.status_code == 422
    r = client.put("/api/admin/settings/login", headers=h,
                   json={"logo_url": "javascript:alert(1)"})
    assert r.status_code == 422
    r = client.put("/api/admin/settings/login", headers=h,
                   json={"login_flow": [{"key": "nope", "enabled": True}]})
    assert r.status_code == 422
    r = client.put("/api/admin/settings/login", headers=h,
                   json={"primary_color": "#123abc", "headline": "Acme CRM",
                         "logo_url": "https://example.com/logo.png"})
    assert r.status_code == 200
    assert r.get_json()["headline"] == "Acme CRM"


def test_public_branding_and_announcement_flow(client):
    h = login(client)
    r = client.get("/api/public/login-branding")
    assert r.status_code == 200
    assert set(r.get_json()) == {"logo_url", "headline", "tagline",
                                 "primary_color", "background"}
    # No announcement configured yet.
    r = client.get("/api/me/announcement", headers=h)
    assert r.status_code == 200
    assert r.get_json()["announcement"] is None
    # Enable one.
    r = client.put("/api/admin/settings/login", headers=h,
                   json={"announcement_enabled": True,
                         "announcement_title": "Maintenance",
                         "announcement_body": "Sunday 2am PT"})
    assert r.status_code == 200
    r = client.get("/api/me/announcement", headers=h)
    assert r.get_json()["announcement"]["title"] == "Maintenance"
    r = client.post("/api/me/announcement/dismiss", headers=h)
    assert r.status_code == 200
    r = client.get("/api/me/announcement", headers=h)
    assert r.get_json()["announcement"] is None


# -------------------------------------- delegated administration (item 9)
def test_delegated_group_password_scope(client):
    h = login(client)
    agent = make_user(client, h, "agent1", role="Support Agent")
    boss = make_user(client, h, "boss1", role="VP Sales")
    helper = make_user(client, h, "helper1")
    hh = login_as(client, "helper1")
    # helper1 is not in any group -> 403 on admin user APIs.
    r = client.post(f"/api/admin/users/{agent['id']}/reset-password",
                    headers=hh)
    assert r.status_code == 403
    # Grant the passwords scope limited to the Support Agent role.
    r = client.post("/api/admin/delegated-groups", headers=h,
                    json={"name": "Helpdesk", "members": [helper["id"]],
                          "scopes": [{"scope": "passwords",
                                      "roles": ["Support Agent"]}]})
    assert r.status_code == 201
    # helper1 can reset the in-scope user's password...
    r = client.post(f"/api/admin/users/{agent['id']}/reset-password",
                    headers=hh)
    assert r.status_code == 200
    assert "temporary_password" in r.get_json()
    # ...but not the out-of-scope VP's.
    r = client.post(f"/api/admin/users/{boss['id']}/reset-password",
                    headers=hh)
    assert r.status_code == 403


def test_delegated_group_users_scope_role_restriction(client):
    h = login(client)
    agent = make_user(client, h, "agent2", role="Support Agent")
    boss = make_user(client, h, "boss2", role="VP Sales")
    mgr = make_user(client, h, "mgr2")
    mh = login_as(client, "mgr2")
    r = client.post("/api/admin/delegated-groups", headers=h,
                    json={"name": "User admins", "members": [mgr["id"]],
                          "scopes": [{"scope": "users",
                                      "roles": ["Support Agent"]}]})
    assert r.status_code == 201
    # In-scope role: can update the user.
    r = client.put(f"/api/admin/users/{agent['id']}", headers=mh,
                   json={"name": "Agent Two"})
    assert r.status_code == 200
    # Out-of-scope role: blocked.
    r = client.put(f"/api/admin/users/{boss['id']}", headers=mh,
                   json={"name": "Boss Two"})
    assert r.status_code == 403
    # Cannot create a user in an out-of-scope role either.
    r = client.post("/api/admin/users", headers=mh,
                    json={"username": "newvp", "name": "New Vp",
                          "email": "newvp@example.com",
                          "profile": "Standard User", "password": "UserPass1!",
                          "role": "VP Sales"})
    assert r.status_code == 403
    # Full admins are unaffected by group restrictions.
    r = client.put(f"/api/admin/users/{boss['id']}", headers=h,
                   json={"name": "Boss Two"})
    assert r.status_code == 200


def test_delegated_group_validation(client):
    h = login(client)
    r = client.post("/api/admin/delegated-groups", headers=h,
                    json={"name": "", "scopes": [{"scope": "users"}]})
    assert r.status_code == 422
    r = client.post("/api/admin/delegated-groups", headers=h,
                    json={"name": "Bad scope", "scopes": [{"scope": "nope"}]})
    assert r.status_code == 422
    r = client.post("/api/admin/delegated-groups", headers=h,
                    json={"name": "Bad role", "scopes": [{"scope": "users",
                                                          "roles": ["Nope"]}]})
    assert r.status_code == 422
    r = client.post("/api/admin/delegated-groups", headers=h,
                    json={"name": "Bad member", "members": ["ghost"],
                          "scopes": [{"scope": "users"}]})
    assert r.status_code == 422


def test_profile_default_division(client):
    h = login(client)
    d = make_division(client, h, "Central")
    r = client.put("/api/admin/profiles/Standard%20User", headers=h,
                   json={"default_division": "nope"})
    assert r.status_code == 422
    r = client.put("/api/admin/profiles/Standard%20User", headers=h,
                   json={"default_division": d["id"]})
    assert r.status_code == 200
    assert r.get_json()["default_division"] == d["id"]
    r = client.put("/api/admin/profiles/Standard%20User", headers=h,
                   json={"default_division": ""})
    assert r.status_code == 200
    assert r.get_json().get("default_division") in (None, "")
