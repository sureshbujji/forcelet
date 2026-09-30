"""Service core: case teams, entitlement management, notes. — Forcelet REST API domain module.

Covers the Salesforce Service Cloud core pieces that Forcelet was missing:

* **Case teams** — predefined teams (CaseTeamDef + CaseTeamMemberDef) that can
  be assigned to cases. Team members gain visibility of the case through the
  service-core routes (see _can_see_case).
* **Entitlement management** — Entitlement records with date-derived status,
  overlap protection, and EntitlementProcess definitions whose ordered
  milestones are stamped onto a case through the existing SLA machinery
  (``mf_case_milestones``), so the standard
  ``GET /api/sobjects/Case/<id>/milestones`` endpoint and the close-case
  completion hook keep working unchanged.
* **Notes** — a first-class Note object with polymorphic parents
  (Account/Contact/Opportunity/Case/Lead) and private-note visibility
  (owner + admins only).

Access model (documented, pragmatic): team definitions, entitlements and
entitlement processes are setup data (admin-only). Notes are governed by
*parent* visibility — anyone who can see the parent record can view/add
notes on it; private notes are additionally restricted to owner + admins.
Case-team assignment requires edit access on the case.

The module is self-contained: :func:`register` bootstraps its objects from
``metadata/fragments/service_core_objects.json`` when they are absent, so it
works on databases created before the fragment is merged into
``standard_objects.json``.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request

from .. import automation
from ._shared import (
    _audit, _do_create, _do_update, require_admin, require_auth,
    serialize, ctx,
)

SERVICE_CORE_OBJECTS = (
    "CaseTeamDef",
    "CaseTeamMemberDef",
    "Entitlement",
    "EntitlementProcess",
    "EntitlementProcessMilestone",
    "Note",
)
_TEAM_ASSIGN_TABLE = "mf_case_team_assignments"
_CASE_ENTITLEMENT_TABLE = "mf_case_entitlement"
NOTE_PARENTS = ("Account", "Contact", "Opportunity", "Case", "Lead")


def _fragment_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    return os.path.join(root, "metadata", "fragments",
                        "service_core_objects.json")


def _ensure_objects(store, registry) -> None:
    """Register service-core objects from the fragment when absent."""
    try:
        with open(_fragment_path()) as f:
            defs = json.load(f)
    except OSError:
        return
    for obj in defs:
        if registry.get_object(obj["name"]):
            continue
        store.meta_put("mf_objects", obj["name"], obj)
        store.ensure_object_table(obj)


def _ensure_config_tables(store) -> None:
    for table in (_TEAM_ASSIGN_TABLE, _CASE_ENTITLEMENT_TABLE):
        store._execute(
            f"CREATE TABLE IF NOT EXISTS {table} "
            "(id TEXT PRIMARY KEY, definition TEXT)")
    store._commit()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ------------------------------------------------------------------ case teams
def _team_members(store, team_def_id: str) -> list:
    return [m for m in store.query("CaseTeamMemberDef", limit=10000)
            if m.get("CaseTeamDefId") == team_def_id]


def _team_member_ids(store, team_def_id: str) -> set:
    return {m.get("UserId") for m in _team_members(store, team_def_id)
            if m.get("UserId")}


def _case_team_assignment(store, case_id: str):
    return store.config_get(_TEAM_ASSIGN_TABLE, case_id)


def _can_see_case(user, case_rec) -> bool:
    """Case visibility including case-team grants.

    The team-membership grant now lives in ``security.can_see_record``,
    so both the service-core routes and the generic ``/api/sobjects``
    record API honor it.
    """
    store, registry, security = ctx()
    return security.can_see_record(user, case_rec, "Case")


def _team_payload(store, team: dict) -> dict:
    members = _team_members(store, team["id"])
    return {"team": team, "members": members}


# --------------------------------------------------------------- entitlements
def _validate_entitlement_dates(values: dict):
    start, end = values.get("StartDate"), values.get("EndDate")
    if start and end and start > end:
        return jsonify({"error": "Start Date must be on or before End Date"}), 422
    return None


def _derive_status(values: dict, explicit):
    """Derive Entitlement.Status from dates.

    Dates always win over an explicit status when they contradict it: a
    lapsed entitlement is Expired and a not-yet-started one is Draft.
    Otherwise an explicit Status is honored; with no explicit value an
    in-window entitlement becomes Active.
    """
    start, end, today = values.get("StartDate"), values.get("EndDate"), _today()
    if end and end < today:
        return "Expired"
    if start and start > today:
        return "Draft"
    if explicit:
        return explicit
    if start and start <= today and (not end or end >= today):
        return "Active"
    return values.get("Status") or "Draft"


def _ranges_overlap(a_start, a_end, b_start, b_end) -> bool:
    lo = max(a_start or "", b_start or "")
    hi = min(a_end or "9999-12-31", b_end or "9999-12-31")
    return lo <= hi


def _overlap_conflict(store, ent_id, account_id, etype, start, end):
    """Another Active entitlement of the same type on the same account whose
    date range overlaps, or None."""
    if not account_id:
        return None
    for e in store.query("Entitlement", limit=10000):
        if e["id"] == ent_id or e.get("Status") != "Active":
            continue
        if (e.get("AccountId") or "") != account_id:
            continue
        if (e.get("Type") or "") != (etype or ""):
            continue
        if _ranges_overlap(start, end, e.get("StartDate"), e.get("EndDate")):
            return e
    return None


def _process_milestones(store, process_id: str) -> list:
    ms = [m for m in store.query("EntitlementProcessMilestone", limit=10000)
          if m.get("EntitlementProcessId") == process_id]
    return sorted(ms, key=lambda m: (m.get("MilestoneOrder") is None,
                                     m.get("MilestoneOrder") or 0,
                                     m.get("Name") or ""))


def _stamp_process_milestones(store, case_id: str, process: dict) -> list:
    """Stamp an entitlement process's milestones via the existing SLA table.

    Uses the same ``mf_case_milestones`` rows as ``start_case_milestones``,
    so the standard milestones endpoint and the close-case completion hook
    work unchanged. The policy name is prefixed to avoid colliding with
    priority-based SLA policies.
    """
    policy = f"Entitlement: {process['Name']}"
    now = datetime.now(timezone.utc)
    out = []
    for m in _process_milestones(store, process["id"]):
        mins = int(m.get("TargetMinutes") or 0)
        due = now + timedelta(minutes=mins)
        mid = store.config_put("mf_case_milestones", {
            "object": "Case", "record_id": case_id,
            "policy": policy, "name": m.get("Name"),
            "due_at": due.isoformat(timespec="seconds"),
            "completed_at": None, "breached": False})
        out.append(store.config_get("mf_case_milestones", mid))
    return out


def _clear_process_milestones(store, case_id: str, policy_name) -> None:
    if not policy_name:
        return
    for m in store.config_all("mf_case_milestones"):
        if m.get("record_id") == case_id and m.get("policy") == policy_name \
                and not m.get("completed_at"):
            store.config_delete("mf_case_milestones", m["id"])


def _case_entitlement_payload(store, security, user, registry, case_id: str,
                              application: dict) -> dict:
    ent = application and store.get("Entitlement", application["entitlement_id"])
    return {"case_id": case_id, "application": application,
            "entitlement": serialize(user, registry.get_object("Entitlement"), ent)
            if ent else None}


# --------------------------------------------------------------------- notes
def _note_dict(rec: dict) -> dict:
    return {
        "Id": rec["id"],
        "Title": rec.get("Title"),
        "Body": rec.get("Body"),
        "ParentId": rec.get("ParentId"),
        "ParentType": rec.get("ParentType"),
        "OwnerId": rec.get("owner_id"),
        "IsPrivate": bool(rec.get("IsPrivate")),
        "CreatedDate": rec.get("created_date"),
        "LastModifiedDate": rec.get("last_modified_date"),
    }


def _note_visible(user, note: dict) -> bool:
    """Private notes are visible to the owner and admins only."""
    store, registry, security = ctx()
    if security.is_admin(user):
        return True
    if not note.get("IsPrivate"):
        return True
    return note.get("owner_id") == user["id"]


def _get_visible_parent(user, parent_type: str, parent_id: str):
    """Return the parent record, None when missing, False when not visible."""
    store, registry, security = ctx()
    parent = store.get(parent_type, parent_id)
    if not parent:
        return None
    if parent_type == "Case":
        return parent if _can_see_case(user, parent) else False
    return parent if security.can_see_record(user, parent, parent_type) else False


# ------------------------------------------------------------------ register
def register(app: Flask):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security
    _ensure_objects(store, registry)
    _ensure_config_tables(store)

    # -------------------------------------------------------- case team defs
    @app.get("/api/service/case-teams")
    @require_auth
    @require_admin
    def sc_team_list():
        user = request.mf_user
        obj = registry.get_object("CaseTeamDef")
        return jsonify([serialize(user, obj, r)
                        for r in store.query("CaseTeamDef", limit=10000)])

    @app.post("/api/service/case-teams")
    @require_auth
    @require_admin
    def sc_team_create():
        user = request.mf_user
        st, payload = _do_create(user, "CaseTeamDef", request.json or {})
        _audit("create", "case-team", (request.json or {}).get("Name", ""))
        return jsonify(payload), st

    @app.get("/api/service/case-teams/<tid>")
    @require_auth
    @require_admin
    def sc_team_get(tid):
        user = request.mf_user
        team = store.get("CaseTeamDef", tid)
        if not team:
            return jsonify({"error": "Not found"}), 404
        obj = registry.get_object("CaseTeamDef")
        payload = _team_payload(store, team)
        payload["team"] = serialize(user, obj, team)
        return jsonify(payload)

    @app.put("/api/service/case-teams/<tid>")
    @require_auth
    @require_admin
    def sc_team_update(tid):
        user = request.mf_user
        st, payload = _do_update(user, "CaseTeamDef", tid, request.json or {})
        return jsonify(payload), st

    @app.delete("/api/service/case-teams/<tid>")
    @require_auth
    @require_admin
    def sc_team_delete(tid):
        user = request.mf_user
        team = store.get("CaseTeamDef", tid)
        if not team or not security.can(user, "delete", "CaseTeamDef"):
            return jsonify({"error": "Not found"}), 404
        for m in _team_members(store, tid):
            store.recycle_put("CaseTeamMemberDef", m, user["id"])
            store.delete("CaseTeamMemberDef", m["id"])
        for asg in store.config_all(_TEAM_ASSIGN_TABLE):
            if asg.get("team_def_id") == tid:
                store.config_delete(_TEAM_ASSIGN_TABLE, asg["id"])
        store.recycle_put("CaseTeamDef", team, user["id"])
        store.delete("CaseTeamDef", tid)
        _audit("delete", "case-team", team.get("Name") or tid)
        return jsonify({"deleted": tid})

    # ----------------------------------------------------- case team members
    @app.get("/api/service/case-teams/<tid>/members")
    @require_auth
    @require_admin
    def sc_team_members_list(tid):
        if not store.get("CaseTeamDef", tid):
            return jsonify({"error": "Not found"}), 404
        return jsonify(_team_members(store, tid))

    @app.post("/api/service/case-teams/<tid>/members")
    @require_auth
    @require_admin
    def sc_team_member_add(tid):
        user = request.mf_user
        if not store.get("CaseTeamDef", tid):
            return jsonify({"error": "Case team not found"}), 404
        body = request.json or {}
        if not security.get_user(body.get("UserId") or ""):
            return jsonify({"error": "Unknown user"}), 422
        st, payload = _do_create(
            user, "CaseTeamMemberDef",
            {"CaseTeamDefId": tid, "UserId": body.get("UserId"),
             "TeamRole": body.get("TeamRole") or "Support Agent"})
        return jsonify(payload), st

    @app.delete("/api/service/case-teams/<tid>/members/<mid>")
    @require_auth
    @require_admin
    def sc_team_member_remove(tid, mid):
        user = request.mf_user
        rec = store.get("CaseTeamMemberDef", mid)
        if not rec or rec.get("CaseTeamDefId") != tid \
                or not security.can(user, "delete", "CaseTeamMemberDef"):
            return jsonify({"error": "Not found"}), 404
        store.recycle_put("CaseTeamMemberDef", rec, user["id"])
        store.delete("CaseTeamMemberDef", mid)
        return jsonify({"deleted": mid})

    # ------------------------------------------------- assign team to a case
    @app.post("/api/service/cases/<cid>/assign-team")
    @require_auth
    def sc_case_assign_team(cid):
        user = request.mf_user
        case = store.get("Case", cid)
        if not case or not _can_see_case(user, case):
            return jsonify({"error": "Not found"}), 404
        if not security.can(user, "edit", "Case"):
            return jsonify({"error": "Edit access to the case is required"}), 403
        team = store.get("CaseTeamDef", (request.json or {}).get("team_def_id") or "")
        if not team:
            return jsonify({"error": "Case team not found"}), 404
        store.config_put(_TEAM_ASSIGN_TABLE, {
            "id": cid, "case_id": cid, "team_def_id": team["id"],
            "assigned_at": _utcnow(), "assigned_by": user["id"]})
        for uid in _team_member_ids(store, team["id"]):
            if uid != user["id"]:
                try:
                    store.notify(uid, "assignment",
                                 f"Added to case team: {team.get('Name')}",
                                 f"You were added to the case team "
                                 f"'{team.get('Name')}' on case "
                                 f"{case.get('Subject') or cid} by "
                                 f"{user.get('name')}.",
                                 "Case", cid)
                except Exception:
                    pass
        _audit("assign", "case-team", team.get("Name") or team["id"],
               f"case={cid}")
        payload = _team_payload(store, team)
        payload["team"] = serialize(
            user, registry.get_object("CaseTeamDef"), team)
        return jsonify(payload), 200

    @app.get("/api/service/cases/<cid>/team")
    @require_auth
    def sc_case_team(cid):
        user = request.mf_user
        case = store.get("Case", cid)
        if not case or not _can_see_case(user, case):
            return jsonify({"error": "Not found"}), 404
        asg = _case_team_assignment(store, cid)
        if not asg:
            return jsonify({"error": "No team assigned to this case"}), 404
        team = store.get("CaseTeamDef", asg["team_def_id"])
        if not team:
            return jsonify({"error": "Assigned team no longer exists"}), 404
        payload = _team_payload(store, team)
        payload["team"] = serialize(
            user, registry.get_object("CaseTeamDef"), team)
        payload["assigned_at"] = asg.get("assigned_at")
        return jsonify(payload)

    # -------------------------------------------------------------- entitlements
    @app.get("/api/service/entitlements")
    @require_auth
    @require_admin
    def sc_entitlement_list():
        user = request.mf_user
        obj = registry.get_object("Entitlement")
        return jsonify([serialize(user, obj, r)
                        for r in store.query("Entitlement", limit=10000)])

    @app.post("/api/service/entitlements")
    @require_auth
    @require_admin
    def sc_entitlement_create():
        user = request.mf_user
        body = dict(request.json or {})
        err = _validate_entitlement_dates(body)
        if err:
            return err
        status = _derive_status(body, body.get("Status"))
        if status == "Active" and body.get("AccountId"):
            clash = _overlap_conflict(store, None, body.get("AccountId"),
                                      body.get("Type"), body.get("StartDate"),
                                      body.get("EndDate"))
            if clash:
                return jsonify({
                    "error": "An overlapping active entitlement of the same "
                             "type already exists for this account",
                    "conflict": clash["id"]}), 422
        st, payload = _do_create(user, "Entitlement", {**body, "Status": status})
        _audit("create", "entitlement", body.get("Name", ""))
        return jsonify(payload), st

    @app.get("/api/service/entitlements/<eid>")
    @require_auth
    @require_admin
    def sc_entitlement_get(eid):
        user = request.mf_user
        rec = store.get("Entitlement", eid)
        if not rec:
            return jsonify({"error": "Not found"}), 404
        return jsonify(serialize(user, registry.get_object("Entitlement"), rec))

    @app.put("/api/service/entitlements/<eid>")
    @require_auth
    @require_admin
    def sc_entitlement_update(eid):
        user = request.mf_user
        rec = store.get("Entitlement", eid)
        if not rec:
            return jsonify({"error": "Not found"}), 404
        body = dict(request.json or {})
        merged = {**rec, **body}
        err = _validate_entitlement_dates(merged)
        if err:
            return err
        status = _derive_status(merged, body.get("Status"))
        if status == "Active" and merged.get("AccountId"):
            clash = _overlap_conflict(store, eid, merged.get("AccountId"),
                                      merged.get("Type"), merged.get("StartDate"),
                                      merged.get("EndDate"))
            if clash:
                return jsonify({
                    "error": "An overlapping active entitlement of the same "
                             "type already exists for this account",
                    "conflict": clash["id"]}), 422
        st, payload = _do_update(user, "Entitlement", eid,
                                 {**body, "Status": status})
        return jsonify(payload), st

    @app.delete("/api/service/entitlements/<eid>")
    @require_auth
    @require_admin
    def sc_entitlement_delete(eid):
        user = request.mf_user
        rec = store.get("Entitlement", eid)
        if not rec or not security.can(user, "delete", "Entitlement"):
            return jsonify({"error": "Not found"}), 404
        store.recycle_put("Entitlement", rec, user["id"])
        store.delete("Entitlement", eid)
        _audit("delete", "entitlement", rec.get("Name") or eid)
        return jsonify({"deleted": eid})

    @app.get("/api/service/accounts/<aid>/entitlements")
    @require_auth
    def sc_account_entitlements(aid):
        user = request.mf_user
        acct = store.get("Account", aid)
        if not acct or not security.can_see_record(user, acct, "Account"):
            return jsonify({"error": "Not found"}), 404
        obj = registry.get_object("Entitlement")
        recs = [r for r in store.query("Entitlement", limit=10000)
                if r.get("AccountId") == aid]
        return jsonify([serialize(user, obj, r) for r in recs])

    # ----------------------------------------------------- entitlement processes
    @app.get("/api/service/entitlement-processes")
    @require_auth
    @require_admin
    def sc_process_list():
        user = request.mf_user
        obj = registry.get_object("EntitlementProcess")
        return jsonify([serialize(user, obj, r)
                        for r in store.query("EntitlementProcess", limit=10000)])

    @app.post("/api/service/entitlement-processes")
    @require_auth
    @require_admin
    def sc_process_create():
        user = request.mf_user
        st, payload = _do_create(user, "EntitlementProcess", request.json or {})
        return jsonify(payload), st

    @app.get("/api/service/entitlement-processes/<pid>")
    @require_auth
    @require_admin
    def sc_process_get(pid):
        user = request.mf_user
        rec = store.get("EntitlementProcess", pid)
        if not rec:
            return jsonify({"error": "Not found"}), 404
        obj = registry.get_object("EntitlementProcess")
        return jsonify({"process": serialize(user, obj, rec),
                        "milestones": _process_milestones(store, pid)})

    @app.put("/api/service/entitlement-processes/<pid>")
    @require_auth
    @require_admin
    def sc_process_update(pid):
        user = request.mf_user
        st, payload = _do_update(user, "EntitlementProcess", pid,
                                 request.json or {})
        return jsonify(payload), st

    @app.delete("/api/service/entitlement-processes/<pid>")
    @require_auth
    @require_admin
    def sc_process_delete(pid):
        user = request.mf_user
        rec = store.get("EntitlementProcess", pid)
        if not rec or not security.can(user, "delete", "EntitlementProcess"):
            return jsonify({"error": "Not found"}), 404
        for m in _process_milestones(store, pid):
            store.recycle_put("EntitlementProcessMilestone", m, user["id"])
            store.delete("EntitlementProcessMilestone", m["id"])
        store.recycle_put("EntitlementProcess", rec, user["id"])
        store.delete("EntitlementProcess", pid)
        return jsonify({"deleted": pid})

    @app.get("/api/service/entitlement-processes/<pid>/milestones")
    @require_auth
    @require_admin
    def sc_process_milestones_list(pid):
        if not store.get("EntitlementProcess", pid):
            return jsonify({"error": "Not found"}), 404
        return jsonify(_process_milestones(store, pid))

    @app.post("/api/service/entitlement-processes/<pid>/milestones")
    @require_auth
    @require_admin
    def sc_process_milestone_add(pid):
        user = request.mf_user
        if not store.get("EntitlementProcess", pid):
            return jsonify({"error": "Entitlement process not found"}), 404
        body = dict(request.json or {})
        try:
            mins = int(body.get("TargetMinutes") or 0)
        except (TypeError, ValueError):
            mins = 0
        if mins <= 0:
            return jsonify({"error": "TargetMinutes must be a positive number "
                                     "of minutes"}), 422
        body["TargetMinutes"] = mins
        st, payload = _do_create(
            user, "EntitlementProcessMilestone",
            {**body, "EntitlementProcessId": pid})
        return jsonify(payload), st

    @app.delete("/api/service/entitlement-processes/<pid>/milestones/<mid>")
    @require_auth
    @require_admin
    def sc_process_milestone_remove(pid, mid):
        user = request.mf_user
        rec = store.get("EntitlementProcessMilestone", mid)
        if not rec or rec.get("EntitlementProcessId") != pid \
                or not security.can(user, "delete", "EntitlementProcessMilestone"):
            return jsonify({"error": "Not found"}), 404
        store.recycle_put("EntitlementProcessMilestone", rec, user["id"])
        store.delete("EntitlementProcessMilestone", mid)
        return jsonify({"deleted": mid})

    # ------------------------------------------------------- apply entitlement
    @app.post("/api/service/cases/<cid>/apply-entitlement")
    @require_auth
    def sc_case_apply_entitlement(cid):
        user = request.mf_user
        case = store.get("Case", cid)
        if not case or not _can_see_case(user, case):
            return jsonify({"error": "Not found"}), 404
        if not security.can(user, "edit", "Case"):
            return jsonify({"error": "Edit access to the case is required"}), 403
        ent = store.get("Entitlement", (request.json or {}).get("entitlement_id") or "")
        if not ent:
            return jsonify({"error": "Entitlement not found"}), 404
        today = _today()
        if ent.get("Status") != "Active" \
                or (ent.get("StartDate") and ent["StartDate"] > today) \
                or (ent.get("EndDate") and ent["EndDate"] < today):
            return jsonify({"error": "Entitlement is not active for today"}), 422
        existing = store.config_get(_CASE_ENTITLEMENT_TABLE, cid)
        if existing and existing.get("entitlement_id") == ent["id"]:
            return jsonify(_case_entitlement_payload(store, security, user, registry, cid, existing)), 200
        if existing:
            _clear_process_milestones(store, cid, existing.get("policy_name"))
        policy_name, stamped = None, []
        proc = ent.get("EntitlementProcessId") and \
            store.get("EntitlementProcess", ent["EntitlementProcessId"])
        if proc:
            policy_name = f"Entitlement: {proc['Name']}"
            stamped = _stamp_process_milestones(store, cid, proc)
        store.config_put(_CASE_ENTITLEMENT_TABLE, {
            "id": cid, "case_id": cid, "entitlement_id": ent["id"],
            "policy_name": policy_name, "applied_at": _utcnow(),
            "applied_by": user["id"]})
        _audit("apply", "entitlement", ent.get("Name") or ent["id"],
               f"case={cid}")
        payload = _case_entitlement_payload(
            store, security, user, registry, cid,
            store.config_get(_CASE_ENTITLEMENT_TABLE, cid))
        payload["milestones_created"] = len(stamped)
        return jsonify(payload), 200

    @app.get("/api/service/cases/<cid>/entitlement")
    @require_auth
    def sc_case_entitlement(cid):
        user = request.mf_user
        case = store.get("Case", cid)
        if not case or not _can_see_case(user, case):
            return jsonify({"error": "Not found"}), 404
        application = store.config_get(_CASE_ENTITLEMENT_TABLE, cid)
        if not application:
            return jsonify({"error": "No entitlement applied to this case"}), 404
        return jsonify(_case_entitlement_payload(store, security, user, registry, cid, application))

    # ------------------------------------------------------------------ notes
    @app.post("/api/service/notes")
    @require_auth
    def sc_note_create():
        user = request.mf_user
        body = dict(request.json or {})
        parent_type = body.get("ParentType")
        if parent_type not in NOTE_PARENTS:
            return jsonify(
                {"error": f"ParentType must be one of {list(NOTE_PARENTS)}"}), 422
        parent = _get_visible_parent(user, parent_type, body.get("ParentId") or "")
        if parent is None:
            return jsonify({"error": "Parent record not found"}), 404
        if parent is False:
            return jsonify({"error": "Not found"}), 404
        obj = registry.get_object("Note")
        clean, errors = registry.validate_record(obj, {
            "Title": body.get("Title"), "Body": body.get("Body"),
            "ParentId": body.get("ParentId"), "ParentType": parent_type,
            "OwnerId": user["id"],
            "IsPrivate": bool(body.get("IsPrivate"))})
        if errors:
            return jsonify({"error": "Validation failed", "details": errors}), 422
        clean["owner_id"] = user["id"]
        clean["created_by"] = user["id"]
        nid = store.insert("Note", clean)
        _audit("create", "note", clean.get("Title") or nid,
               f"{parent_type}={body.get('ParentId')}")
        return jsonify(_note_dict(store.get("Note", nid))), 201

    @app.get("/api/service/notes")
    @require_auth
    def sc_note_list():
        user = request.mf_user
        parent_type = request.args.get("parent_type")
        parent_id = request.args.get("parent_id")
        if parent_type not in NOTE_PARENTS or not parent_id:
            return jsonify(
                {"error": "parent_type and parent_id are required"}), 422
        parent = _get_visible_parent(user, parent_type, parent_id)
        if parent is None:
            return jsonify({"error": "Parent record not found"}), 404
        if parent is False:
            return jsonify({"error": "Not found"}), 404
        recs = [r for r in store.query("Note", limit=10000)
                if r.get("ParentType") == parent_type
                and r.get("ParentId") == parent_id
                and _note_visible(user, r)]
        return jsonify([_note_dict(r) for r in recs])

    @app.get("/api/service/notes/<nid>")
    @require_auth
    def sc_note_get(nid):
        user = request.mf_user
        rec = store.get("Note", nid)
        if not rec or not _note_visible(user, rec):
            return jsonify({"error": "Not found"}), 404
        parent = _get_visible_parent(user, rec.get("ParentType"),
                                     rec.get("ParentId"))
        if parent is False:
            return jsonify({"error": "Not found"}), 404
        return jsonify(_note_dict(rec))

    @app.put("/api/service/notes/<nid>")
    @require_auth
    def sc_note_update(nid):
        user = request.mf_user
        rec = store.get("Note", nid)
        if not rec or not _note_visible(user, rec):
            return jsonify({"error": "Not found"}), 404
        if rec.get("owner_id") != user["id"] and not security.is_admin(user):
            return jsonify({"error": "Only the owner or an admin can edit "
                                     "this note"}), 403
        body = dict(request.json or {})
        obj = registry.get_object("Note")
        values = {}
        for key in ("Title", "Body", "IsPrivate"):
            if key in body:
                values[key] = bool(body[key]) if key == "IsPrivate" else body[key]
        clean, errors = registry.validate_record(obj, values, partial=True)
        if errors:
            return jsonify({"error": "Validation failed", "details": errors}), 422
        store.update("Note", nid, clean)
        return jsonify(_note_dict(store.get("Note", nid)))

    @app.delete("/api/service/notes/<nid>")
    @require_auth
    def sc_note_delete(nid):
        user = request.mf_user
        rec = store.get("Note", nid)
        if not rec or not _note_visible(user, rec):
            return jsonify({"error": "Not found"}), 404
        if rec.get("owner_id") != user["id"] and not security.is_admin(user):
            return jsonify({"error": "Only the owner or an admin can delete "
                                     "this note"}), 403
        store.recycle_put("Note", rec, user["id"])
        store.delete("Note", nid)
        return jsonify({"deleted": nid})

    # ------------------------------------------------------------ related list
    @app.get("/api/service/related")
    @require_auth
    def sc_related():
        user = request.mf_user
        parent_type = request.args.get("parent_type")
        parent_id = request.args.get("parent_id")
        if parent_type not in NOTE_PARENTS or not parent_id:
            return jsonify(
                {"error": "parent_type and parent_id are required"}), 422
        parent = _get_visible_parent(user, parent_type, parent_id)
        if parent is None:
            return jsonify({"error": "Parent record not found"}), 404
        if parent is False:
            return jsonify({"error": "Not found"}), 404
        out = {"parent_type": parent_type, "parent_id": parent_id,
               "notes": [_note_dict(r) for r in store.query("Note", limit=10000)
                         if r.get("ParentType") == parent_type
                         and r.get("ParentId") == parent_id
                         and _note_visible(user, r)]}
        if parent_type == "Case":
            asg = _case_team_assignment(store, parent_id)
            team = asg and store.get("CaseTeamDef", asg["team_def_id"])
            if team:
                team_payload = _team_payload(store, team)
                team_payload["team"] = serialize(
                    user, registry.get_object("CaseTeamDef"), team)
                out["case_team"] = team_payload
            else:
                out["case_team"] = None
            application = store.config_get(_CASE_ENTITLEMENT_TABLE, parent_id)
            out["entitlement"] = \
                _case_entitlement_payload(store, security, user, registry,
                                             parent_id, application) \
                if application else None
            out["milestones"] = automation.case_milestones(store, parent_id)
        return jsonify(out)
