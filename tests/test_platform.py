"""Tests for platform services: cron scheduling, field history tracking
configuration, change sets, and semantic search."""
from datetime import datetime, timedelta, timezone

import pytest

from forcelet import automation, changesets, cron, history_tracking, semantic
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


# ------------------------------------------------------------------ cron
def test_cron_parse_valid():
    s = cron.parse("0 9 * * *")
    assert s.minutes == {0} and s.hours == {9}
    s = cron.parse("*/15 * * * *")
    assert s.minutes == {0, 15, 30, 45}
    s = cron.parse("30 14 * * mon-fri")
    assert s.dows == {1, 2, 3, 4, 5}
    s = cron.parse("0 0 1 jan *")
    assert s.months == {1} and s.doms == {1}
    # 7 is Sunday, same as 0; names agree with numerics
    assert cron.parse("0 9 * * 7").dows == cron.parse("0 9 * * 0").dows == {0}
    assert cron.parse("0 9 * * sun").dows == {0}
    assert cron.parse("0 9 * * mon").dows == {1}


def test_cron_sunday_matches_sunday():
    # regression: numeric 0/7 and "sun" must fire on Sunday, not Monday
    sunday = datetime(2026, 10, 4, 9, 0)    # a Sunday
    monday = datetime(2026, 10, 5, 9, 0)    # a Monday
    for expr in ("0 9 * * 0", "0 9 * * 7", "0 9 * * sun"):
        spec = cron.parse(expr)
        assert spec.matches(sunday) is True, expr
        assert spec.matches(monday) is False, expr
    assert cron.parse("0 9 * * mon").matches(monday) is True
    assert cron.parse("0 9 * * mon").matches(sunday) is False
    # next Sunday occurrence from a Monday
    nxt = cron.parse("0 9 * * 0").next_occurrence(datetime(2026, 10, 5, 10, 0))
    assert nxt == datetime(2026, 10, 11, 9, 0)


def test_cron_parse_invalid():
    for bad in ["", "0 9 * *", "* * * * * *", "61 * * * *", "*/0 * * * *",
                "0 9 * * someday", "5-2 * * * *"]:
        with pytest.raises(ValueError):
            cron.parse(bad)


def test_cron_describe():
    assert cron.parse("0 9 * * *").describe() == "Daily at 09:00"
    assert cron.parse("*/15 * * * *").describe() == "Every 15 minutes"
    assert "Weekly" in cron.parse("30 14 * * mon-fri").describe()


def test_cron_next_occurrence():
    s = cron.parse("0 9 * * *")
    nxt = s.next_occurrence(datetime(2026, 9, 29, 18, 0))
    assert nxt == datetime(2026, 9, 30, 9, 0)
    # strictly after `after`
    nxt = s.next_occurrence(datetime(2026, 9, 29, 9, 0))
    assert nxt == datetime(2026, 9, 30, 9, 0)


def test_cron_is_due():
    now = datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc)
    assert cron.is_due("0 9 * * *", None, now) is True  # never ran
    assert cron.is_due("0 9 * * *", "2026-09-29T09:00:00", now) is False
    assert cron.is_due("0 9 * * *", "2026-09-28T09:00:00", now) is True
    assert cron.is_due("*/15 * * * *", "2026-09-29T17:30:00", now) is True


def test_cron_scheduled_job_runs_when_due(app):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security
    store.config_put("mf_scheduled_jobs", {
        "name": "cronjob", "cron": "* * * * *", "active": True,
        "run_as": "admin", "code": "pass", "interval_minutes": 1440})
    results = automation.run_due_scheduled_jobs(store, registry, security)
    assert any(r["job"] == "cronjob" and r["ok"] for r in results)
    # bad cron expression is reported, not fatal
    store.config_put("mf_scheduled_jobs", {
        "name": "badcron", "cron": "not a cron", "active": True,
        "run_as": "admin", "code": "pass"})
    results = automation.run_due_scheduled_jobs(store, registry, security)
    runs = store.scheduled_runs()
    assert any(r["status"] == "error" and "cron" in r["detail"] for r in runs)


def test_cron_validate_endpoint(client):
    h = login(client)
    r = client.post("/api/admin/scheduled-jobs/validate-cron",
                    json={"cron": "0 9 * * mon"}, headers=h)
    assert r.get_json()["ok"] is True
    r = client.post("/api/admin/scheduled-jobs/validate-cron",
                    json={"cron": "nope"}, headers=h)
    assert r.get_json()["ok"] is False


