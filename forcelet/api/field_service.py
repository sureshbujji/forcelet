"""Field Service: scheduling engine and dispatcher REST API. — Forcelet platform module.

Covers the Salesforce Field Service core: service territories, operating
hours/time slots, service resources with skills, work types with required
skills, resource absences, crews, a constraint-based auto-scheduler, a
travel-time route optimizer, a dispatcher day view, and a technician
self-service (\"my schedule\") API with guarded status transitions.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import math
import threading
from datetime import datetime, timedelta

from flask import Flask, jsonify, request

from ._shared import (
    _do_create, _do_update, _visible_records, current_user, require_auth,
    serialize, ctx,
)

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday"]

# Statuses that occupy a resource's calendar.
BUSY_STATUSES = {"Scheduled", "Dispatched", "In Progress"}
# Process-level lock serializing check-then-act inventory sequences
# (stock decrement, request fulfillment, report upsert). Effective for the
# documented deployment (one Gunicorn worker, multiple threads). A
# multi-worker/multi-host deployment must replace this with a distributed
# lock or database-level atomicity (see the Postgres scale scope).
_FS_LOCK = threading.Lock()
# Statuses an appointment can be in when it has no resource yet.
UNSCHEDULED_STATUSES = {"None", "", "Scheduled"}
# Allowed status transitions for the technician mobile flow.
TRANSITIONS = {
    "None": {"Scheduled", "Cancelled"},
    "Scheduled": {"Dispatched", "Cancelled"},
    "Dispatched": {"In Progress", "Cancelled"},
    "In Progress": {"Completed", "Cannot Complete", "Cancelled"},
    "Cannot Complete": {"Scheduled"},
    "Completed": set(),
    "Cancelled": {"Scheduled"},
}
AVG_SPEED_KMH = 50.0  # travel-time model


# ------------------------------------------------------------------ helpers
def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _dt_str(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_geo(s):
    """Geolocation fields store 'lat;lng'."""
    if not s:
        return None
    try:
        lat, lng = str(s).split(";")
        return float(lat), float(lng)
    except (ValueError, AttributeError):
        return None


def _haversine_km(a, b):
    if not a or not b:
        return 0.0
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def _travel_min(a, b):
    return _haversine_km(a, b) / AVG_SPEED_KMH * 60.0


def _day_bounds(day):
    d = datetime.strptime(day, "%Y-%m-%d")
    return d, d + timedelta(days=1)


def _overlaps(a0, a1, b0, b1):
    return a0 < b1 and b0 < a1


def _subtract(windows, blocks):
    """Remove blocked intervals from a list of (start, end) windows."""
    out = []
    for ws, we in windows:
        segs = [(ws, we)]
        for bs, be in blocks:
            nxt = []
            for s, e in segs:
                if not _overlaps(s, e, bs, be):
                    nxt.append((s, e))
                    continue
                if s < bs:
                    nxt.append((s, bs))
                if be < e:
                    nxt.append((be, e))
            segs = nxt
        out.extend(s for s in segs if s[1] > s[0])
    return sorted(out)


# ------------------------------------------------------------ engine inputs
def _all(store, obj_name):
    return store.query(obj_name, owner_ids=None, limit=10000)


def _visible_appts(user):
    """ServiceAppointment records the user may see (sharing-aware).

    A technician additionally sees appointments assigned to their linked
    service resource, even when record sharing alone would hide them:
    dispatcher-created appointments must appear in the technician's
    My Schedule and stay actionable (time entries, parts, reports).
    Assignment never grants access without the profile-level read
    permission on ServiceAppointment.
    """
    store, _, security = ctx()
    recs, _ = _visible_records(user, "ServiceAppointment")
    if security.can(user, "read", "ServiceAppointment"):
        linked = _linked_resource(store, user)
        if linked:
            seen = {r["id"] for r in recs}
            for a in _all(store, "ServiceAppointment"):
                if a.get("ServiceResourceId") == linked["id"] and a["id"] not in seen:
                    recs.append(a)
                    seen.add(a["id"])
    return recs


def _visible_map(user, obj_name):
    """{id: record} of records the user may see."""
    recs, _ = _visible_records(user, obj_name)
    return {r["id"]: r for r in recs}


def _linked_resource(store, user):
    """Active ServiceResource linked to the caller's login username."""
    username = (user.get("username") or "").lower()
    return next((r for r in _all(store, "ServiceResource")
                 if (r.get("Username") or "").lower() == username
                 and r.get("IsActive", True)), None)


def _notify_resource_user(store, security, resource, title, body, appt_id=None):
    """Notify the login user linked to a ServiceResource; skip if none."""
    username = (resource or {}).get("Username")
    if not username:
        return
    u = security.get_user_by_username(username)
    if not u:
        return
    store.notify(u["id"], "field_service", title, body or "",
                 "ServiceAppointment", appt_id)


def _by_id(store, obj_name):
    return {r["id"]: r for r in _all(store, obj_name)}


def _operating_windows(store, hours_id, day):
    """(start, end) datetimes a set of operating hours covers on a date."""
    if not hours_id:
        d0, d1 = _day_bounds(day)
        return [(d0, d1)]  # no hours defined -> available all day
    dow = DAY_NAMES[datetime.strptime(day, "%Y-%m-%d").weekday()]
    wins = []
    for ts in _all(store, "TimeSlot"):
        if ts.get("OperatingHoursId") != hours_id or ts.get("DayOfWeek") != dow:
            continue
        s, e = ts.get("StartTime"), ts.get("EndTime")
        if s and e:
            wins.append((datetime.strptime(f"{day}T{s[:8]}", "%Y-%m-%dT%H:%M:%S"),
                         datetime.strptime(f"{day}T{e[:8]}", "%Y-%m-%dT%H:%M:%S")))
    return sorted(wins)


