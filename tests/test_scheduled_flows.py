"""Tests for scheduled flows: schedule-triggered flow automation."""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forcelet import automation
from forcelet.api import create_app
from test_forcelet import login


@pytest.fixture()
def app_client():
    db = tempfile.mktemp(suffix=".db")
    app = create_app(db)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, app
    os.unlink(db)


def _admin(app_client):
    c, _app = app_client
    return login(c, "admin"), c


def _make_flow(c, h, **kw):
    body = {"name": "Nightly touch", "object": "Task", "trigger": "scheduled",
            "schedule": {"frequency": "daily", "time": "02:00"},
            "condition": {"==": [{"field": "Subject"}, "Needs touch"]},
            "actions": [{"type": "set_fields", "object": "Task",
                         "record_id": "{{Trigger.Id}}",
                         "fields": {"Subject": "Touched by flow"}}],
            "active": True}
    body.update(kw)
    r = c.post("/api/admin/flows", headers=h, json=body)
    return r


def _make_task(store, subject):
    return store.insert("Task", {"Subject": subject, "Status": "Not Started",
                                 "owner_id": "admin", "created_by": "admin"})


# ------------------------------------------------------------ validation
def test_schedule_validation_rejects_bad_input(app_client):
    c, _app = app_client
    h = login(c, "admin")
    # bad time
    r = _make_flow(c, h, schedule={"frequency": "daily", "time": "25:00"})
    assert r.status_code == 422, r.get_json()
    # bad frequency
    r = _make_flow(c, h, schedule={"frequency": "hourly", "time": "02:00"})
    assert r.status_code == 422, r.get_json()
    # bad day_of_week
    r = _make_flow(c, h, schedule={"frequency": "weekly", "time": "02:00",
                                   "day_of_week": 9})
    assert r.status_code == 422, r.get_json()
    # unknown object
    r = _make_flow(c, h, object="Nope")
    assert r.status_code == 422, r.get_json()
    # valid: created with a next_run
    r = _make_flow(c, h)
    assert r.status_code == 201, r.get_json()
    assert r.get_json().get("next_run")


def test_criteria_alias_normalized(app_client):
    c, _app = app_client
    h = login(c, "admin")
    body = {"name": "Alias flow", "object": "Task", "trigger": "scheduled",
            "schedule": {"frequency": "daily", "time": "02:00"},
            "criteria": {"==": [{"field": "Subject"}, "x"]},
            "actions": [], "active": True}
    r = c.post("/api/admin/flows", headers=h, json=body)
    assert r.status_code == 201, r.get_json()
    saved = r.get_json()
    assert saved.get("condition") == {"==": [{"field": "Subject"}, "x"]}
    assert "criteria" not in saved


# ------------------------------------------------------------ execution
def test_due_flow_executes_on_matching_records_only(app_client):
    c, app = app_client
    h, _ = _admin(app_client)
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security
    match_id = _make_task(store, "Needs touch")
    other_id = _make_task(store, "Leave alone")
    r = _make_flow(c, h)
    fid = r.get_json()["id"]
    flow = store.config_get("mf_flows", fid)
    flow["next_run"] = "2000-01-01T00:00:00"
    store.config_put("mf_flows", flow)

    results = automation.run_due_scheduled_flows(store, registry, security)
    assert len(results) == 1
    res = results[0]
    assert res["matched"] == 1 and res["executed"] == 1 and res["errors"] == 0
    assert store.get("Task", match_id)["Subject"] == "Touched by flow"
    assert store.get("Task", other_id)["Subject"] == "Leave alone"


def test_non_due_flow_does_not_run(app_client):
    c, app = app_client
    h, _ = _admin(app_client)
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security
    tid = _make_task(store, "Needs touch")
    r = _make_flow(c, h)
    fid = r.get_json()["id"]
    flow = store.config_get("mf_flows", fid)
    flow["next_run"] = (datetime.now(timezone.utc)
                        + timedelta(days=1)).isoformat(timespec="seconds")
    store.config_put("mf_flows", flow)

    assert automation.run_due_scheduled_flows(store, registry, security) == []
    assert store.get("Task", tid)["Subject"] == "Needs touch"


def test_next_run_advances_after_run(app_client):
    c, app = app_client
    h, _ = _admin(app_client)
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security
    _make_task(store, "Needs touch")
    r = _make_flow(c, h)
    fid = r.get_json()["id"]
    flow = store.config_get("mf_flows", fid)
    flow["next_run"] = "2000-01-01T00:00:00"
    store.config_put("mf_flows", flow)

    automation.run_due_scheduled_flows(store, registry, security)
    advanced = store.config_get("mf_flows", fid)["next_run"]
    assert datetime.fromisoformat(advanced) > datetime.now(timezone.utc)
    # second pass: nothing due anymore (idempotent claim)
    assert automation.run_due_scheduled_flows(store, registry, security) == []