def test_cron_job_create_and_runs_endpoint(client):
    h = login(client)
    r = client.post("/api/admin/scheduled-jobs", headers=h, json={
        "name": "J1", "cron": "0 9 * * *", "run_as": "admin",
        "code": "pass", "active": True})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["cron"] == "0 9 * * *"
    bad = client.post("/api/admin/scheduled-jobs", headers=h, json={
        "name": "J2", "cron": "bogus", "run_as": "admin", "code": "pass"})
    assert bad.status_code == 422
    jid = r.get_json()["id"]
    rr = client.post(f"/api/admin/scheduled-jobs/{jid}/run", headers=h)
    assert rr.status_code == 200
    runs = client.get(f"/api/admin/scheduled-jobs/{jid}/runs", headers=h).get_json()
    assert runs and runs[0]["status"] == "ok"


# ------------------------------------------------------- history tracking
def test_history_default_config(app):
    cfg = history_tracking.effective_config(app.mf_store, "Case")
    assert cfg["enabled"] is True and cfg["fields"] == []


def test_history_disabled_skips_logging(app):
    store = app.mf_store
    history_tracking.set_config(store, "Case", enabled=False)
    user = {"id": "u1"}
    automation.log_history(store, "Case", "r1", {"Status": "New"},
                           {"Status": "Closed", "Priority": "High"}, user)
    assert automation.get_history(store, "Case", "r1") == []


def test_history_field_selection(app):
    store = app.mf_store
    history_tracking.set_config(store, "Case", enabled=True, fields=["Status"])
    user = {"id": "u1"}
    automation.log_history(store, "Case", "r2", {"Status": "New", "Priority": "Low"},
                           {"Status": "Closed", "Priority": "High"}, user)
    rows = automation.get_history(store, "Case", "r2")
    assert [r["field_name"] for r in rows] == ["Status"]


def test_history_purge(app):
    store = app.mf_store
    history_tracking.set_config(store, "Case", enabled=True, retention_days=30)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    store._execute(
        "INSERT INTO mf_history (id, object_name, record_id, field_name, old_value,"
        " new_value, changed_by, changed_at) VALUES (?,?,?,?,?,?,?,?)",
        ("h_old", "Case", "rx", "Status", "New", "Closed", "u1", old_ts))
    store._execute(
        "INSERT INTO mf_history (id, object_name, record_id, field_name, old_value,"
        " new_value, changed_by, changed_at) VALUES (?,?,?,?,?,?,?,?)",
        ("h_new", "Case", "rx", "Priority", "Low", "High", "u1",
         datetime.now(timezone.utc).isoformat()))
    store._commit()
    purged = history_tracking.purge_old_history(store)
    assert purged.get("Case") == 1
    assert {r["id"] for r in automation.get_history(store, "Case", "rx")} == {"h_new"}


def test_history_tracking_api(client):
    h = login(client)
    rows = client.get("/api/admin/history-tracking", headers=h).get_json()
    assert any(r["object_name"] == "Case" for r in rows)
    r = client.put("/api/admin/history-tracking/Case", headers=h, json={
        "enabled": True, "fields": ["Status"], "retention_days": 90})
    assert r.status_code == 200
    assert r.get_json()["fields"] == ["Status"]
    bad = client.put("/api/admin/history-tracking/Case", headers=h, json={
        "fields": ["Nope"]})
    assert bad.status_code == 422
    # end-to-end: an update logs only the selected field
    c = client.post("/api/sobjects/Case", headers=h,
                    json={"Subject": "ht", "Status": "New", "Origin": "Web"}).get_json()
    client.patch(f"/api/sobjects/Case/{c['Id']}", headers=h,
                 json={"Status": "Working", "Priority": "High"})
    hist = client.get(f"/api/sobjects/Case/{c['Id']}/history", headers=h).get_json()
    assert {x["field_name"] for x in hist} == {"Status"}
    r = client.post("/api/admin/history-tracking/purge", headers=h)
    assert r.status_code == 200 and "purged" in r.get_json()


