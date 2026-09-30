"""Tests for Field Service: data model, scheduling engine, dispatcher API,
technician self-service, and status transitions."""
import pytest

from helpers import login
from forcelet.api import create_app


@pytest.fixture()
def app(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def h(client):
    return login(client)


def _id(resp_json):
    return resp_json["Id"]


def _mk(client, h, obj, fields):
    r = client.post(f"/api/sobjects/{obj}", headers=h, json=fields)
    assert r.status_code in (200, 201), r.get_json()
    return _id(r.get_json())


def _demo_day(client, h):
    """Create two unscheduled appointments for 2026-10-06 (fresh DB per test)."""
    skills = {s["Name"]: s["Id"]
              for s in client.get("/api/sobjects/Skill", headers=h).get_json()}
    terrs = {t["Name"]: t["Id"]
             for t in client.get("/api/sobjects/ServiceTerritory", headers=h).get_json()}
    wt_hvac = _mk(client, h, "WorkType",
                  {"Name": "Demo HVAC Service", "EstimatedDuration": 90})
    _mk(client, h, "WorkTypeSkill",
        {"WorkTypeId": wt_hvac, "SkillId": skills["HVAC"]})
    wt_appl = _mk(client, h, "WorkType",
                  {"Name": "Demo Appliance Repair", "EstimatedDuration": 120})
    _mk(client, h, "WorkTypeSkill",
        {"WorkTypeId": wt_appl, "SkillId": skills["Appliance Repair"]})
    _mk(client, h, "WorkTypeSkill",
        {"WorkTypeId": wt_appl, "SkillId": skills["Electrical"]})
    wo1 = _mk(client, h, "WorkOrder",
              {"Name": "Demo WO HVAC", "WorkOrderNumber": "W-D1",
               "Status": "New", "Priority": "High",
               "Subject": "HVAC not cooling",
               "WorkTypeId": wt_hvac, "ServiceTerritoryId": terrs["South Bay"],
               "DurationMinutes": 90})
    a1 = _mk(client, h, "ServiceAppointment",
            {"Name": "HVAC repair visit", "WorkOrderId": wo1, "Status": "None",
             "ServiceTerritoryId": terrs["South Bay"],
             "ArrivalWindowStart": "2026-10-06T09:00:00",
             "ArrivalWindowEnd": "2026-10-06T12:00:00",
             "Location": "37.3382;-121.8863",
             "Address": "100 Market St, San Jose, CA 95113"})
    wo2 = _mk(client, h, "WorkOrder",
              {"Name": "Demo WO Washer", "WorkOrderNumber": "W-D2",
               "Status": "New", "Priority": "Medium",
               "Subject": "Washer not draining",
               "WorkTypeId": wt_appl, "ServiceTerritoryId": terrs["Bay Area"],
               "DurationMinutes": 120})
    a2 = _mk(client, h, "ServiceAppointment",
            {"Name": "Washer repair visit", "WorkOrderId": wo2, "Status": "None",
             "ServiceTerritoryId": terrs["Bay Area"],
             "ArrivalWindowStart": "2026-10-06T13:00:00",
             "ArrivalWindowEnd": "2026-10-06T17:00:00",
             "Location": "37.5485;-121.9886",
             "Address": "45500 Fremont Blvd, Fremont, CA 94538"})
    return a1, a2


# ------------------------------------------------------------ data model
def test_field_service_objects_registered(app):
    names = {o["name"] for o in app.mf_registry.list_objects()}
    for n in ["ServiceTerritory", "OperatingHours", "TimeSlot",
              "ServiceResource", "Skill", "ServiceResourceSkill",
              "WorkType", "WorkTypeSkill", "ResourceAbsence",
              "ServiceCrew", "ServiceCrewMember"]:
        assert n in names, n
    wo_fields = {f["name"] for f in app.mf_registry.get_object("WorkOrder")["fields"]}
    assert {"WorkTypeId", "ServiceTerritoryId", "DurationMinutes",
            "ServiceAddress"} <= wo_fields
    sa_fields = {f["name"] for f in app.mf_registry.get_object("ServiceAppointment")["fields"]}
    assert {"ServiceResourceId", "ServiceTerritoryId", "ArrivalWindowStart",
            "ArrivalWindowEnd", "ActualStart", "ActualEnd", "Location",
            "TravelTimeMinutes"} <= sa_fields


def test_seed_data_present(client, h):
    for obj, minimum in [("ServiceTerritory", 2), ("ServiceResource", 2),
                         ("Skill", 4), ("WorkType", 3), ("TimeSlot", 5),
                         ("ResourceAbsence", 1), ("ServiceCrew", 1)]:
        r = client.get(f"/api/sobjects/{obj}", headers=h)
        assert r.status_code == 200, (obj, r.get_json())
        assert len(r.get_json()) >= minimum, obj


def test_master_detail_cascade(client, h):
    oh = _mk(client, h, "OperatingHours", {"Name": "Temp Hours"})
    ts = _mk(client, h, "TimeSlot", {"OperatingHoursId": oh,
                                     "DayOfWeek": "Monday",
                                     "StartTime": "09:00:00", "EndTime": "17:00:00"})
    r = client.delete(f"/api/sobjects/OperatingHours/{oh}", headers=h)
    assert r.status_code == 200, r.get_json()
    # time slot should be cascade-deleted to the recycle bin with its master
    r = client.get(f"/api/sobjects/TimeSlot/{ts}", headers=h)
    assert r.status_code == 404


# ------------------------------------------------------------ dispatch API
def test_dispatch_day_structure(client, h):
    _demo_day(client, h)
    r = client.get("/api/field-service/dispatch?date=2026-10-06", headers=h)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["date"] == "2026-10-06"
    assert len(body["lanes"]) == 2
    names = [a["Name"] for a in body["unscheduled"]]
    assert "HVAC repair visit" in names and "Washer repair visit" in names


def test_candidates_rank_skills_and_territory(client, h):
    _demo_day(client, h)
    r = client.get("/api/field-service/dispatch?date=2026-10-06", headers=h)
    hvac = next(a for a in r.get_json()["unscheduled"] if "HVAC" in a["Name"])
    r = client.get(f"/api/field-service/candidates?appointment_id={hvac['Id']}"
                   "&date=2026-10-06", headers=h)
    assert r.status_code == 200, r.get_json()
    cands = r.get_json()["candidates"]
    assert cands, "expected at least one candidate"
    # Priya (South Bay + HVAC skill) outranks Sam (wrong territory)
    assert cands[0]["resource"]["Name"] == "Priya Nair"
    assert all(c["resource"]["Name"] != "Sam Rivera" for c in cands)


def test_assign_and_conflict(client, h):
    _demo_day(client, h)
    r = client.get("/api/field-service/dispatch?date=2026-10-06", headers=h)
    lanes = {ln["resource"]["Name"]: ln["resource"]["Id"]
             for ln in r.get_json()["lanes"]}
    hvac = next(a for a in r.get_json()["unscheduled"] if "HVAC" in a["Name"])
    r = client.post("/api/field-service/assign", headers=h,
                    json={"appointment_id": hvac["Id"],
                          "resource_id": lanes["Priya Nair"],
                          "start": "2026-10-06T09:00:00"})
    assert r.status_code == 200, r.get_json()

    # overlapping assignment on the same resource must be rejected
    wo = _mk(client, h, "WorkOrder",
             {"Name": "WO-T1", "WorkOrderNumber": "W-T1", "Status": "New",
              "Priority": "Low", "Subject": "Overlap probe"})
    appt = _mk(client, h, "ServiceAppointment",
               {"Name": "Overlap probe", "WorkOrderId": wo, "Status": "None",
                "ArrivalWindowStart": "2026-10-06T09:30:00",
                "ArrivalWindowEnd": "2026-10-06T12:00:00"})
    r = client.post("/api/field-service/assign", headers=h,
                    json={"appointment_id": appt,
                          "resource_id": lanes["Priya Nair"],
                          "start": "2026-10-06T09:30:00"})
    assert r.status_code == 422, r.get_json()


def test_assign_rejects_missing_skill(client, h):
    _demo_day(client, h)
    r = client.get("/api/sobjects/ServiceResource", headers=h)
    sam = next(x for x in r.get_json() if x["Name"] == "Sam Rivera")
    # Sam (Bay Area) cannot take the South Bay HVAC job: territory mismatch
    d = client.get("/api/field-service/dispatch?date=2026-10-06", headers=h)
    hvac = next(a for a in d.get_json()["unscheduled"] if "HVAC" in a["Name"])
    r = client.post("/api/field-service/assign", headers=h,
                    json={"appointment_id": hvac["Id"],
                          "resource_id": sam["Id"]})
    assert r.status_code == 422
    assert "territory" in r.get_json()["error"].lower()

    # a resource in-territory but without the skill is rejected too
    noskill = _mk(client, h, "ServiceResource",
                  {"Name": "No Skill Nora", "ResourceType": "Technician",
                   "ServiceTerritoryId": hvac["ServiceTerritoryId"],
                   "IsActive": True})
    r = client.post("/api/field-service/assign", headers=h,
                    json={"appointment_id": hvac["Id"], "resource_id": noskill})
    assert r.status_code == 422
    assert "skill" in r.get_json()["error"].lower()


def test_absence_blocks_availability(client, h):
    # Priya is on vacation 2026-10-09: no candidates for a South Bay job then
    terr = _mk(client, h, "ServiceTerritory", {"Name": "Absence Terr"})
    wt = _mk(client, h, "WorkType", {"Name": "Absence WT", "EstimatedDuration": 60})
    wo = _mk(client, h, "WorkOrder",
             {"Name": "WO-T2", "WorkOrderNumber": "W-T2", "Status": "New",
              "Subject": "Absence probe", "WorkTypeId": wt,
              "ServiceTerritoryId": terr})
    appt = _mk(client, h, "ServiceAppointment",
               {"Name": "Absence probe appt", "WorkOrderId": wo, "Status": "None",
                "ServiceTerritoryId": terr})
    r = client.get(f"/api/sobjects/ServiceResource", headers=h)
    priya = next(x for x in r.get_json() if x["Name"] == "Priya Nair")
    r = client.patch(f"/api/sobjects/ServiceResource/{priya['Id']}", headers=h,
                     json={"ServiceTerritoryId": terr})
    assert r.status_code == 200, r.get_json()
    r = client.post("/api/sobjects/ResourceAbsence", headers=h,
                    json={"ServiceResourceId": priya["Id"], "Type": "Vacation",
                          "Start": "2026-10-09T00:00:00",
                          "End": "2026-10-09T23:59:59"})
    assert r.status_code in (200, 201), r.get_json()
    r = client.get(f"/api/field-service/candidates?appointment_id={appt}"
                   "&date=2026-10-09", headers=h)
    assert r.status_code == 200, r.get_json()
    assert all(c["resource"]["Name"] != "Priya Nair"
               for c in r.get_json()["candidates"])


# ------------------------------------------------------------ auto-schedule
def test_schedule_day_assigns_all(client, h):
    _demo_day(client, h)
    r = client.post("/api/field-service/schedule", headers=h,
                    json={"date": "2026-10-06"})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert len(body["scheduled"]) == 3, body  # 2 seeded + 1 pre-existing
    assert body["unscheduled"] == []
    # every assignment landed inside operating hours with a resource
    r = client.get("/api/field-service/dispatch?date=2026-10-06", headers=h)
    assert r.get_json()["unscheduled"] == []


def test_optimize_respects_arrival_windows(client, h):
    _demo_day(client, h)
    client.post("/api/field-service/schedule", headers=h,
                json={"date": "2026-10-06"})
    r = client.post("/api/field-service/optimize", headers=h,
                    json={"date": "2026-10-06"})
    assert r.status_code == 200, r.get_json()
    r = client.get("/api/sobjects/ServiceAppointment", headers=h)
    for a in r.get_json():
        ws, we = a.get("ArrivalWindowStart"), a.get("ArrivalWindowEnd")
        if ws and we and a.get("ScheduledStart"):
            assert ws <= a["ScheduledStart"], (a["Name"], a["ScheduledStart"], ws)
            assert a["ScheduledEnd"] <= we, (a["Name"], a["ScheduledEnd"], we)


# ------------------------------------------------------------ status flow
def test_status_transitions_and_work_order_completion(client, h):
    _demo_day(client, h)
    client.post("/api/field-service/schedule", headers=h,
                json={"date": "2026-10-06"})
    r = client.get("/api/sobjects/ServiceAppointment", headers=h)
    appt = next(a for a in r.get_json()
                if a.get("ServiceResourceId") and a.get("Status") == "Scheduled")
    for st in ["Dispatched", "In Progress", "Completed"]:
        r = client.post("/api/field-service/status", headers=h,
                        json={"appointment_id": appt["Id"], "status": st})
        assert r.status_code == 200, (st, r.get_json())
    r = client.get(f"/api/sobjects/ServiceAppointment/{appt['Id']}", headers=h)
    done = r.get_json()
    assert done["ActualStart"] and done["ActualEnd"]
    # illegal jump back is rejected
    r = client.post("/api/field-service/status", headers=h,
                    json={"appointment_id": appt["Id"], "status": "Dispatched"})
    assert r.status_code == 422


def test_unassign(client, h):
    _demo_day(client, h)
    client.post("/api/field-service/schedule", headers=h,
                json={"date": "2026-10-06"})
    r = client.get("/api/sobjects/ServiceAppointment", headers=h)
    appt = next(a for a in r.get_json() if a.get("ServiceResourceId"))
    r = client.post("/api/field-service/unassign", headers=h,
                    json={"appointment_id": appt["Id"]})
    assert r.status_code == 200, r.get_json()
    r = client.get(f"/api/sobjects/ServiceAppointment/{appt['Id']}", headers=h)
    assert r.get_json()["ServiceResourceId"] in (None, "")
    assert r.get_json()["Status"] == "None"


# ------------------------------------------------------------ my schedule
def test_my_schedule_matches_username(client):
    h_leo = login(client, "leo")
    r = client.get("/api/field-service/my-schedule?date=2026-10-06", headers=h_leo)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["resource"]["Name"] == "Sam Rivera"


def test_my_schedule_empty_without_resource(client, h):
    r = client.get("/api/field-service/my-schedule?date=2026-10-06", headers=h)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["resource"] is None
    assert r.get_json()["appointments"] == []


# ------------------------------------------------------------ batch 2: parts
def _fs_seed(client, h):
    locs = {l["Name"]: l["Id"]
            for l in client.get("/api/sobjects/Location", headers=h).get_json()}
    prods = {p["Name"]: p["Id"]
             for p in client.get("/api/sobjects/Product", headers=h).get_json()}
    return locs, prods


def _stock_qty(client, h, loc_id, prod_id):
    items = client.get("/api/sobjects/ProductItem", headers=h).get_json()
    return next(i["QuantityOnHand"] for i in items
                if i["LocationId"] == loc_id and i["ProductId"] == prod_id)


def test_batch2_objects_registered(app):
    names = {o["name"] for o in app.mf_registry.list_objects()}
    for n in ["Location", "ProductItem", "ProductConsumed", "ProductRequest",
              "MaintenancePlan", "TimeEntry", "ServiceReport"]:
        assert n in names, n


def test_batch2_seed_data(client, h):
    locs, prods = _fs_seed(client, h)
    assert "Main Warehouse" in locs
    assert "Sam Rivera Van" in locs and "Priya Nair Van" in locs
    for p in ("HVAC Filter", "Washer Drive Belt", "Thermostat"):
        assert p in prods, p
    items = client.get("/api/sobjects/ProductItem", headers=h).get_json()
    assert len(items) >= 9


def test_consume_product_decrements_stock(client, h):
    _demo_day(client, h)
    d = client.get("/api/field-service/dispatch?date=2026-10-06", headers=h)
    hvac = next(a for a in d.get_json()["unscheduled"] if "HVAC" in a["Name"])
    lanes = {ln["resource"]["Name"]: ln["resource"]["Id"]
             for ln in d.get_json()["lanes"]}
    r = client.post("/api/field-service/assign", headers=h,
                    json={"appointment_id": hvac["Id"],
                          "resource_id": lanes["Priya Nair"],
                          "start": "2026-10-06T09:00:00"})
    assert r.status_code == 200, r.get_json()
    locs, prods = _fs_seed(client, h)
    van, filt = locs["Priya Nair Van"], prods["HVAC Filter"]
    assert _stock_qty(client, h, van, filt) == 5
    r = client.post("/api/field-service/consume-product", headers=h,
                    json={"appointment_id": hvac["Id"], "product_id": filt,
                          "quantity": 2})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["remaining"] == 3
    assert _stock_qty(client, h, van, filt) == 3
    consumed = client.get("/api/sobjects/ProductConsumed", headers=h).get_json()
    assert any(c["ServiceAppointmentId"] == hvac["Id"] and c["Quantity"] == 2
               for c in consumed)
    # insufficient stock rejected
    r = client.post("/api/field-service/consume-product", headers=h,
                    json={"appointment_id": hvac["Id"], "product_id": filt,
                          "quantity": 99})
    assert r.status_code == 422
    assert "stock" in r.get_json()["error"].lower()


def test_consume_product_no_van(client, h):
    res = _mk(client, h, "ServiceResource",
              {"Name": "Vanless Vic", "ResourceType": "Technician",
               "IsActive": True})
    wo = _mk(client, h, "WorkOrder",
             {"Name": "WO-V", "WorkOrderNumber": "W-V", "Status": "New",
              "Subject": "x"})
    appt = _mk(client, h, "ServiceAppointment",
               {"Name": "Vanless appt", "WorkOrderId": wo, "Status": "None",
                "ServiceResourceId": res})
    _, prods = _fs_seed(client, h)
    filt = next(iter(prods.values()))
    r = client.post("/api/field-service/consume-product", headers=h,
                    json={"appointment_id": appt, "product_id": filt,
                          "quantity": 1})
    assert r.status_code == 422
    assert "van" in r.get_json()["error"].lower()


def test_product_request_fulfill_transfers_stock(client, h):
    locs, prods = _fs_seed(client, h)
    wh, van, filt = locs["Main Warehouse"], locs["Sam Rivera Van"], prods["HVAC Filter"]
    h_leo = login(client, "leo")
    r = client.post("/api/field-service/product-requests", headers=h_leo,
                    json={"product_id": filt, "source_location_id": wh,
                          "destination_location_id": van, "quantity": 4})
    assert r.status_code in (200, 201), r.get_json()
    rid = r.get_json()["Id"]
    assert r.get_json()["Status"] == "Submitted"
    assert r.get_json()["RequestedBy"] == "leo"
    wh_before = _stock_qty(client, h, wh, filt)
    van_before = _stock_qty(client, h, van, filt)
    r = client.post(f"/api/field-service/product-requests/{rid}/fulfill",
                    headers=h)
    assert r.status_code == 200, r.get_json()
    assert _stock_qty(client, h, wh, filt) == wh_before - 4
    assert _stock_qty(client, h, van, filt) == van_before + 4
    r = client.get("/api/field-service/product-requests", headers=h)
    req = next(x for x in r.get_json()["requests"] if x["Id"] == rid)
    assert req["Status"] == "Fulfilled"
    assert req["product_name"] == "HVAC Filter"
    # requester was notified
    r = client.get("/api/notifications", headers=h_leo)
    assert any("Request fulfilled" in n["title"] for n in r.get_json())


def test_fulfill_rejects_bad_status(client, h):
    locs, prods = _fs_seed(client, h)
    rid = _mk(client, h, "ProductRequest",
              {"Name": "Draft req", "ProductId": next(iter(prods.values())),
               "SourceLocationId": locs["Main Warehouse"],
               "DestinationLocationId": locs["Sam Rivera Van"],
               "Quantity": 1, "Status": "Draft", "RequestedBy": "admin"})
    r = client.post(f"/api/field-service/product-requests/{rid}/fulfill",
                    headers=h)
    assert r.status_code == 422


# ------------------------------------------------------------ batch 2: plans
def test_maintenance_plan_generate(client, h):
    from datetime import date, timedelta
    today = date.today()
    nxt = today.strftime("%Y-%m-%d")
    expect_next = (today + timedelta(days=30)).strftime("%Y-%m-%d")
    terrs = {t["Name"]: t["Id"] for t in
             client.get("/api/sobjects/ServiceTerritory", headers=h).get_json()}
    plan = _mk(client, h, "MaintenancePlan",
               {"Name": "Quarterly HVAC check",
                "Subject": "Quarterly HVAC maintenance",
                "ServiceTerritoryId": terrs["Bay Area"], "Frequency": "Monthly",
                "NextRunDate": nxt, "IsActive": True, "Priority": "Medium",
                "DurationMinutes": 60})
    inactive = _mk(client, h, "MaintenancePlan",
                   {"Name": "Inactive plan", "Frequency": "Monthly",
                    "NextRunDate": nxt, "IsActive": False})
    ended = _mk(client, h, "MaintenancePlan",
                {"Name": "Ended plan", "Frequency": "Monthly",
                 "NextRunDate": nxt, "EndDate": "2026-01-01", "IsActive": True})
    r = client.post("/api/field-service/maintenance-plans/generate", headers=h)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    gen_ids = [g["plan_id"] for g in body["generated"]]
    assert plan in gen_ids
    assert inactive not in gen_ids and ended not in gen_ids
    gen = next(g for g in body["generated"] if g["plan_id"] == plan)
    wo = client.get(f"/api/sobjects/WorkOrder/{gen['work_order_id']}",
                    headers=h).get_json()
    assert wo["Subject"] == "Quarterly HVAC maintenance"
    assert wo["Status"] == "New"
    assert wo["ServiceTerritoryId"] == terrs["Bay Area"]
    appt = client.get(f"/api/sobjects/ServiceAppointment/{gen['appointment_id']}",
                      headers=h).get_json()
    assert appt["Status"] == "None"  # unscheduled
    assert appt["ScheduledStart"].startswith(nxt)
    p = client.get(f"/api/sobjects/MaintenancePlan/{plan}", headers=h).get_json()
    assert p["NextRunDate"] == expect_next


# ------------------------------------------------------------ batch 2: time
def _leo_appt(client, h, h_leo, suffix):
    """Appointment owned by leo (Sam's linked user) and assigned to Sam."""
    skills = {s["Name"]: s["Id"]
              for s in client.get("/api/sobjects/Skill", headers=h).get_json()}
    terrs = {t["Name"]: t["Id"] for t in
             client.get("/api/sobjects/ServiceTerritory", headers=h).get_json()}
    wt = _mk(client, h, "WorkType",
             {"Name": f"Leo WT {suffix}", "EstimatedDuration": 60})
    _mk(client, h, "WorkTypeSkill",
        {"WorkTypeId": wt, "SkillId": skills["Appliance Repair"]})
    wo = _mk(client, h, "WorkOrder",
             {"Name": f"Leo WO {suffix}", "WorkOrderNumber": f"W-L{suffix}",
              "Status": "New", "Subject": "x", "WorkTypeId": wt,
              "ServiceTerritoryId": terrs["Bay Area"]})
    # owned by leo -> visible to him under the sharing model
    appt = _mk(client, h_leo, "ServiceAppointment",
               {"Name": f"Leo appt {suffix}", "WorkOrderId": wo,
                "Status": "None", "ServiceTerritoryId": terrs["Bay Area"]})
    lanes = {ln["resource"]["Name"]: ln["resource"]["Id"]
             for ln in client.get("/api/field-service/dispatch?date=2026-10-06",
                                  headers=h).get_json()["lanes"]}
    r = client.post("/api/field-service/assign", headers=h,
                    json={"appointment_id": appt,
                          "resource_id": lanes["Sam Rivera"],
                          "start": "2026-10-06T13:00:00"})
    assert r.status_code == 200, r.get_json()
    return appt


def test_time_entry_flow(client, h):
    h_leo = login(client, "leo")  # Sam's linked technician
    appt = _leo_appt(client, h, h_leo, "T1")
    r = client.post("/api/field-service/time-entries", headers=h_leo,
                    json={"appointment_id": appt, "hours": 1.5,
                          "work_date": "2026-10-06", "entry_type": "Work",
                          "notes": "Replaced drive belt"})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["Hours"] == 1.5
    assert r.get_json()["EntryType"] == "Work"
    # non-positive hours rejected
    r = client.post("/api/field-service/time-entries", headers=h_leo,
                    json={"appointment_id": appt, "hours": 0})
    assert r.status_code == 422
    # unrelated read-only user rejected (not linked tech, no edit right)
    h_ana = login(client, "ana")
    r = client.post("/api/field-service/time-entries", headers=h_ana,
                    json={"appointment_id": appt, "hours": 1})
    assert r.status_code == 404


# ------------------------------------------------------------ batch 2: reports
def test_service_report_upsert(client, h):
    h_leo = login(client, "leo")  # Sam's linked technician
    appt = _leo_appt(client, h, h_leo, "R1")
    sig = "data:image/png;base64,iVBORw0KGgo="
    r = client.post("/api/field-service/service-reports", headers=h_leo,
                    json={"appointment_id": appt,
                          "summary": "Replaced drive belt, tested OK.",
                          "signature_name": "Jane Customer",
                          "signature_data": sig})
    assert r.status_code == 201, r.get_json()
    rid = r.get_json()["Id"]
    assert r.get_json()["SignedAt"]
    # second post upserts the same report
    r = client.post("/api/field-service/service-reports", headers=h_leo,
                    json={"appointment_id": appt,
                          "summary": "Updated summary.",
                          "signature_name": "Jane Customer",
                          "signature_data": sig})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["Id"] == rid
    assert r.get_json()["Summary"] == "Updated summary."
    # missing signature rejected
    r = client.post("/api/field-service/service-reports", headers=h_leo,
                    json={"appointment_id": appt, "summary": "x",
                          "signature_data": "  "})
    assert r.status_code == 422
    # unrelated read-only user rejected
    h_ana = login(client, "ana")
    r = client.post("/api/field-service/service-reports", headers=h_ana,
                    json={"appointment_id": appt, "summary": "x",
                          "signature_data": sig})
    assert r.status_code == 404


# ------------------------------------------------------------ batch 2: notify
def test_assign_and_unassign_notify_technician(client, h):
    _demo_day(client, h)
    d = client.get("/api/field-service/dispatch?date=2026-10-06", headers=h)
    hvac = next(a for a in d.get_json()["unscheduled"] if "HVAC" in a["Name"])
    lanes = {ln["resource"]["Name"]: ln["resource"]["Id"]
             for ln in d.get_json()["lanes"]}
    h_ana = login(client, "ana")  # Priya's linked user
    r = client.post("/api/field-service/assign", headers=h,
                    json={"appointment_id": hvac["Id"],
                          "resource_id": lanes["Priya Nair"],
                          "start": "2026-10-06T09:00:00"})
    assert r.status_code == 200, r.get_json()
    r = client.get("/api/notifications", headers=h_ana)
    titles = [n["title"] for n in r.get_json()]
    assert any("New job assigned" in t and "HVAC repair visit" in t
               for t in titles), titles
    r = client.post("/api/field-service/unassign", headers=h,
                    json={"appointment_id": hvac["Id"]})
    assert r.status_code == 200, r.get_json()
    r = client.get("/api/notifications", headers=h_ana)
    titles = [n["title"] for n in r.get_json()]
    assert any("Job unassigned" in t for t in titles), titles


def test_assigned_technician_sees_dispatcher_created_appointment(client, h):
    """Regression: a dispatcher-created appointment assigned to a
    technician's linked ServiceResource must be visible and actionable to
    that technician, invisible to unrelated users, and never visible to a
    user lacking profile-level read on ServiceAppointment."""
    res = {r["Name"]: r["Id"] for r in
           client.get("/api/sobjects/ServiceResource", headers=h).get_json()}
    wo = _mk(client, h, "WorkOrder",
             {"Name": "Visibility WO", "WorkOrderNumber": "W-VIS",
              "Status": "New", "Subject": "visibility check"})
    aid = _mk(client, h, "ServiceAppointment",
              {"Name": "Visibility visit", "WorkOrderId": wo, "Status": "Scheduled",
               "ServiceResourceId": res["Sam Rivera"],
               "ScheduledStart": "2026-11-02T09:00:00",
               "ScheduledEnd": "2026-11-02T10:00:00"})
    # 1. leo (Sam Rivera's linked user) sees it in My Schedule and can act on it.
    h_leo = login(client, "leo")
    sched = client.get("/api/field-service/my-schedule?date=2026-11-02",
                       headers=h_leo).get_json()
    assert aid in {a["Id"] for a in sched["appointments"]}
    r = client.post("/api/field-service/consume-product", headers=h_leo,
                    json={"appointment_id": aid, "product_id": "nope", "quantity": 1})
    assert r.status_code != 404, r.get_json()  # 422 (bad product), not hidden
    # 2. ana (unrelated technician) does not see it.
    h_ana = login(client, "ana")
    sched = client.get("/api/field-service/my-schedule?date=2026-11-02",
                       headers=h_ana).get_json()
    assert aid not in {a["Id"] for a in sched["appointments"]}
    # 3. Assignment never bypasses object-level read permission.
    r = client.post("/api/admin/profiles", headers=h,
                    json={"name": "No SA Read", "object_permissions": {},
                          "field_permissions": {}})
    assert r.status_code == 201, r.get_json()
    r = client.post("/api/admin/users", headers=h,
                    json={"username": "noread", "name": "No Read",
                          "profile": "No SA Read", "password": "forcelet"})
    assert r.status_code == 201, r.get_json()
    terr = client.get("/api/sobjects/ServiceTerritory", headers=h).get_json()[0]
    nr_res = _mk(client, h, "ServiceResource",
                 {"Name": "No Read Tech", "ResourceType": "Technician",
                  "Username": "noread", "ServiceTerritoryId": terr["Id"],
                  "IsActive": True})
    aid2 = _mk(client, h, "ServiceAppointment",
               {"Name": "No-read visit", "WorkOrderId": wo, "Status": "Scheduled",
                "ServiceResourceId": nr_res,
                "ScheduledStart": "2026-11-02T09:00:00",
                "ScheduledEnd": "2026-11-02T10:00:00"})
    h_nr = login(client, "noread")
    sched = client.get("/api/field-service/my-schedule?date=2026-11-02",
                       headers=h_nr).get_json()
    assert sched["resource"] is not None  # linked resource is found
    assert aid2 not in {a["Id"] for a in sched["appointments"]}


# ------------------------------------------------------------ concurrency
def _threaded(app, h, method, path, body, n):
    """Fire n simultaneous requests (one test_client per thread)."""
    import threading
    results = [None] * n

    def worker(i):
        c = app.test_client()
        try:
            r = c.open(path, method=method, headers=h, json=body)
            results[i] = (r.status_code, r.get_json())
        except Exception as e:  # noqa: BLE001 - surface thread failures
            results[i] = ("exc", str(e)[:200])

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return results


def test_concurrent_consume_cannot_overspend(app, client, h):
    """Two simultaneous consumptions of 4 units against 5 on hand: exactly
    one succeeds (remaining 1); stock never goes negative."""
    _demo_day(client, h)
    d = client.get("/api/field-service/dispatch?date=2026-10-06", headers=h)
    hvac = next(a for a in d.get_json()["unscheduled"] if "HVAC" in a["Name"])
    lanes = {ln["resource"]["Name"]: ln["resource"]["Id"]
             for ln in d.get_json()["lanes"]}
    client.post("/api/field-service/assign", headers=h,
                json={"appointment_id": hvac["Id"],
                      "resource_id": lanes["Priya Nair"],
                      "start": "2026-10-06T09:00:00"})
    locs, prods = _fs_seed(client, h)
    van, filt = locs["Priya Nair Van"], prods["HVAC Filter"]
    assert _stock_qty(client, h, van, filt) == 5
    rs = _threaded(app, h, "POST", "/api/field-service/consume-product",
                   {"appointment_id": hvac["Id"], "product_id": filt,
                    "quantity": 4}, 2)
    codes = sorted(r[0] for r in rs)
    assert codes == [200, 422], rs
    assert _stock_qty(client, h, van, filt) == 1


def test_concurrent_fulfill_moves_stock_once(app, client, h):
    """Two simultaneous fulfillments of the same request: one succeeds, one
    is rejected; the source loses stock exactly once."""
    locs, prods = _fs_seed(client, h)
    src, dst = locs["Main Warehouse"], locs["Sam Rivera Van"]
    filt = prods["HVAC Filter"]
    before = _stock_qty(client, h, src, filt)
    rid = _mk(client, h, "ProductRequest",
              {"Name": "Concurrency PR", "ProductId": filt,
               "SourceLocationId": src, "DestinationLocationId": dst,
               "Quantity": 2, "Status": "Submitted"})
    rs = _threaded(app, h, "POST",
                   f"/api/field-service/product-requests/{rid}/fulfill",
                   {}, 2)
    codes = sorted(r[0] for r in rs)
    assert codes == [200, 422], rs
    assert _stock_qty(client, h, src, filt) == before - 2


def test_concurrent_service_report_no_duplicates(app, client, h):
    """Two simultaneous report submissions for one appointment: a single
    ServiceReport record exists afterwards."""
    _demo_day(client, h)
    d = client.get("/api/field-service/dispatch?date=2026-10-06", headers=h)
    hvac = next(a for a in d.get_json()["unscheduled"] if "HVAC" in a["Name"])
    lanes = {ln["resource"]["Name"]: ln["resource"]["Id"]
             for ln in d.get_json()["lanes"]}
    client.post("/api/field-service/assign", headers=h,
                json={"appointment_id": hvac["Id"],
                      "resource_id": lanes["Priya Nair"],
                      "start": "2026-10-06T09:00:00"})
    body = {"appointment_id": hvac["Id"], "summary": "Fixed",
            "signature_data": "data:image/png;base64,iVBOR",
            "signature_name": "Tech"}
    rs = _threaded(app, h, "POST", "/api/field-service/service-reports",
                   body, 2)
    assert all(r[0] in (200, 201) for r in rs), rs
    ids = {r[1]["Id"] for r in rs}
    assert len(ids) == 1, ids  # both threads converged on the same record
    reps = [r for r in client.get("/api/sobjects/ServiceReport", headers=h).get_json()
            if r["ServiceAppointmentId"] == hvac["Id"]]
    assert len(reps) == 1, reps
