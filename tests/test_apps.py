"""Tests for custom applications (App Manager): CRUD, visibility, tab
filtering, layout overrides, related-list config, and change-set support."""
import pytest

from forcelet import apps as apps_mod
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


def make_app(client, h, **kw):
    body = {"name": "Sales", "label": "Sales",
            "tabs": [{"kind": "object", "ref": "Account"},
                     {"kind": "object", "ref": "Opportunity"},
                     {"kind": "utility", "ref": "reports"}]}
    body.update(kw)
    r = client.post("/api/admin/apps", headers=h, json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()


# ------------------------------------------------------------------ CRUD
def test_app_crud(client):
    h = login(client)
    created = make_app(client, h)
    assert created["name"] == "Sales"
    assert [t["ref"] for t in created["tabs"]] == ["Account", "Opportunity", "reports"]

    r = client.get("/api/admin/apps", headers=h)
    assert len(r.get_json()) == 1

    r = client.put(f"/api/admin/apps/{created['id']}", headers=h,
                   json={"label": "Sales Cloud"})
    assert r.status_code == 200 and r.get_json()["label"] == "Sales Cloud"

    # duplicate name rejected
    r = client.post("/api/admin/apps", headers=h,
                    json={"name": "Sales", "label": "Dup", "tabs": []})
    assert r.status_code == 400

    # invalid tab rejected
    r = client.post("/api/admin/apps", headers=h,
                    json={"name": "Bad", "label": "Bad",
                          "tabs": [{"kind": "object", "ref": ""}]})
    assert r.status_code == 400

    r = client.delete(f"/api/admin/apps/{created['id']}", headers=h)
    assert r.status_code == 200
    assert client.get("/api/admin/apps", headers=h).get_json() == []


def test_legacy_mode_when_no_apps(client):
    h = login(client)
    d = client.get("/api/apps", headers=h).get_json()
    assert d["legacy"] is True and d["apps"] == []


def test_seed_apps(client):
    h = login(client)
    r = client.post("/api/admin/apps/seed", headers=h)
    assert r.get_json()["created"] == ["Sales", "Service"]
    # second seed is a no-op
    r = client.post("/api/admin/apps/seed", headers=h)
    assert r.get_json()["created"] == []
    d = client.get("/api/apps", headers=h).get_json()
    assert d["legacy"] is False
    labels = [a["label"] for a in d["apps"]]
    assert labels == ["Sales", "Service"]
    assert d["default_app_id"] == d["apps"][0]["id"]


# ------------------------------------------------------- visibility/profiles
def test_all_false_profile_access_stays_visible(client):
    # the App Manager submits a row per profile; all-false rows must not hide the app
    h = login(client)
    created = make_app(client, h)
    r = client.put(f"/api/admin/apps/{created['id']}", headers=h,
                   json={"profile_access": {
                       "System Administrator": {"visible": False, "default": False},
                       "Standard User": {"visible": False, "default": False}}})
    assert r.get_json()["profile_access"] == {}
    d = client.get("/api/apps", headers=h).get_json()
    assert [a["name"] for a in d["apps"]] == ["Sales"]


def test_profile_visibility_and_default(client):
    h = login(client)
    a1 = make_app(client, h, name="Sales", label="Sales")
    a2 = make_app(client, h, name="Service", label="Service",
                  profile_access={"System Administrator":
                                  {"visible": True, "default": True}})
    d = client.get("/api/apps", headers=h).get_json()
    # Sales has no profile_access -> visible to all; Service explicit
    assert {a["name"] for a in d["apps"]} == {"Sales", "Service"}
    assert d["default_app_id"] == a2["id"]

    # hide Sales from admins by granting it to another profile only
    client.put(f"/api/admin/apps/{a1['id']}", headers=h,
               json={"profile_access": {"Standard User":
                                        {"visible": True, "default": False}}})
    d = client.get("/api/apps", headers=h).get_json()
    assert [a["name"] for a in d["apps"]] == ["Service"]


def test_tabs_filtered_by_object_permission(client):
    h = login(client)
    # non-admin profile cannot read Opportunity: tab must be hidden
    store = client.application.mf_store
    prof = store.meta_get("mf_profiles", "Standard User") or {}
    perms = dict(prof.get("object_permissions") or {})
    perms["Opportunity"] = {"create": False, "read": False,
                            "edit": False, "delete": False}
    store.meta_put("mf_profiles", "Standard User",
                   {**prof, "object_permissions": perms})
    make_app(client, h)
    user = {"id": "u1", "profile": "Standard User"}
    sec = client.application.mf_security
    visible = apps_mod.visible_apps(store, sec, user)
    refs = [t["ref"] for t in visible[0]["tabs"]]
    assert "Account" in refs and "Opportunity" not in refs


# ------------------------------------------------------- layout & relationships
def test_layout_override_honored(client):
    h = login(client)
    created = make_app(client, h)
    # give the Account tab a layout override pointing at profile "Default"
    tabs = created["tabs"]
    tabs[0]["layout"] = {"profile": "Default", "record_type": "Default"}
    client.put(f"/api/admin/apps/{created['id']}", headers=h, json={"tabs": tabs})
    # sanity: override path returns 200 and a layout-shaped doc
    r = client.get(f"/api/layout/Account?app={created['id']}", headers=h)
    assert r.status_code == 200
    assert isinstance(r.get_json(), dict)
    # bogus app id -> falls back to the profile layout, still 200
    r = client.get("/api/layout/Account?app=nope", headers=h)
    assert r.status_code == 200


def test_related_lists_config_roundtrip(client):
    h = login(client)
    created = make_app(client, h)
    tabs = created["tabs"]
    tabs[0]["related_lists"] = ["Contact", "Opportunity"]
    r = client.put(f"/api/admin/apps/{created['id']}", headers=h, json={"tabs": tabs})
    assert r.get_json()["tabs"][0]["related_lists"] == ["Contact", "Opportunity"]
    d = client.get("/api/apps", headers=h).get_json()
    tab = [t for t in d["apps"][0]["tabs"] if t["ref"] == "Account"][0]
    assert tab["related_lists"] == ["Contact", "Opportunity"]


# ------------------------------------------------------- change sets
def test_app_in_changeset_inventory(client):
    h = login(client)
    make_app(client, h)
    r = client.get("/api/admin/change-sets/components/available", headers=h)
    assert r.status_code == 200
    refs = [c["ref"] for c in r.get_json()["app"]]
    assert "Sales" in refs


def test_app_packaged_and_deployed(client, tmp_path):
    h = login(client)
    make_app(client, h)
    from forcelet import changesets
    store = client.application.mf_store
    cs = changesets.create_changeset(store, "apps-cs", "", "admin")
    changesets.add_component(store, cs["id"], "app", "Sales")
    doc = changesets.export_changeset(store, client.application.mf_registry, cs["id"])
    assert doc["package"]["config"]["apps"][0]["name"] == "Sales"

    # deploy into a fresh org
    app2 = create_app(str(tmp_path / "t2.db"))
    c2 = app2.test_client()
    h2 = login(c2)
    store2 = app2.mf_store
    cs2 = changesets.import_changeset_doc(store2, doc, "admin")
    res = changesets.deploy_changeset(store2, app2.mf_registry, cs2["id"],
                                      {"id": "admin"})
    assert res["status"] == "Deployed", res.get("log")
    assert res["results"]["summary"]["config"]["apps"] == 1
    assert store2.config_all("mf_apps")[0]["name"] == "Sales"


def test_migrate_standard_app_tabs_adds_missing_fs_tabs(app):
    """Existing Service apps gain new standard tabs without losing customizations."""
    with app.app_context():
        store = app.mf_store
        # simulate a pre-Field-Service Service app with a custom tab
        old_service = {
            "name": "Service", "label": "Service", "icon": "", "color": "#0e7c3e",
            "sort_order": 2, "description": "old",
            "tabs": [{"kind": "object", "ref": "Account"},
                     {"kind": "object", "ref": "Case"},
                     {"kind": "object", "ref": "MyCustom__c"}],
        }
        store.config_put(apps_mod.APP_TABLE, old_service)
        # and a custom app that must not be touched
        store.config_put(apps_mod.APP_TABLE, {
            "name": "Custom", "label": "Custom",
            "tabs": [{"kind": "object", "ref": "Account"}]})

        changed = apps_mod.migrate_standard_app_tabs(store)
        assert changed == ["Service"]

        svc = apps_mod.get_app(store, [a["id"] for a in store.config_all(apps_mod.APP_TABLE)
                                       if a["name"] == "Service"][0])
        refs = [t["ref"] for t in svc["tabs"]]
        # old tabs keep their order, custom tab preserved
        assert refs[:3] == ["Account", "Case", "MyCustom__c"]
        # new Field Service standard tabs were appended
        for tab in ("ServiceTerritory", "ServiceResource", "WorkType",
                    "OperatingHours", "Skill", "ResourceAbsence", "ServiceCrew"):
            assert tab in refs

        # second run is a no-op
        assert apps_mod.migrate_standard_app_tabs(store) == []