def test_zero_matches_is_noop(app_client):
    c, app = app_client
    h, _ = _admin(app_client)
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security
    _make_task(store, "Something else")
    r = _make_flow(c, h)
    fid = r.get_json()["id"]
    flow = store.config_get("mf_flows", fid)
    flow["next_run"] = "2000-01-01T00:00:00"
    store.config_put("mf_flows", flow)

    results = automation.run_due_scheduled_flows(store, registry, security)
    assert len(results) == 1
    assert results[0]["matched"] == 0 and results[0]["executed"] == 0
    assert results[0]["errors"] == 0


def test_inactive_flow_does_not_run(app_client):
    c, app = app_client
    h, _ = _admin(app_client)
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security
    tid = _make_task(store, "Needs touch")
    r = _make_flow(c, h, active=False)
    fid = r.get_json()["id"]
    flow = store.config_get("mf_flows", fid)
    flow["next_run"] = "2000-01-01T00:00:00"
    store.config_put("mf_flows", flow)

    assert automation.run_due_scheduled_flows(store, registry, security) == []
    assert store.get("Task", tid)["Subject"] == "Needs touch"


# ------------------------------------------------------------ next_run math
def test_compute_next_run_daily():
    # time already passed today -> tomorrow
    now = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
    nxt = automation.compute_next_run({"frequency": "daily", "time": "09:00"},
                                      now)
    assert nxt == "2026-10-01T09:00:00+00:00"
    # time still ahead today -> today
    nxt = automation.compute_next_run({"frequency": "daily", "time": "11:00"},
                                      now)
    assert nxt == "2026-09-30T11:00:00+00:00"


def test_compute_next_run_weekly():
    # 2026-09-30 is a Wednesday (weekday 2)
    now = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
    assert now.weekday() == 2
    # next Monday (dow 0) -> 2026-10-05
    nxt = automation.compute_next_run(
        {"frequency": "weekly", "time": "09:00", "day_of_week": 0}, now)
    assert nxt == "2026-10-05T09:00:00+00:00"
    # same day later time -> today
    nxt = automation.compute_next_run(
        {"frequency": "weekly", "time": "11:00", "day_of_week": 2}, now)
    assert nxt == "2026-09-30T11:00:00+00:00"
    # same day earlier time -> next week
    nxt = automation.compute_next_run(
        {"frequency": "weekly", "time": "09:00", "day_of_week": 2}, now)
    assert nxt == "2026-10-07T09:00:00+00:00"


# ------------------------------------------------------------ PATCH behavior
def test_patch_recomputes_next_run_only_on_schedule_change(app_client):
    c, app = app_client
    h, _ = _admin(app_client)
    store = app.mf_store
    r = _make_flow(c, h)
    assert r.status_code == 201
    fid = r.get_json()["id"]
    before = store.config_get("mf_flows", fid)["next_run"]

    # unrelated edit keeps next_run
    r = c.patch(f"/api/admin/flows/{fid}", headers=h,
                json={"name": "Renamed"})
    assert r.status_code == 200, r.get_json()
    assert store.config_get("mf_flows", fid)["next_run"] == before

    # schedule change recomputes next_run
    r = c.patch(f"/api/admin/flows/{fid}", headers=h,
                json={"schedule": {"frequency": "daily", "time": "03:30"}})
    assert r.status_code == 200, r.get_json()
    after = store.config_get("mf_flows", fid)
    assert "T03:30:00" in after["next_run"]

    # bad schedule on PATCH is rejected
    r = c.patch(f"/api/admin/flows/{fid}", headers=h,
                json={"schedule": {"frequency": "daily", "time": "nope"}})
    assert r.status_code == 422


def test_scheduler_run_once_includes_flows(app_client):
    from forcelet import scheduler
    c, app = app_client
    h, _ = _admin(app_client)
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security
    tid = _make_task(store, "Needs touch")
    r = _make_flow(c, h)
    fid = r.get_json()["id"]
    flow = store.config_get("mf_flows", fid)
    flow["next_run"] = "2000-01-01T00:00:00"
    store.config_put("mf_flows", flow)

    results = scheduler.run_once(store, registry, security)
    assert any(x.get("flow_id") == fid for x in results)
    assert store.get("Task", tid)["Subject"] == "Touched by flow"
