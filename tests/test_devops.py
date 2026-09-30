"""Tests for DevOps platform services: Bulk API 2.0 ingest, streaming events,
sandboxes/scratch orgs, source tracking, custom metadata types/settings,
managed packages, and external (OData) objects."""
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from forcelet import automation, devops
from forcelet.api import create_app
from forcelet.expressions import eval_expr
from helpers import login


@pytest.fixture()
def app(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def make_bulk_job(client, h, obj="Account", op="insert", csv_text=None, **kw):
    body = {"object": obj, "operation": op}
    body.update(kw)
    r = client.post("/api/bulk/jobs", headers=h, json=body)
    assert r.status_code == 201, r.get_json()
    job = r.get_json()
    if csv_text is not None:
        r = client.put(f"/api/bulk/jobs/{job['id']}", headers=h,
                       data=csv_text, content_type="text/csv")
        assert r.status_code == 200, r.get_json()
    return job


def wait_for_job(client, h, jid, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/bulk/jobs/{jid}", headers=h)
        job = r.get_json()
        if job["state"] in ("JobComplete", "Failed", "Aborted"):
            return job
        time.sleep(0.2)
    raise AssertionError(f"bulk job {jid} did not finish in {timeout}s")


# ------------------------------------------------------------------ Bulk API 2.0
def test_bulk_insert_update_delete(client):
    h = login(client)
    job = make_bulk_job(client, h, csv_text="Name\nBulk One\nBulk Two\n")
    r = client.patch(f"/api/bulk/jobs/{job['id']}", headers=h,
                     json={"state": "UploadComplete"})
    assert r.status_code == 200
    done = wait_for_job(client, h, job["id"])
    assert done["state"] == "JobComplete", done
    assert done["succeeded"] == 2 and done["failed"] == 0

    r = client.get(f"/api/bulk/jobs/{job['id']}/successful", headers=h)
    assert r.status_code == 200 and "sf__Id" in r.get_data(as_text=True)

    # update one row, delete the other
    ids = [l for l in r.get_data(as_text=True).splitlines()[1:]]
    first_id = ids[0].split(",")[-2]
    upd = make_bulk_job(client, h, op="update",
                        csv_text=f"Id,Name\n{first_id},Bulk One Renamed\n")
    client.patch(f"/api/bulk/jobs/{upd['id']}", headers=h,
                 json={"state": "UploadComplete"})
    done = wait_for_job(client, h, upd["id"])
    assert done["succeeded"] == 1

    second_id = ids[1].split(",")[-2]
    dele = make_bulk_job(client, h, op="delete", csv_text=f"Id\n{second_id}\n")
    client.patch(f"/api/bulk/jobs/{dele['id']}", headers=h,
                 json={"state": "UploadComplete"})
    done = wait_for_job(client, h, dele["id"])
    assert done["succeeded"] == 1

    r = client.get("/api/sobjects/Account", headers=h)
    names = [x["Name"] for x in r.get_json()]
    assert "Bulk One Renamed" in names and "Bulk Two" not in names


def test_bulk_failed_rows_report(client):
    h = login(client)
    # update without Id -> per-row failure, job still completes
    job = make_bulk_job(client, h, op="update", csv_text="Name\nNo Id Here\n")
    client.patch(f"/api/bulk/jobs/{job['id']}", headers=h,
                 json={"state": "UploadComplete"})
    done = wait_for_job(client, h, job["id"])
    assert done["state"] == "JobComplete"
    assert done["failed"] == 1 and done["succeeded"] == 0
    r = client.get(f"/api/bulk/jobs/{job['id']}/failed", headers=h)
    body = r.get_data(as_text=True)
    assert "sf__Error" in body and "missing Id" in body


def test_bulk_validation(client):
    h = login(client)
    r = client.post("/api/bulk/jobs", headers=h,
                    json={"object": "Nope", "operation": "insert"})
    assert r.status_code == 422
    r = client.post("/api/bulk/jobs", headers=h,
                    json={"object": "Account", "operation": "upsert"})
    assert r.status_code == 422  # externalIdFieldName required
    r = client.post("/api/bulk/jobs", headers=h,
                    json={"object": "Account", "operation": "insert"})
    jid = r.get_json()["id"]
    r = client.patch(f"/api/bulk/jobs/{jid}", headers=h,
                     json={"state": "UploadComplete"})
    assert r.status_code == 422  # no CSV uploaded
    assert "error" in r.get_json()


def test_bulk_abort(client):
    h = login(client)
    job = make_bulk_job(client, h, csv_text="Name\nX\n")
    r = client.delete(f"/api/bulk/jobs/{job['id']}", headers=h)
    assert r.status_code == 200
    assert r.get_json()["state"] == "Aborted"


# ------------------------------------------------------------------ streaming
def test_streaming_change_events(client):
    h = login(client)
    r = client.post("/api/sobjects/Account", headers=h, json={"Name": "Streamed"})
    assert r.status_code == 201
    rid = r.get_json()["Id"]
    events = devops.broker.events_since(0, {"/data/AccountChangeEvent"})
    assert any(e["payload"]["record_id"] == rid and e["payload"]["event"] == "create"
               for e in events)


def test_streaming_platform_event(client):
    h = login(client)
    r = client.post("/api/streaming/events", headers=h,
                    json={"name": "OrderShipped", "payload": {"order": "42"}})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["topic"] == "/event/OrderShipped__e"
    events = devops.broker.events_since(0, {"/event/OrderShipped__e"})
    assert events and events[-1]["payload"]["payload"] == {"order": "42"}


def test_streaming_rejects_bad_name(client):
    h = login(client)
    r = client.post("/api/streaming/events", headers=h,
                    json={"name": "no spaces!", "payload": {}})
    assert r.status_code == 422


# ------------------------------------------------------------------ sandboxes
def _seed_account(app):
    store = app.mf_store
    rid = store.insert("Account", {"Name": "Sandbox Seed"})
    store._commit()
    return rid


def test_sandbox_kinds(client, app, tmp_path, monkeypatch):
    monkeypatch.setenv("FORCELET_SANDBOX_DIR", str(tmp_path / "sbx"))
    h = login(client)
    _seed_account(app)
    store = app.mf_store
    admin = app.mf_security.get_user_by_username("admin")

    full = devops.create_sandbox(store, admin, "fullcopy", kind="full")
    assert full["kind"] == "full" and os.path.exists(full["db_path"])
    con = sqlite3.connect(full["db_path"])
    n = con.execute('SELECT COUNT(*) FROM "sobj_Account"').fetchone()[0]
    con.close()
    assert n >= 1

    devbox = devops.create_sandbox(store, admin, "devcopy", kind="developer")
    con = sqlite3.connect(devbox["db_path"])
    n = con.execute('SELECT COUNT(*) FROM "sobj_Account"').fetchone()[0]
    objs = con.execute(
        "SELECT COUNT(*) FROM mf_objects").fetchone()[0]
    con.close()
    assert n == 0 and objs > 0  # metadata only

    # duplicate name rejected
    with pytest.raises(ValueError):
        devops.create_sandbox(store, admin, "fullcopy")

    # refresh + delete via API
    r = client.post(f"/api/admin/sandboxes/{devbox['id']}/refresh", headers=h)
    assert r.status_code == 200
    r = client.delete(f"/api/admin/sandboxes/{devbox['id']}", headers=h)
    assert r.status_code == 200
    assert not os.path.exists(devbox["db_path"])


def test_scratch_org_expiry(client, app, tmp_path, monkeypatch):
    monkeypatch.setenv("FORCELET_SANDBOX_DIR", str(tmp_path / "sbx"))
    store, admin = app.mf_store, app.mf_security.get_user_by_username("admin")
    sb = devops.create_sandbox(store, admin, "scratch1", kind="developer",
                               scratch=True, expires_in_days=1)
    assert sb["scratch"] and sb["expires_at"]
    # force expiry then list -> pruned
    sb["expires_at"] = "2000-01-01T00:00:00"
    store.config_put(devops.SANDBOX_TABLE, sb)
    remaining = devops.list_sandboxes(store)
    assert all(s["id"] != sb["id"] for s in remaining)


# ------------------------------------------------------------------ source tracking
def test_source_tracking(client):
    h = login(client)
    since = devops.source_changes(client.application.mf_store)["server_time"]
    time.sleep(1.1)  # audit timestamps have 1s resolution
    r = client.post("/api/admin/metadata-types", headers=h, json={
        "label": "Region Map", "api_name": "RegionMap__mdt",
        "fields": [{"name": "Code", "label": "Code", "type": "Text"}]})
    assert r.status_code == 201, r.get_json()
    r = client.get("/api/admin/source/changes", headers=h,
                   query_string={"since": since})
    assert r.status_code == 200
    changes = r.get_json()["changes"]
    assert any(c["type"] == "CustomMetadataType" and c["name"] == "RegionMap__mdt"
               for c in changes)


# ------------------------------------------------------------------ custom metadata & settings
def make_cmdt(client, h, api_name="Tier__mdt"):
    r = client.post("/api/admin/metadata-types", headers=h, json={
        "label": "Tier", "api_name": api_name, "description": "Support tiers",
        "fields": [
            {"name": "Discount", "label": "Discount", "type": "Percent"},
            {"name": "SlaHours", "label": "SLA Hours", "type": "Number"},
        ]})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def test_cmdt_crud_and_formula_access(client):
    h = login(client)
    t = make_cmdt(client, h)
    r = client.post(f"/api/admin/metadata-types/{t['id']}/records", headers=h,
                    json={"developer_name": "Gold",
                          "values": {"Discount": 15, "SlaHours": 4}})
    assert r.status_code == 201, r.get_json()
    rec = r.get_json()
    assert rec["values"] == {"Discount": 15, "SlaHours": 4}

    # bad value rejected
    r = client.post(f"/api/admin/metadata-types/{t['id']}/records", headers=h,
                    json={"developer_name": "Bad", "values": {"Discount": "lots"}})
    assert r.status_code == 422

    # readable from the expression engine ($CustomMetadata)
    val = eval_expr({"custom_metadata": {"type": "Tier__mdt", "record": "Gold",
                                        "field": "SlaHours"}}, {})
    assert val == 4

    # update + delete
    r = client.put(f"/api/admin/metadata-records/{rec['id']}", headers=h,
                   json={"developer_name": "Gold", "values": {"Discount": 20}})
    assert r.status_code == 200
    assert r.get_json()["values"]["Discount"] == 20
    r = client.delete(f"/api/admin/metadata-types/{t['id']}", headers=h)
    assert r.status_code == 200


def test_cmdt_api_name_rules(client):
    h = login(client)
    r = client.post("/api/admin/metadata-types", headers=h, json={
        "label": "Nope", "api_name": "Nope__c",
        "fields": [{"name": "F", "label": "F", "type": "Text"}]})
    assert r.status_code == 422


def test_custom_settings(client):
    h = login(client)
    r = client.post("/api/admin/custom-settings", headers=h, json={
        "name": "OrgDefaults", "label": "Org Defaults",
        "values": {"default_priority": "High", "sla_hours": 8}})
    assert r.status_code == 201, r.get_json()
    val = eval_expr({"custom_setting": {"name": "OrgDefaults",
                                       "field": "sla_hours"}}, {})
    assert val == 8
    # upsert overwrites
    r = client.post("/api/admin/custom-settings", headers=h, json={
        "name": "OrgDefaults", "values": {"sla_hours": 4}})
    assert r.status_code == 200
    assert r.get_json()["values"] == {"sla_hours": 4}


# ------------------------------------------------------------------ managed packages
def test_managed_package_install_and_upgrade(tmp_path):
    src = create_app(str(tmp_path / "src.db"))
    store, registry = src.mf_store, src.mf_registry
    admin = src.mf_security.get_user_by_username("admin")
    devops.create_cmdt(store, admin, {
        "label": "Pkg", "api_name": "Pkg__mdt",
        "fields": [{"name": "F", "label": "F", "type": "Text"}]})
    devops.create_cmdt_record(store, admin,
                              devops.get_cmdt(store, "Pkg__mdt")["id"],
                              "One", {"F": "v1"})

    pkg = automation.build_package(store, registry, namespace="acme",
                                   version="1.1", managed=True)
    assert pkg["namespace"] == "acme" and pkg["managed"] is True
    assert pkg["custom_metadata"]["types"][0]["api_name"] == "Pkg__mdt"

    dst = create_app(str(tmp_path / "dst.db"))
    dstore, dregistry = dst.mf_store, dst.mf_registry
    dadmin = dst.mf_security.get_user_by_username("admin")
    summary = automation.import_package(dstore, dregistry, pkg, dadmin)
    assert summary["installed_package"]["version"] == "1.1"
    assert devops.get_cmdt(dstore, "Pkg__mdt") is not None
    assert devops.custom_metadata_value(dstore, "Pkg__mdt", "One", "F") == "v1"

    # reinstall of same version rejected; downgrade rejected
    with pytest.raises(ValueError):
        automation.import_package(dstore, dregistry, pkg, dadmin)
    old = dict(pkg, version="1.0")
    with pytest.raises(ValueError):
        automation.import_package(dstore, dregistry, old, dadmin)

    # upgrade works
    new = automation.build_package(store, registry, namespace="acme", version="1.2")
    summary = automation.import_package(dstore, dregistry, new, dadmin)
    assert summary["installed_package"]["version"] == "1.2"


def test_installed_packages_api(client):
    h = login(client)
    store = client.application.mf_store
    admin = client.application.mf_security.get_user_by_username("admin")
    pkg = automation.build_package(store, client.application.mf_registry,
                                   namespace="widgets", version="2.0")
    automation.import_package(store, client.application.mf_registry, pkg, admin)
    r = client.get("/api/admin/packages/installed", headers=h)
    assert any(p["namespace"] == "widgets" for p in r.get_json())
    r = client.delete("/api/admin/packages/installed/widgets", headers=h)
    assert r.status_code == 200
    r = client.get("/api/admin/packages/installed", headers=h)
    assert all(p["namespace"] != "widgets" for p in r.get_json())


# ------------------------------------------------------------------ external objects
class _ODataHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"value": [{"Id": "1", "Name": "External Widget"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture()
def odata_server():
    srv = HTTPServer(("127.0.0.1", 0), _ODataHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}/odata"
    srv.shutdown()


def test_external_object_query(client, odata_server):
    h = login(client)
    r = client.post("/api/admin/external-objects", headers=h, json={
        "label": "Products", "api_name": "Products__x",
        "odata_url": odata_server, "entity_set": "Products",
        "key_field": "Id"})
    assert r.status_code == 201, r.get_json()
    r = client.get("/api/xdata/Products__x", headers=h,
                   query_string={"$top": "5"})
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert data["records"][0]["Name"] == "External Widget"


def test_external_object_validation(client):
    h = login(client)
    r = client.post("/api/admin/external-objects", headers=h, json={
        "label": "Bad", "api_name": "Bad__c",
        "odata_url": "http://x", "entity_set": "Things"})
    assert r.status_code == 422