def _territory_ancestors(store, terr_id, terr_by_id):
    out = []
    seen = set()
    cur = terr_by_id.get(terr_id, {}).get("ParentTerritoryId")
    while cur and cur not in seen:
        seen.add(cur)
        out.append(cur)
        cur = terr_by_id.get(cur, {}).get("ParentTerritoryId")
    return out


def _resource_qualifies_for_territory(store, resource, appt_terr_id, terr_by_id):
    if not appt_terr_id:
        return True
    rt = resource.get("ServiceTerritoryId")
    if not rt:
        return True  # floater: serves any territory
    if rt == appt_terr_id:
        return True
    # a resource in a child territory can serve its parent territory's work
    return rt in _territory_children(store, appt_terr_id, terr_by_id)


def _territory_children(store, terr_id, terr_by_id):
    kids = set()
    stack = [terr_id]
    while stack:
        t = stack.pop()
        for tid, rec in terr_by_id.items():
            if rec.get("ParentTerritoryId") == t and tid not in kids:
                kids.add(tid)
                stack.append(tid)
    return kids


def _required_skills(store, appt, wo_by_id):
    wo = wo_by_id.get(appt.get("WorkOrderId") or "") or {}
    wt_id = wo.get("WorkTypeId")
    if not wt_id:
        return set()
    return {r["SkillId"] for r in _all(store, "WorkTypeSkill")
            if r.get("WorkTypeId") == wt_id and r.get("SkillId")}


def _resource_skills(store, resource_id):
    return {r["SkillId"] for r in _all(store, "ServiceResourceSkill")
            if r.get("ServiceResourceId") == resource_id and r.get("SkillId")}