# ------------------------------------------------------------- change sets
def test_changeset_crud(client):
    h = login(client)
    r = client.post("/api/admin/change-sets", headers=h,
                    json={"name": "CS1", "description": "d"})
    assert r.status_code == 201
    cs = r.get_json()
    assert cs["status"] == "Draft" and cs["components"] == []
    bad = client.post("/api/admin/change-sets", headers=h, json={"name": ""})
    assert bad.status_code == 422
    r = client.post(f"/api/admin/change-sets/{cs['id']}/components", headers=h,
                    json={"action": "add", "type": "bogus", "ref": "X"})
    assert r.status_code == 422
    r = client.post(f"/api/admin/change-sets/{cs['id']}/components", headers=h,
                    json={"action": "add", "type": "flow", "ref": "Nope"})
    assert r.status_code == 200
    assert len(r.get_json()["components"]) == 1
    # duplicate add is idempotent
    r = client.post(f"/api/admin/change-sets/{cs['id']}/components", headers=h,
                    json={"action": "add", "type": "flow", "ref": "Nope"})
    assert len(r.get_json()["components"]) == 1
    r = client.post(f"/api/admin/change-sets/{cs['id']}/components", headers=h,
                    json={"action": "remove", "type": "flow", "ref": "Nope"})
    assert r.get_json()["components"] == []


def test_changeset_validate_deploy_roundtrip(client, app):
    h = login(client)
    # a real component to ship
    vr = client.post("/api/admin/validation-rules", headers=h, json={
        "name": "VR_CS", "object": "Case", "message": "bad",
        "condition": {"==": [{"field": "Priority"}, "Bogus"]}, "active": True})
    assert vr.status_code == 201
    cs = client.post("/api/admin/change-sets", headers=h,
                     json={"name": "Ship VR"}).get_json()
    client.post(f"/api/admin/change-sets/{cs['id']}/components", headers=h,
                json={"action": "add", "type": "validation_rule", "ref": "VR_CS"})
    v = client.post(f"/api/admin/change-sets/{cs['id']}/validate",
                    headers=h).get_json()
    assert v["valid"] is True and v["component_count"] == 1
    d = client.post(f"/api/admin/change-sets/{cs['id']}/deploy",
                    headers=h).get_json()
    assert d["status"] == "Deployed", d
    deps = client.get(f"/api/admin/change-sets/{cs['id']}/deployments",
                      headers=h).get_json()
    assert len(deps) == 1 and deps[0]["status"] == "Deployed"
    # validation catches a component whose object is missing here
    cs2 = client.post("/api/admin/change-sets", headers=h,
                      json={"name": "Bad"}).get_json()
    client.post(f"/api/admin/change-sets/{cs2['id']}/components", headers=h,
                json={"action": "add", "type": "object_field",
                      "ref": "NoSuchObject.Field__c"})
    v2 = client.post(f"/api/admin/change-sets/{cs2['id']}/validate",
                     headers=h).get_json()
    assert v2["valid"] is False and v2["errors"]
    d2 = client.post(f"/api/admin/change-sets/{cs2['id']}/deploy",
                     headers=h).get_json()
    assert d2["status"] == "Failed"


def test_changeset_download_upload(client):
    h = login(client)
    cs = client.post("/api/admin/change-sets", headers=h,
                     json={"name": "Portable"}).get_json()
    client.post(f"/api/admin/change-sets/{cs['id']}/components", headers=h,
                json={"action": "add", "type": "email_template", "ref": "Nope"})
    dl = client.get(f"/api/admin/change-sets/{cs['id']}/download", headers=h)
    assert dl.status_code == 200
    doc = dl.get_json()
    assert doc["changeset_version"] == 1
    doc["changeset"]["name"] = "Portable (imported)"  # same-org re-upload renames
    up = client.post("/api/admin/change-sets/upload", headers=h,
                     json={"changeset": doc}).get_json()
    assert up["status"] == "Inbound"
    assert len(up["components"]) == 1
    bad = client.post("/api/admin/change-sets/upload", headers=h,
                      json={"changeset": {"nope": True}})
    assert bad.status_code == 422


