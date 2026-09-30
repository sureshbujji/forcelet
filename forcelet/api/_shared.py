"""Shared API helpers: auth decorators, serialization, record DML.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import json
from functools import wraps

from flask import current_app, jsonify, request

from .. import automation
from .. import crypto as _crypto
from .. import datamodel as _datamodel
from ..expressions import eval_expr, record_context
from ..field_types import FIELD_TYPES, validate_value


def ctx():
    """Return (store, registry, security) for the current app."""
    return current_app.mf_store, current_app.mf_registry, current_app.mf_security



def current_user():
    store, registry, security = ctx()
    auth = request.headers.get("Authorization", "")
    token = None
    if auth.startswith("Bearer "):
        token = auth[7:]
    elif request.args.get("access_token"):
        # query-param tokens exist for browser EventSource streams, which
        # cannot set an Authorization header.
        token = request.args["access_token"]
    if not token:
        return None
    if token.startswith("mf-"):
        return security.get_user(token[3:])
    if token.startswith("mf_live_"):
        import hashlib
        rec = store.get_api_key(hashlib.sha256(token.encode()).hexdigest())
        if rec:
            store.touch_api_key(rec["key_hash"])
            return security.get_user(rec["user_id"])
    return None


def require_auth(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        user = current_user()
        if not user:
            return jsonify({"error": "Authentication required"}), 401
        request.mf_user = user
        return fn(*a, **kw)
    return wrapper


def require_admin(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        store, registry, security = ctx()
        if not security.is_admin(request.mf_user):
            return jsonify({"error": "System Administrator profile required"}), 403
        return fn(*a, **kw)
    return wrapper


def _audit(action, entity_type, name, details=""):
    store, registry, security = ctx()
    try:
        store.audit(request.mf_user, action, entity_type, name, details)
    except Exception:
        pass


def serialize(user, obj_def, record):
    store, registry, security = ctx()
    readable = set(security.readable_fields(user, obj_def))
    data = {"Id": record["id"], "OwnerId": record.get("owner_id"),
            "RecordType": record.get("record_type") or "Default",
            "CreatedDate": record.get("created_date"),
            "LastModifiedDate": record.get("last_modified_date")}
    for f in obj_def.get("fields", []):
        if f["name"] not in readable:
            continue
        if f.get("formula"):
            try:
                data[f["name"]] = eval_expr(f["formula"], record_context(record))
            except Exception:
                data[f["name"]] = None
        elif f.get("rollup"):
            try:
                data[f["name"]] = automation.compute_rollup(
                    store, security, user, f["rollup"], record["id"])
            except Exception:
                data[f["name"]] = None
        else:
            v = record.get(f["name"])
            data[f["name"]] = _crypto.decrypt(v) if f.get("encrypted") else v
    if obj_def["name"] == "Account":
        person_name = _datamodel.person_display_name(record)
        if person_name:
            data["Name"] = person_name
    return data


def _visible_records(user, obj_name):
    store, registry, security = ctx()
    obj = registry.get_object(obj_name)
    return [r for r in store.query(obj_name, owner_ids=None, limit=10000)
            if security.can_see_record(user, r, obj_name)], obj


def _do_create(user, obj_name, body, allow_duplicates=False):
    store, registry, security = ctx()
    obj = registry.get_object(obj_name)
    if not obj or not security.can(user, "create", obj_name):
        return 404, {"error": "Unknown object or no access"}
    rt = body.get("RecordType") or automation.default_record_type(store, obj_name)
    if rt != "Default" and rt not in [r["name"] for r in automation.get_record_types(store, obj_name)]:
        return 422, {"error": f"Unknown record type '{rt}'"}
    editable = set(security.editable_fields(user, obj))
    values = {k: v for k, v in body.items()
              if k in editable and k not in ("RecordType",)}
    is_person = values.get("IsPersonAccount") or rt == "PersonAccount"
    if obj_name == "Account" and is_person and "Name" in editable:
        values["IsPersonAccount"] = True
        if not values.get("Name"):
            # person accounts derive their display name from the person fields
            values["Name"] = _datamodel.person_display_name(values) or "Person Account"
    clean, errors = registry.validate_record(obj, values)
    if errors:
        return 422, {"error": "Validation failed", "details": errors}
    md_err = _datamodel.validate_md_parents_exist(store, obj, clean)
    if md_err:
        return 422, {"error": "Validation failed", "details": [md_err]}
    dups = automation.check_duplicates(store, obj_name, clean)
    if dups and not allow_duplicates:
        return 409, {"error": "Possible duplicates found", "duplicates": dups}
    clean["owner_id"] = (automation.apply_assignment_rules(
        store, registry, security, obj_name, clean, user) or user["id"])
    clean["created_by"] = user["id"]
    clean["record_type"] = rt
    terr = automation.run_triggers(store, registry, security, obj_name,
                                  "before_insert", clean, None, user)
    if terr:
        return 422, {"error": "Trigger failed", "details": terr}
    clean2, errors = registry.validate_record(obj, {k: v for k, v in clean.items()
                                                    if k in editable}, partial=True)
    if errors:
        return 422, {"error": "Validation failed", "details": errors}
    clean.update(clean2)
    vr_errors = automation.check_validation_rules(store, obj_name, {**clean, "record_type": rt})
    if vr_errors:
        return 422, {"error": "Validation rule failed", "details": vr_errors}
    rid = store.insert(obj_name, clean)
    rec = store.get(obj_name, rid)
    if obj_name == "Case":
        automation.start_case_milestones(store, obj_name, rec)
    if clean.get("owner_id") and clean["owner_id"] != user["id"]:
        owner = security.get_user(clean["owner_id"])
        if owner:
            label = clean.get("Name") or clean.get("Subject") or rid
            store.notify(owner["id"], "assignment",
                         f"{obj_name} assigned to you",
                         f"{label} was assigned to you by "
                         f"{user.get('name')}.",
                         obj_name, rid)
    terr = automation.run_triggers(store, registry, security, obj_name,
                                  "after_insert", rec, None, user)
    if terr:
        store.delete(obj_name, rid)  # compensate: roll back the insert
        return 422, {"error": "Trigger failed", "details": terr}
    automation.run_flows(store, registry, security, obj_name, "create", rec, None, user)
    automation.apply_escalation_rules(store, registry, security, obj_name,
                                      rec, None, user)
    automation.run_auto_responses(store, registry, security, obj_name, rec, user)
    automation.dispatch_webhooks(store, obj_name, "create", serialize(user, obj, rec), user)
    automation.submit_for_approval(store, security, obj_name, rec, user)
    store.emit_change(obj_name, rid, "create", user,
                      snapshot={k: v for k, v in store.get(obj_name, rid).items()
                                if not _crypto.is_encrypted(v)})
    return 201, serialize(user, obj, store.get(obj_name, rid))


def _do_update(user, obj_name, rid, body, allow_duplicates=False):
    store, registry, security = ctx()
    obj = registry.get_object(obj_name)
    rec = obj and store.get(obj_name, rid)
    if not obj or not rec or not security.can(user, "edit", obj_name) \
            or not security.can_see_record(user, rec, obj_name):
        return 404, {"error": "Not found"}
    if automation.pending_request_for(store, obj_name, rid) and not security.is_admin(user):
        return 423, {"error": "Record is locked: an approval request is pending"}
    try:
        _datamodel.assert_mutable(obj)
    except ValueError as e:
        return 422, {"error": str(e)}
    editable = set(security.editable_fields(user, obj))
    values = {k: v for k, v in body.items() if k in editable}
    clean, errors = registry.validate_record(obj, values, partial=True)
    if errors:
        return 422, {"error": "Validation failed", "details": errors}
    for f in _datamodel.md_fields(obj):
        if f["name"] in clean and clean[f["name"]] != rec.get(f["name"]) \
                and not f.get("reparentable", True):
            return 422, {"error": f"{f.get('label', f['name'])} is not reparentable"}
    md_err = _datamodel.validate_md_parents_exist(store, obj, clean)
    if md_err:
        return 422, {"error": "Validation failed", "details": [md_err]}
    working = {**rec, **clean}
    terr = automation.run_triggers(store, registry, security, obj_name,
                                  "before_update", working, rec, user)
    if terr:
        return 422, {"error": "Trigger failed", "details": terr}
    allowed = {f["name"] for f in obj["fields"]}
    folded = {k: working[k] for k in allowed
              if working.get(k) != rec.get(k) and k not in clean}
    if folded:
        clean2, errors = registry.validate_record(obj, folded, partial=True)
        if errors:
            return 422, {"error": "Validation failed", "details": errors}
        clean.update(clean2)
    merged = {**rec, **clean}
    dups = automation.check_duplicates(store, obj_name, merged, exclude_id=rid)
    if dups and not allow_duplicates:
        return 409, {"error": "Possible duplicates found", "duplicates": dups}
    vr_errors = automation.check_validation_rules(store, obj_name, merged, rec)
    if vr_errors:
        return 422, {"error": "Validation rule failed", "details": vr_errors}
    automation.log_history(store, obj_name, rid, rec, merged, user)
    store.update(obj_name, rid, clean)
    new_rec = store.get(obj_name, rid)
    if obj_name == "Case":
        automation.complete_case_milestones(store, obj_name, new_rec, rec)
    automation.apply_escalation_rules(store, registry, security, obj_name,
                                      new_rec, rec, user)
    terr = automation.run_triggers(store, registry, security, obj_name,
                                  "after_update", new_rec, rec, user)
    if terr:
        store.update(obj_name, rid, {k: rec[k] for k in clean if k in rec})  # compensate
        return 422, {"error": "Trigger failed", "details": terr}
    automation.run_flows(store, registry, security, obj_name, "update", new_rec, rec, user)
    automation.dispatch_webhooks(store, obj_name, "update", serialize(user, obj, new_rec), user)
    store.emit_change(obj_name, rid, "update", user, changed_fields=list(clean.keys()),
                      snapshot={k: v for k, v in new_rec.items()
                                if not _crypto.is_encrypted(v)})
    return 200, serialize(user, obj, new_rec)