def _appt_duration_min(store, appt, wo_by_id):
    wo = wo_by_id.get(appt.get("WorkOrderId") or "") or {}
    for v in (wo.get("DurationMinutes"),):
        if v:
            try:
                return max(15, int(float(v)))
            except (TypeError, ValueError):
                pass
    wt = None
    if wo.get("WorkTypeId"):
        wt = next((w for w in _all(store, "WorkType")
                   if w["id"] == wo["WorkTypeId"]), None)
    if wt and wt.get("EstimatedDuration"):
        try:
            return max(15, int(float(wt["EstimatedDuration"])))
        except (TypeError, ValueError):
            pass
    s, e = _parse_dt(appt.get("ScheduledStart")), _parse_dt(appt.get("ScheduledEnd"))
    if s and e and e > s:
        return max(15, int((e - s).total_seconds() // 60))
    return 60


def _appt_location(appt):
    return _parse_geo(appt.get("Location"))


def _resource_day_windows(store, resource, day, appts_by_id, ignore_appt_id=None):
    """Free (start, end) windows for a resource on a date."""
    wins = _operating_windows(store, resource.get("OperatingHoursId"), day)
    d0, d1 = _day_bounds(day)
    blocks = []
    for ab in _all(store, "ResourceAbsence"):
        if ab.get("ServiceResourceId") != resource["id"]:
            continue
        s, e = _parse_dt(ab.get("Start")), _parse_dt(ab.get("End"))
        if s and e and _overlaps(s, e, d0, d1):
            blocks.append((max(s, d0), min(e, d1)))
    for a in appts_by_id.values():
        if a["id"] == ignore_appt_id:
            continue
        if a.get("ServiceResourceId") != resource["id"]:
            continue
        if (a.get("Status") or "") not in BUSY_STATUSES:
            continue
        s, e = _parse_dt(a.get("ScheduledStart")), _parse_dt(a.get("ScheduledEnd"))
        if s and e and _overlaps(s, e, d0, d1):
            blocks.append((max(s, d0), min(e, d1)))
    return _subtract(wins, blocks)


def _earliest_slot(windows, duration_min, earliest=None):
    """Earliest start datetime fitting duration_min inside windows."""
    for ws, we in windows:
        s = max(ws, earliest) if earliest else ws
        if s + timedelta(minutes=duration_min) <= we:
            return s
    return None


def candidates_for(store, appt, day, appts_by_id=None, wo_by_id=None,
                   terr_by_id=None):
    """Rank eligible resources for an appointment.

    Returns a list of dicts: resource, start (iso), travel_min, score,
    reasons. Sorted best-first.
    """
    appts_by_id = appts_by_id or _by_id(store, "ServiceAppointment")
    wo_by_id = wo_by_id or _by_id(store, "WorkOrder")
    terr_by_id = terr_by_id or _by_id(store, "ServiceTerritory")
    wo = wo_by_id.get(appt.get("WorkOrderId") or "") or {}
    appt_terr = appt.get("ServiceTerritoryId") or wo.get("ServiceTerritoryId")
    need = _required_skills(store, appt, wo_by_id)
    duration = _appt_duration_min(store, appt, wo_by_id)
    loc = _appt_location(appt)
    pref_start = _parse_dt(appt.get("ScheduledStart"))
    win_s = _parse_dt(appt.get("ArrivalWindowStart"))
    win_e = _parse_dt(appt.get("ArrivalWindowEnd"))

    out = []
    for res in _all(store, "ServiceResource"):
        if not res.get("IsActive", True):
            continue
        reasons = []
        if not _resource_qualifies_for_territory(store, res, appt_terr, terr_by_id):
            continue
        have = _resource_skills(store, res["id"])
        missing = need - have
        if missing:
            continue
        if need:
            reasons.append(f"skills: {len(need & have)}/{len(need)} matched")
        windows = _resource_day_windows(store, res, day, appts_by_id,
                                        ignore_appt_id=appt["id"])
        earliest = max(win_s, pref_start) if win_s and pref_start else (win_s or pref_start)
        start = _earliest_slot(windows, duration, earliest)
        if start is None:
            continue
        if win_e and start + timedelta(minutes=duration) > win_e:
            continue
        home = _parse_geo(res.get("HomeBase"))
        travel = _travel_min(home, loc)
        wait = max(0.0, (start - earliest).total_seconds() / 60) if earliest else 0.0
        score = travel + wait * 0.5
        reasons.append(f"travel ~{travel:.0f} min")
        if wait:
            reasons.append(f"starts {wait:.0f} min after requested")
        out.append({"resource": res, "start": _dt_str(start),
                    "travel_min": round(travel, 1), "score": round(score, 1),
                    "reasons": reasons, "duration_min": duration})
    out.sort(key=lambda c: c["score"])
    return out


def _apply_assignment(store, user, appt, cand, security=None):
    """Persist an assignment chosen by the engine. Returns (ok, error)."""
    start = datetime.strptime(cand["start"], "%Y-%m-%dT%H:%M:%S")
    end = start + timedelta(minutes=cand["duration_min"])
    body = {"ServiceResourceId": cand["resource"]["id"],
            "ScheduledStart": _dt_str(start), "ScheduledEnd": _dt_str(end),
            "Status": "Scheduled",
            "TravelTimeMinutes": cand["travel_min"]}
    # TravelTimeMinutes may not exist as a field; drop silently if unknown
    code, data = _do_update(user, "ServiceAppointment", appt["id"], body)
    if code == 422 and "TravelTimeMinutes" in str(data):
        body.pop("TravelTimeMinutes")
        code, data = _do_update(user, "ServiceAppointment", appt["id"], body)
    if code not in (200, 201):
        return False, (data.get("error") if isinstance(data, dict) else str(data))
    if security is not None:
        _notify_resource_user(
            store, security, cand["resource"],
            f"New job assigned: {appt.get('Name') or 'appointment'}",
            f"Scheduled for {cand['start']}.", appt["id"])
    return True, None


def schedule_day(store, user, day, territory_id=None, security=None):
    """Auto-assign every unscheduled appointment on a date.

    Returns {"scheduled": [...], "unscheduled": [{appointment, reason}]}.
    """
    appts_by_id = {a["id"]: a for a in _visible_appts(user)}
    busy_by_id = _by_id(store, "ServiceAppointment")  # full calendar for conflict checks
    wo_by_id = _by_id(store, "WorkOrder")
    terr_by_id = _by_id(store, "ServiceTerritory")
    d0, d1 = _day_bounds(day)

    def on_day(a):
        s = _parse_dt(a.get("ScheduledStart")) or _parse_dt(a.get("ArrivalWindowStart"))
        if s and not (d0 <= s < d1):
            return False
        # appointments without any time yet are schedulable any day
        return True

    queue = []
    for a in appts_by_id.values():
        if a.get("ServiceResourceId"):
            continue
        if (a.get("Status") or "None") not in UNSCHEDULED_STATUSES:
            continue
        if territory_id:
            wo = wo_by_id.get(a.get("WorkOrderId") or "") or {}
            at = a.get("ServiceTerritoryId") or wo.get("ServiceTerritoryId")
            if at != territory_id:
                continue
        if not on_day(a):
            continue
        queue.append(a)

    prio = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    def sort_key(a):
        wo = wo_by_id.get(a.get("WorkOrderId") or "") or {}
        s = _parse_dt(a.get("ArrivalWindowStart")) or _parse_dt(a.get("ScheduledStart"))
        return (prio.get(wo.get("Priority"), 2), s or d1)
    queue.sort(key=sort_key)

    scheduled, unscheduled = [], []
    for appt in queue:
        cands = candidates_for(store, appt, day, busy_by_id, wo_by_id, terr_by_id)
        if not cands:
            unscheduled.append({"appointment_id": appt["id"],
                                "name": appt.get("Name"),
                                "reason": "No eligible resource with availability"})
            continue
        ok, err = _apply_assignment(store, user, appt, cands[0], security)
        if ok:
            refreshed = store.get("ServiceAppointment", appt["id"])
            appts_by_id[appt["id"]] = refreshed
            busy_by_id[appt["id"]] = refreshed
            scheduled.append({"appointment_id": appt["id"],
                              "resource_id": cands[0]["resource"]["id"],
                              "resource_name": cands[0]["resource"]["Name"],
                              "start": cands[0]["start"]})
        else:
            unscheduled.append({"appointment_id": appt["id"],
                                "name": appt.get("Name"), "reason": err or "assign failed"})
    return {"scheduled": scheduled, "unscheduled": unscheduled}


def optimize_day(store, user, day, territory_id=None):
    """Re-sequence each resource's scheduled appointments to cut travel.

    Nearest-neighbor ordering from the resource's home base, then re-time
    back-to-back from the day's first feasible start. Only touches
    Status='Scheduled' appointments.
    """
    appts_by_id = {a["id"]: a for a in _visible_appts(user)}
    busy_by_id = _by_id(store, "ServiceAppointment")  # full calendar for block checks
    wo_by_id = _by_id(store, "WorkOrder")
    d0, d1 = _day_bounds(day)
    by_res = {}
    for a in appts_by_id.values():
        rid = a.get("ServiceResourceId")
        if not rid or (a.get("Status") or "") != "Scheduled":
            continue
        s = _parse_dt(a.get("ScheduledStart"))
        if not s or not (d0 <= s < d1):
            continue
        if territory_id:
            wo = wo_by_id.get(a.get("WorkOrderId") or "") or {}
            at = a.get("ServiceTerritoryId") or wo.get("ServiceTerritoryId")
            if at != territory_id:
                continue
        by_res.setdefault(rid, []).append(a)

    moved = []
    for rid, appts in by_res.items():
        if len(appts) < 2:
            continue
        res = next((r for r in _all(store, "ServiceResource") if r["id"] == rid), None)
        if not res:
            continue
        home = _parse_geo(res.get("HomeBase"))
        # nearest-neighbor order
        remaining = list(appts)
        order, pos = [], home
        while remaining:
            nxt = min(remaining,
                      key=lambda a: _travel_min(pos, _appt_location(a)))
            order.append(nxt)
            pos = _appt_location(nxt) or pos
            remaining.remove(nxt)
        # re-time sequentially from the first window start
        windows = _resource_day_windows(store, res, day, busy_by_id,
                                        ignore_appt_id=None)
        # exclude this resource's own appointments from the block list by
        # recomputing windows ignoring all of them
        ids = {a["id"] for a in appts}
        wins = _operating_windows(store, res.get("OperatingHoursId"), day)
        d0b, d1b = d0, d1
        blocks = []
        for ab in _all(store, "ResourceAbsence"):
            if ab.get("ServiceResourceId") != rid:
                continue
            s, e = _parse_dt(ab.get("Start")), _parse_dt(ab.get("End"))
            if s and e and _overlaps(s, e, d0b, d1b):
                blocks.append((max(s, d0b), min(e, d1b)))
        for a in busy_by_id.values():
            if a["id"] in ids or a.get("ServiceResourceId") != rid:
                continue
            if (a.get("Status") or "") not in BUSY_STATUSES:
                continue
            s, e = _parse_dt(a.get("ScheduledStart")), _parse_dt(a.get("ScheduledEnd"))
            if s and e and _overlaps(s, e, d0b, d1b):
                blocks.append((max(s, d0b), min(e, d1b)))
        wins = _subtract(wins, blocks)
        if not wins:
            continue
        cursor = wins[0][0]
        pos = home
        for a in order:
            dur = _appt_duration_min(store, a, wo_by_id)
            travel = _travel_min(pos, _appt_location(a))
            cursor = cursor + timedelta(minutes=travel)
            win_s = _parse_dt(a.get("ArrivalWindowStart"))
            win_e = _parse_dt(a.get("ArrivalWindowEnd"))
            if win_s and cursor < win_s:
                cursor = win_s
            slot = _earliest_slot(wins, dur, cursor)
            if slot is None:
                break  # leave the rest where they are
            if win_e and slot + timedelta(minutes=dur) > win_e:
                break  # cannot honor the arrival window; leave in place
            cursor = slot
            end = cursor + timedelta(minutes=dur)
            old_start = a.get("ScheduledStart")
            if old_start != _dt_str(cursor):
                code, _ = _do_update(user, "ServiceAppointment", a["id"],
                                     {"ScheduledStart": _dt_str(cursor),
                                      "ScheduledEnd": _dt_str(end)})
                if code in (200, 201):
                    moved.append({"appointment_id": a["id"],
                                  "old_start": old_start,
                                  "new_start": _dt_str(cursor)})
                    a["ScheduledStart"] = _dt_str(cursor)
                    a["ScheduledEnd"] = _dt_str(end)
            cursor = end
            pos = _appt_location(a) or pos
    return {"moved": moved}


def _transition_ok(store, user, appt_id, new_status):
    appt = store.get("ServiceAppointment", appt_id)
    if not appt:
        return 404, {"error": "Appointment not found"}
    cur = appt.get("Status") or "None"
    allowed = TRANSITIONS.get(cur, set())
    if new_status not in allowed:
        return 422, {"error": f"Cannot move from '{cur}' to '{new_status}'"}
    body = {"Status": new_status}
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    if new_status == "In Progress":
        body["ActualStart"] = now
    if new_status in ("Completed", "Cannot Complete", "Cancelled"):
        body["ActualEnd"] = now
    code, data = _do_update(user, "ServiceAppointment", appt_id, body)
    if code not in (200, 201):
        return code, data
    if new_status == "Completed":
        _maybe_complete_work_order(store, user, appt.get("WorkOrderId"))
    return 200, {"ok": True, "status": new_status}


def _maybe_complete_work_order(store, user, wo_id):
    if not wo_id:
        return
    appts = [a for a in _all(store, "ServiceAppointment")
             if a.get("WorkOrderId") == wo_id]
    if appts and all((a.get("Status") or "") == "Completed" for a in appts):
        _do_update(user, "WorkOrder", wo_id,
                   {"Status": "Completed",
                    "CompletedDate": datetime.now().strftime("%Y-%m-%d")})


# ------------------------------------------------------------------- API
def register(app: Flask):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security

    def _ser(user, obj_name, rec):
        obj = registry.get_object(obj_name)
        return serialize(user, obj, rec)

    @app.get("/api/field-service/dispatch")
    @require_auth
    def dispatch_day():
        user = current_user()
        day = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")
        territory_id = request.args.get("territory_id") or None
        try:
            d0, d1 = _day_bounds(day)
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 422
        wo_by_id = _by_id(store, "WorkOrder")
        resources = [r for r in _all(store, "ServiceResource") if r.get("IsActive", True)]
        if territory_id:
            resources = [r for r in resources
                         if not r.get("ServiceTerritoryId")
                         or r.get("ServiceTerritoryId") == territory_id]
        lanes = []
        visible_appts = _visible_appts(user)
        for res in sorted(resources, key=lambda r: r.get("Name") or ""):
            appts = []
            for a in visible_appts:
                if a.get("ServiceResourceId") != res["id"]:
                    continue
                if (a.get("Status") or "") not in BUSY_STATUSES:
                    continue
                s = _parse_dt(a.get("ScheduledStart"))
                if not s or not (d0 <= s < d1):
                    continue
                wo = wo_by_id.get(a.get("WorkOrderId") or "") or {}
                if territory_id:
                    at = a.get("ServiceTerritoryId") or wo.get("ServiceTerritoryId")
                    if at != territory_id:
                        continue
                item = _ser(user, "ServiceAppointment", a)
                item["work_order"] = {"Id": wo.get("id"), "Subject": wo.get("Subject"),
                                      "Priority": wo.get("Priority")}
                appts.append(item)
            appts.sort(key=lambda a: a.get("ScheduledStart") or "")
            lanes.append({"resource": _ser(user, "ServiceResource", res),
                          "appointments": appts})
        unscheduled = []
        for a in visible_appts:
            if a.get("ServiceResourceId"):
                continue
            if (a.get("Status") or "None") not in UNSCHEDULED_STATUSES:
                continue
            wo = wo_by_id.get(a.get("WorkOrderId") or "") or {}
            if territory_id:
                at = a.get("ServiceTerritoryId") or wo.get("ServiceTerritoryId")
                if at != territory_id:
                    continue
            s = _parse_dt(a.get("ScheduledStart")) or _parse_dt(a.get("ArrivalWindowStart"))
            if s and not (d0 <= s < d1):
                continue
            item = _ser(user, "ServiceAppointment", a)
            item["work_order"] = {"Id": wo.get("id"), "Subject": wo.get("Subject"),
                                  "Priority": wo.get("Priority")}
            item["required_skills"] = sorted(_required_skills(store, a, wo_by_id))
            unscheduled.append(item)
        return jsonify({"date": day, "lanes": lanes, "unscheduled": unscheduled})

    @app.get("/api/field-service/candidates")
    @require_auth
    def candidates():
        user = current_user()
        appt_id = request.args.get("appointment_id")
        day = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")
        appt = store.get("ServiceAppointment", appt_id) if appt_id else None
        if not appt:
            return jsonify({"error": "appointment_id required"}), 422
        if appt["id"] not in {a["id"] for a in _visible_appts(user)}:
            return jsonify({"error": "Not found"}), 404
        cands = candidates_for(store, appt, day)
        return jsonify({"appointment_id": appt_id,
                        "candidates": [{**c,
                                        "resource": _ser(user, "ServiceResource", c["resource"])}
                                       for c in cands]})

    @app.post("/api/field-service/assign")
    @require_auth
    def assign():
        user = current_user()
        body = request.json or {}
        appt = store.get("ServiceAppointment", body.get("appointment_id"))
        res = store.get("ServiceResource", body.get("resource_id"))
        if not appt or not res:
            return jsonify({"error": "appointment_id and resource_id required"}), 422
        day = (body.get("start") or "")[:10] or datetime.now().strftime("%Y-%m-%d")
        appts_by_id = _by_id(store, "ServiceAppointment")
        wo_by_id = _by_id(store, "WorkOrder")
        terr_by_id = _by_id(store, "ServiceTerritory")
        if not _resource_qualifies_for_territory(
                store, res, appt.get("ServiceTerritoryId")
                or (wo_by_id.get(appt.get("WorkOrderId") or "") or {}).get("ServiceTerritoryId"),
                terr_by_id):
            return jsonify({"error": "Resource does not serve this territory"}), 422
        missing = _required_skills(store, appt, wo_by_id) - _resource_skills(store, res["id"])
        if missing:
            return jsonify({"error": "Resource lacks required skills",
                            "missing_skill_ids": sorted(missing)}), 422
        duration = _appt_duration_min(store, appt, wo_by_id)
        start = _parse_dt(body.get("start"))
        if not start:
            cands = [c for c in candidates_for(store, appt, day, appts_by_id,
                                               wo_by_id, terr_by_id)
                     if c["resource"]["id"] == res["id"]]
            if not cands:
                return jsonify({"error": "No feasible slot for this resource"}), 422
            start = datetime.strptime(cands[0]["start"], "%Y-%m-%dT%H:%M:%S")
        else:
            windows = _resource_day_windows(store, res, day, appts_by_id,
                                            ignore_appt_id=appt["id"])
            if not any(ws <= start and start + timedelta(minutes=duration) <= we
                       for ws, we in windows):
                return jsonify({"error": "Requested time conflicts with availability"}), 422
        ok, err = _apply_assignment(store, user, appt,
                                    {"resource": res, "start": _dt_str(start),
                                     "duration_min": duration,
                                     "travel_min": _travel_min(
                                         _parse_geo(res.get("HomeBase")),
                                         _appt_location(appt))},
                                    security)
        if not ok:
            return jsonify({"error": err or "assign failed"}), 422
        return jsonify({"ok": True, "appointment_id": appt["id"],
                        "resource_id": res["id"], "start": _dt_str(start)})

    @app.post("/api/field-service/unassign")
    @require_auth
    def unassign():
        user = current_user()
        body = request.json or {}
        appt = store.get("ServiceAppointment", body.get("appointment_id"))
        if not appt:
            return jsonify({"error": "appointment_id required"}), 422
        res = store.get("ServiceResource", appt.get("ServiceResourceId")) \
            if appt.get("ServiceResourceId") else None
        code, data = _do_update(user, "ServiceAppointment", appt["id"],
                                {"ServiceResourceId": None, "Status": "None"})
        if code not in (200, 201):
            return jsonify(data), code
        if res:
            _notify_resource_user(
                store, security, res,
                f"Job unassigned: {appt.get('Name') or 'appointment'}",
                "The assignment was removed by the dispatcher.", appt["id"])
        return jsonify({"ok": True})

    @app.post("/api/field-service/schedule")
    @require_auth
    def schedule():
        user = current_user()
        body = request.json or {}
        day = body.get("date") or datetime.now().strftime("%Y-%m-%d")
        try:
            return jsonify(schedule_day(store, user, day,
                                        body.get("territory_id"), security))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422

    @app.post("/api/field-service/optimize")
    @require_auth
    def optimize():
        user = current_user()
        body = request.json or {}
        day = body.get("date") or datetime.now().strftime("%Y-%m-%d")
        try:
            return jsonify(optimize_day(store, user, day,
                                        body.get("territory_id")))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422

    @app.get("/api/field-service/my-schedule")
    @require_auth
    def my_schedule():
        user = current_user()
        day = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")
        try:
            d0, d1 = _day_bounds(day)
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 422
        username = (user.get("username") or "").lower()
        res = next((r for r in _all(store, "ServiceResource")
                    if (r.get("Username") or "").lower() == username
                    and r.get("IsActive", True)), None)
        if not res:
            return jsonify({"resource": None, "appointments": []})
        wo_by_id = _by_id(store, "WorkOrder")
        out = []
        visible_ids = {a["id"] for a in _visible_appts(user)}
        for a in _all(store, "ServiceAppointment"):
            if a.get("ServiceResourceId") != res["id"]:
                continue
            if a["id"] not in visible_ids:
                continue
            s = _parse_dt(a.get("ScheduledStart"))
            if not s or not (d0 <= s < d1):
                continue
            wo = wo_by_id.get(a.get("WorkOrderId") or "") or {}
            item = _ser(user, "ServiceAppointment", a)
            item["work_order"] = {"Id": wo.get("id"), "Subject": wo.get("Subject"),
                                  "Description": wo.get("Description"),
                                  "Priority": wo.get("Priority"),
                                  "ServiceAddress": wo.get("ServiceAddress")}
            out.append(item)
        out.sort(key=lambda a: a.get("ScheduledStart") or "")
        return jsonify({"resource": _ser(user, "ServiceResource", res),
                        "date": day, "appointments": out})

    @app.post("/api/field-service/status")
    @require_auth
    def set_status():
        user = current_user()
        body = request.json or {}
        if not body.get("appointment_id") or not body.get("status"):
            return jsonify({"error": "appointment_id and status required"}), 422
        code, data = _transition_ok(store, user, body["appointment_id"],
                                    body["status"])
        return jsonify(data), code

    # ------------------------------------------------- batch 2: inventory
    def _van_for_resource(user, resource_id):
        locs, _ = _visible_records(user, "Location")
        return next((l for l in locs
                     if l.get("ServiceResourceId") == resource_id
                     and l.get("LocationType") == "Van"), None)

    def _parse_qty(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @app.post("/api/field-service/consume-product")
    @require_auth
    def consume_product():
        user = current_user()
        body = request.json or {}
        appt = store.get("ServiceAppointment", body.get("appointment_id"))
        if not appt:
            return jsonify({"error": "appointment_id required"}), 422
        if appt["id"] not in {a["id"] for a in _visible_appts(user)}:
            return jsonify({"error": "Not found"}), 404
        res_id = appt.get("ServiceResourceId")
        if not res_id:
            return jsonify({"error": "Appointment has no assigned resource"}), 422
        product = _visible_map(user, "Product").get(body.get("product_id") or "")
        if not product:
            return jsonify({"error": "product_id required"}), 422
        qty = _parse_qty(body.get("quantity"))
        if qty <= 0:
            return jsonify({"error": "quantity must be positive"}), 422
        van = _van_for_resource(user, res_id)
        if not van:
            return jsonify({"error": "Resource has no van location"}), 422
        # Stock check + decrement + consumption record must be atomic with
        # respect to other inventory mutations in this process.
        with _FS_LOCK:
            items = _visible_map(user, "ProductItem")
            item = next((i for i in items.values()
                         if i.get("LocationId") == van["id"]
                         and i.get("ProductId") == product["id"]), None)
            on_hand = _parse_qty((item or {}).get("QuantityOnHand"))
            if not item or on_hand < qty:
                return jsonify({"error": "Insufficient stock", "on_hand": on_hand}), 422
            code, data = _do_update(user, "ProductItem", item["id"],
                                    {"QuantityOnHand": on_hand - qty})
            if code not in (200, 201):
                return jsonify(data), code
            code, data = _do_create(user, "ProductConsumed", {
                "Name": f"{product.get('Name')} x{int(qty)}",
                "WorkOrderId": appt.get("WorkOrderId"),
                "ServiceAppointmentId": appt["id"],
                "ProductId": product["id"],
                "Quantity": qty,
                "UnitPrice": 0})
            if code not in (200, 201):
                return jsonify(data), code
            return jsonify({"ok": True, "product_consumed_id": data["Id"],
                            "remaining": on_hand - qty})

    @app.get("/api/field-service/van-stock")
    @require_auth
    def van_stock():
        user = current_user()
        res = _linked_resource(store, user)
        if not res:
            return jsonify({"error": "No linked service resource"}), 404
        van = _van_for_resource(user, res["id"])
        items = []
        if van:
            prod_map = _visible_map(user, "Product")
            for i in _visible_map(user, "ProductItem").values():
                if i.get("LocationId") != van["id"]:
                    continue
                prod = prod_map.get(i.get("ProductId") or "")
                entry = _ser(user, "ProductItem", i)
                entry["product_name"] = (prod or {}).get("Name")
                items.append(entry)
        return jsonify({"resource_id": res["id"],
                        "location_id": van["id"] if van else None,
                        "items": items})

    @app.get("/api/field-service/product-requests")
    @require_auth
    def list_product_requests():
        user = current_user()
        recs, _ = _visible_records(user, "ProductRequest")
        recs.sort(key=lambda r: r.get("created_date") or "", reverse=True)
        prod_map = _visible_map(user, "Product")
        loc_map = _visible_map(user, "Location")
        out = []
        for r in recs:
            item = _ser(user, "ProductRequest", r)
            item["product_name"] = (prod_map.get(r.get("ProductId") or "") or {}).get("Name")
            item["source_name"] = (loc_map.get(r.get("SourceLocationId") or "") or {}).get("Name")
            item["destination_name"] = (loc_map.get(r.get("DestinationLocationId") or "") or {}).get("Name")
            out.append(item)
        return jsonify({"requests": out})

    @app.post("/api/field-service/product-requests")
    @require_auth
    def create_product_request():
        user = current_user()
        body = request.json or {}
        product = _visible_map(user, "Product").get(body.get("product_id") or "")
        loc_map = _visible_map(user, "Location")
        src = loc_map.get(body.get("source_location_id") or "")
        dst = loc_map.get(body.get("destination_location_id") or "")
        if not product or not src or not dst:
            return jsonify({"error": "product_id, source_location_id and "
                                     "destination_location_id required"}), 422
        qty = _parse_qty(body.get("quantity"))
        if qty <= 0:
            return jsonify({"error": "quantity must be positive"}), 422
        code, data = _do_create(user, "ProductRequest", {
            "Name": f"Request {product.get('Name')} x{int(qty)}",
            "ProductId": product["id"],
            "SourceLocationId": src["id"],
            "DestinationLocationId": dst["id"],
            "Quantity": qty,
            "Status": "Submitted",
            "RequestedBy": user.get("username")})
        return jsonify(data), code

    @app.post("/api/field-service/product-requests/<rid>/fulfill")
    @require_auth
    def fulfill_product_request(rid):
        user = current_user()
        req = _visible_map(user, "ProductRequest").get(rid)
        if not req:
            return jsonify({"error": "Not found"}), 404
        if not security.can(user, "edit", "ProductRequest"):
            return jsonify({"error": "Not found"}), 404
        if (req.get("Status") or "") not in ("Submitted", "Approved"):
            return jsonify({"error": "Only Submitted/Approved requests can be "
                                     "fulfilled"}), 422
        qty = _parse_qty(req.get("Quantity"))
        if qty <= 0:
            return jsonify({"error": "Invalid quantity"}), 422
        # The stock move + status flip must be atomic with respect to other
        # inventory mutations in this process. Re-read the request inside the
        # lock so a concurrent fulfillment is rejected instead of double-moving.
        with _FS_LOCK:
            req = store.get("ProductRequest", rid)
            if (req.get("Status") or "") not in ("Submitted", "Approved"):
                return jsonify({"error": "Only Submitted/Approved requests can be "
                                         "fulfilled"}), 422
            items = _visible_map(user, "ProductItem")
            src_item = next((i for i in items.values()
                             if i.get("LocationId") == req.get("SourceLocationId")
                             and i.get("ProductId") == req.get("ProductId")), None)
            src_qty = _parse_qty((src_item or {}).get("QuantityOnHand"))
            if not src_item or src_qty < qty:
                return jsonify({"error": "Insufficient stock at source",
                                "on_hand": src_qty}), 422
            dst_item = next((i for i in items.values()
                             if i.get("LocationId") == req.get("DestinationLocationId")
                             and i.get("ProductId") == req.get("ProductId")), None)
            code, data = _do_update(user, "ProductItem", src_item["id"],
                                    {"QuantityOnHand": src_qty - qty})
            if code not in (200, 201):
                return jsonify(data), code
            if dst_item:
                code, data = _do_update(
                    user, "ProductItem", dst_item["id"],
                    {"QuantityOnHand": _parse_qty(dst_item.get("QuantityOnHand")) + qty})
                if code not in (200, 201):
                    return jsonify(data), code
            else:
                prod = _visible_map(user, "Product").get(req.get("ProductId") or "")
                code, data = _do_create(user, "ProductItem", {
                    "Name": f"{(prod or {}).get('Name', 'Part')} replenishment",
                    "ProductId": req.get("ProductId"),
                    "LocationId": req.get("DestinationLocationId"),
                    "QuantityOnHand": qty})
                if code not in (200, 201):
                    return jsonify(data), code
            code, data = _do_update(user, "ProductRequest", rid,
                                    {"Status": "Fulfilled"})
            if code not in (200, 201):
                return jsonify(data), code
        requester = security.get_user_by_username(req.get("RequestedBy") or "")
        if requester:
            prod = _visible_map(user, "Product").get(req.get("ProductId") or "")
            store.notify(
                requester["id"], "field_service",
                f"Request fulfilled: {(prod or {}).get('Name', 'parts')} x{int(qty)}",
                "Your product request was fulfilled.", "ProductRequest", rid)
        return jsonify({"ok": True})

    # ------------------------------------------- batch 2: maintenance plans
    FREQ_DAYS = {"Weekly": 7, "Monthly": 30, "Quarterly": 91, "Yearly": 365}

    @app.post("/api/field-service/maintenance-plans/generate")
    @require_auth
    def generate_maintenance():
        user = current_user()
        today = datetime.now().strftime("%Y-%m-%d")
        recs, _ = _visible_records(user, "MaintenancePlan")
        generated, skipped = [], []
        for plan in recs:
            pid = plan["id"]
            if not plan.get("IsActive", True):
                skipped.append({"plan_id": pid, "reason": "inactive"})
                continue
            nxt = plan.get("NextRunDate") or ""
            if not nxt or nxt > today:
                skipped.append({"plan_id": pid, "reason": "not due"})
                continue
            if plan.get("EndDate") and plan.get("EndDate") < today:
                skipped.append({"plan_id": pid, "reason": "ended"})
                continue
            wo_fields = {
                "Name": plan.get("Subject") or f"Maintenance: {plan.get('Name')}",
                "Subject": plan.get("Subject") or f"Maintenance: {plan.get('Name')}",
                "Status": "New",
                "Priority": plan.get("Priority") or "Medium",
                "Description": plan.get("Description") or ""}
            for k in ("WorkTypeId", "ServiceTerritoryId", "DurationMinutes",
                      "AccountId"):
                if plan.get(k) not in (None, ""):
                    wo_fields[k] = plan[k]
            wo_code, wo_data = _do_create(user, "WorkOrder", wo_fields)
            if wo_code not in (200, 201):
                skipped.append({"plan_id": pid, "reason": "work order create failed"})
                continue
            dur = int(plan.get("DurationMinutes") or 60)
            start = f"{nxt}T09:00:00"
            end = _dt_str(datetime.strptime(start, "%Y-%m-%dT%H:%M:%S")
                          + timedelta(minutes=dur))
            sa_fields = {
                "Name": plan.get("Subject") or f"Maintenance: {plan.get('Name')}",
                "WorkOrderId": wo_data["Id"], "Status": "None",
                "ScheduledStart": start, "ScheduledEnd": end}
            if plan.get("ServiceTerritoryId"):
                sa_fields["ServiceTerritoryId"] = plan["ServiceTerritoryId"]
            sa_code, sa_data = _do_create(user, "ServiceAppointment", sa_fields)
            if sa_code not in (200, 201):
                skipped.append({"plan_id": pid, "reason": "appointment create failed"})
                continue
            new_next = (datetime.strptime(nxt, "%Y-%m-%d")
                        + timedelta(days=FREQ_DAYS.get(plan.get("Frequency"), 30))
                        ).strftime("%Y-%m-%d")
            _do_update(user, "MaintenancePlan", pid, {"NextRunDate": new_next})
            generated.append({"plan_id": pid, "work_order_id": wo_data["Id"],
                              "appointment_id": sa_data["Id"]})
        return jsonify({"generated": generated, "skipped": skipped})

    # ------------------------------------------------ batch 2: time entries
    def _may_log_for(user, appt):
        """True if caller is the appointment's technician or a dispatcher."""
        res_id = appt.get("ServiceResourceId")
        linked = _linked_resource(store, user)
        if linked and res_id and linked["id"] == res_id:
            return True
        return security.can(user, "edit", "ServiceAppointment")

    @app.post("/api/field-service/time-entries")
    @require_auth
    def create_time_entry():
        user = current_user()
        body = request.json or {}
        appt = store.get("ServiceAppointment", body.get("appointment_id"))
        if not appt:
            return jsonify({"error": "appointment_id required"}), 422
        if appt["id"] not in {a["id"] for a in _visible_appts(user)}:
            return jsonify({"error": "Not found"}), 404
        hours = _parse_qty(body.get("hours"))
        if hours <= 0:
            return jsonify({"error": "hours must be positive"}), 422
        if not appt.get("ServiceResourceId"):
            return jsonify({"error": "Appointment has no assigned resource"}), 422
        if not _may_log_for(user, appt):
            return jsonify({"error": "Not found"}), 404
        entry_type = body.get("entry_type") or "Work"
        if entry_type not in ("Work", "Travel", "Break"):
            return jsonify({"error": "entry_type must be Work, Travel or Break"}), 422
        work_date = body.get("work_date") or datetime.now().strftime("%Y-%m-%d")
        code, data = _do_create(user, "TimeEntry", {
            "Name": f"Time entry {work_date}",
            "ServiceResourceId": appt["ServiceResourceId"],
            "ServiceAppointmentId": appt["id"],
            "WorkDate": work_date,
            "Hours": hours,
            "EntryType": entry_type,
            "Notes": body.get("notes") or ""})
        return jsonify(data), code

    # ---------------------------------------------- batch 2: service reports
    @app.post("/api/field-service/service-reports")
    @require_auth
    def upsert_service_report():
        user = current_user()
        body = request.json or {}
        appt = store.get("ServiceAppointment", body.get("appointment_id"))
        if not appt:
            return jsonify({"error": "appointment_id required"}), 422
        if appt["id"] not in {a["id"] for a in _visible_appts(user)}:
            return jsonify({"error": "Not found"}), 404
        if not (body.get("signature_data") or "").strip():
            return jsonify({"error": "signature_data required"}), 422
        if not _may_log_for(user, appt):
            return jsonify({"error": "Not found"}), 404
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        fields = {"Summary": body.get("summary") or "",
                  "SignatureName": body.get("signature_name") or "",
                  "SignatureData": body.get("signature_data"),
                  "SignedAt": now}
        # Check-then-upsert must be atomic in this process so concurrent
        # submissions for the same appointment cannot create duplicates.
        with _FS_LOCK:
            reports = _visible_map(user, "ServiceReport")
            existing_id = next((rid for rid, r in reports.items()
                                if r.get("ServiceAppointmentId") == appt["id"]), None)
            if existing_id:
                code, data = _do_update(user, "ServiceReport", existing_id, fields)
            else:
                code, data = _do_create(user, "ServiceReport", {
                    "Name": f"Report for {appt.get('Name') or appt['id']}",
                    "ServiceAppointmentId": appt["id"], **fields})
            return jsonify(data), code