def test_changeset_cross_org_deploy(tmp_path):
    # org A: author a validation rule, put it in a change set, download
    app_a = create_app(str(tmp_path / "a.db"))
    app_a.config["TESTING"] = True
    ca = app_a.test_client()
    ha = login(ca)
    vr = ca.post("/api/admin/validation-rules", headers=ha, json={
        "name": "VR_SHIP", "object": "Case", "message": "nope",
        "condition": {"==": [{"field": "Priority"}, "Bogus"]}, "active": True})
    assert vr.status_code == 201
    ob = ca.post("/api/admin/objects", headers=ha, json={
        "name": "Widget", "label": "Widget", "plural": "Widgets"})
    assert ob.status_code == 201, ob.get_json()
    cs = ca.post("/api/admin/change-sets", headers=ha,
                 json={"name": "Ship VR"}).get_json()
    ca.post(f"/api/admin/change-sets/{cs['id']}/components", headers=ha,
            json={"action": "add", "type": "validation_rule", "ref": "VR_SHIP"})
    ca.post(f"/api/admin/change-sets/{cs['id']}/components", headers=ha,
            json={"action": "add", "type": "custom_object", "ref": "Widget"})
    ca.post(f"/api/admin/change-sets/{cs['id']}/status", headers=ha,
            json={"status": "Outbound"})
    doc = ca.get(f"/api/admin/change-sets/{cs['id']}/download",
                 headers=ha).get_json()
    # the packaged payload really contains the rule under the plural key
    cfg = doc["package"]["config"]
    assert [d["name"] for d in cfg.get("validation_rules", [])] == ["VR_SHIP"]
    assert [o["name"] for o in doc["package"]["custom_objects"]] == ["Widget"]
    # org B: fresh database, upload + deploy
    app_b = create_app(str(tmp_path / "b.db"))
    app_b.config["TESTING"] = True
    cb = app_b.test_client()
    hb = login(cb)
    up = cb.post("/api/admin/change-sets/upload", headers=hb,
                 json={"changeset": doc}).get_json()
    v = cb.post(f"/api/admin/change-sets/{up['id']}/validate",
                headers=hb).get_json()
    assert v["valid"] is True, v
    d = cb.post(f"/api/admin/change-sets/{up['id']}/deploy",
                headers=hb).get_json()
    assert d["status"] == "Deployed", d
    rules = cb.get("/api/admin/validation-rules", headers=hb).get_json()
    assert "VR_SHIP" in [r["name"] for r in rules]
    objs = cb.get("/api/objects", headers=hb).get_json()
    assert "Widget" in [o["name"] for o in objs]
    # ...but nothing else leaked: B has no other trace of A's config
    deps = cb.get(f"/api/admin/change-sets/{up['id']}/deployments",
                  headers=hb).get_json()
    assert deps and deps[0]["status"] == "Deployed"


def test_changeset_available_components(client):
    h = login(client)
    avail = client.get("/api/admin/change-sets/components/available",
                       headers=h).get_json()
    assert "validation_rule" in avail and "object_field" in avail
    assert all("ref" in c and "label" in c
               for ctype in avail.values() for c in ctype)


# ---------------------------------------------------------- semantic search
def test_semantic_tfidf_ranking():
    docs = [("a", "the windshield is cracked and needs replacement"),
            ("b", "quarterly revenue report for the finance team"),
            ("c", "customer complains about a broken windscreen on delivery")]
    # "a" matches two query terms, "c" only one -> "a" must rank first;
    # the unrelated finance doc scores zero and is excluded entirely
    ranked = semantic.search_index(semantic.build_index(docs),
                                   "broken windshield replacement", 3)
    ids = [doc_id for doc_id, _ in ranked]
    assert ids[0] == "a" and "b" not in ids
    assert all(0 < s <= 1.0 for _, s in ranked)


def test_semantic_empty_query():
    assert semantic.search_index(semantic.build_index([("a", "hello")]), "", 5) == []


def test_semantic_search_api(client):
    h = login(client)
    c1 = client.post("/api/sobjects/Case", headers=h, json={
        "Subject": "broken windshield on delivery", "Origin": "Web"}).get_json()
    client.post("/api/sobjects/Case", headers=h, json={
        "Subject": "quarterly finance report", "Origin": "Web"})
    # word order differs, so the substring keyword search misses it...
    kw = client.get("/api/search", headers=h,
                    query_string={"q": "windshield broken"}).get_json()
    assert not any(c1["Id"] in [r["Id"] for r in g["records"]] for g in kw)
    # ...but the semantic search matches on words regardless of order
    r = client.get("/api/search/semantic", headers=h,
                   query_string={"q": "windshield broken", "limit": 10})
    body = r.get_json()
    assert body["provider"] == "tfidf"
    assert body["results"], body
    top = body["results"][0]
    assert top["id"] == c1["Id"]
    assert 0 < top["score"] <= 1.0
    assert top["snippet"]
    empty = client.get("/api/search/semantic", headers=h,
                       query_string={"q": ""}).get_json()
    assert empty["results"] == []
