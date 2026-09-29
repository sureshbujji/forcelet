"""REST API for Forcelet.

Auth: POST /api/login {"username": "...", "password": "..."} -> bearer token.
(Demo default password is 'forcelet'; change it via /api/change-password.
Use HTTPS / a reverse proxy in front of the dev server for anything real.)

Every endpoint enforces profile + permission-set permissions, role-hierarchy
and criteria-based sharing, field-level security, validation rules, duplicate
rules, and approval locks.
"""
from __future__ import annotations

import csv
import io
import json
import os
from functools import wraps

from flask import Flask, jsonify, request, Response, send_file
from werkzeug.utils import secure_filename

from . import automation
from . import crypto as _crypto
from .bootstrap import bootstrap
from .expressions import eval_expr, record_context
from .field_types import FIELD_TYPES, validate_value


def create_app(db_path: str):
    store, registry, security = bootstrap(db_path)
    app = Flask(__name__)
    app.mf_store, app.mf_registry, app.mf_security = store, registry, security

    # ------------------------------------------------------------ auth
    def current_user():
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[7:]
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
            if not security.is_admin(request.mf_user):
                return jsonify({"error": "System Administrator profile required"}), 403
            return fn(*a, **kw)
        return wrapper

    def _audit(action, entity_type, name, details=""):
        try:
            store.audit(request.mf_user, action, entity_type, name, details)
        except Exception:
            pass

    @app.post("/api/login")
    def login():
        body = request.json or {}
        user = security.get_user_by_username(body.get("username", ""))
        if not user or not security.check_password(user, body.get("password", "")):
            return jsonify({"error": "Invalid username or password"}), 401
        return jsonify({"token": f"mf-{user['id']}",
                        "user": {"id": user["id"], "username": user["username"],
                                 "name": user["name"], "profile": user["profile"],
                                 "role": user["role"]}})

    @app.post("/api/change-password")
    @require_auth
    def change_password():
        body = request.json or {}
        user = request.mf_user
        if not security.check_password(user, body.get("current", "")):
            return jsonify({"error": "Current password is incorrect"}), 403
        if len(body.get("new", "")) < 8:
            return jsonify({"error": "New password must be at least 8 characters"}), 422
        security.set_password(user["id"], body["new"])
        return jsonify({"changed": True})

    # ------------------------------------------------------------ metadata
    @app.get("/api/me")
    @require_auth
    def me():
        user = dict(request.mf_user)
        user.pop("password_hash", None)
        return jsonify(user)

    @app.get("/api/objects")
    @require_auth
    def list_objects():
        user = request.mf_user
        out = []
        for o in registry.list_objects():
            if security.can(user, "read", o["name"]):
                out.append({"name": o["name"], "label": o["label"],
                            "plural": o["plural"], "is_custom": o["is_custom"],
                            "field_count": len(o.get("fields", []))})
        return jsonify(out)

    @app.get("/api/describe/<obj_name>")
    @require_auth
    def describe(obj_name):
        user = request.mf_user
        obj = registry.get_object(obj_name)
        if not obj or not security.can(user, "read", obj_name):
            return jsonify({"error": "Unknown object or no access"}), 404
        rt = request.args.get("record_type") or automation.default_record_type(store, obj_name)
        fields = []
        for f in obj.get("fields", []):
            if security.can(user, "read", obj_name, f["name"]):
                f2 = dict(f)
                if f.get("type") in ("Picklist", "MultiPicklist"):
                    f2["picklist_values"] = automation.picklist_values_for(store, obj_name, rt, f)
                f2["editable"] = (not f.get("formula") and not f.get("rollup")) and security.can(user, "edit", obj_name, f["name"])
                f2["computed"] = bool(f.get("formula") or f.get("rollup"))
                fields.append(f2)
        return jsonify({"name": obj["name"], "label": obj["label"], "plural": obj["plural"],
                        "is_custom": obj["is_custom"], "fields": fields,
                        "record_types": [{"name": r["name"], "label": r.get("label", r["name"]),
                                          "is_default": bool(r.get("is_default"))}
                                         for r in automation.get_record_types(store, obj_name)],
                        "default_record_type": automation.default_record_type(store, obj_name),
                        "permissions": {a: security.can(user, a, obj_name) for a in ("create", "read", "edit", "delete")}})

    @app.get("/api/layout/<obj_name>")
    @require_auth
    def layout(obj_name):
        user = request.mf_user
        if not security.can(user, "read", obj_name):
            return jsonify({"error": "Unknown object or no access"}), 404
        rt = request.args.get("record_type") or "Default"
        lay = store.layout_get(obj_name, user.get("profile"), rt) or {"sections": [], "related_lists": []}
        return jsonify(lay)

    # ------------------------------------------------------------ records
    def serialize(user, obj_def, record):
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
        return data

    def _visible_records(user, obj_name):
        obj = registry.get_object(obj_name)
        return [r for r in store.query(obj_name, owner_ids=None, limit=10000)
                if security.can_see_record(user, r, obj_name)], obj

    def _can_see_view(user, view):
        return bool(view.get("shared")) or view.get("owner") == user["id"] \
            or security.is_admin(user)

    @app.get("/api/sobjects/<obj_name>")
    @require_auth
    def list_records(obj_name):
        user = request.mf_user
        records, obj = _visible_records(user, obj_name)
        if not obj or not security.can(user, "read", obj_name):
            return jsonify({"error": "Unknown object or no access"}), 404
        view = None
        view_id = request.args.get("view")
        if view_id:
            view = store.config_get("mf_list_views", view_id)
            if not view or view.get("object") != obj_name or not _can_see_view(user, view):
                view = None
        filt = (view or {}).get("filters") or None
        rows = []
        for r in records:
            if filt:
                try:
                    if not eval_expr(filt, record_context(r), user=user):
                        continue
                except Exception:
                    continue
            rows.append(r)
        q = (request.args.get("search") or "").strip().lower()
        if q:
            text_fields = {f["name"] for f in obj.get("fields", [])
                           if f["type"] in ("Text", "TextArea", "Email", "Phone", "URL")}
            rows = [r for r in rows
                    if any(q in str(r.get(fn) or "").lower() for fn in text_fields)]
        sort_key = request.args.get("sort") or (view or {}).get("sort_by")
        reverse = (request.args.get("dir") or (view or {}).get("sort_dir") or "asc").lower() == "desc"
        if sort_key:
            rows.sort(key=lambda r: (r.get(sort_key) is None, r.get(sort_key)), reverse=reverse)
        return jsonify([serialize(user, obj, r) for r in rows[:200]])

    # Shared create/update pipelines (used by the REST endpoints, upsert,
    # web-to-lead, and CSV import). Return (status_code, payload).
    def _do_create(user, obj_name, body, allow_duplicates=False):
        obj = registry.get_object(obj_name)
        if not obj or not security.can(user, "create", obj_name):
            return 404, {"error": "Unknown object or no access"}
        rt = body.get("RecordType") or automation.default_record_type(store, obj_name)
        if rt != "Default" and rt not in [r["name"] for r in automation.get_record_types(store, obj_name)]:
            return 422, {"error": f"Unknown record type '{rt}'"}
        editable = set(security.editable_fields(user, obj))
        values = {k: v for k, v in body.items()
                  if k in editable and k not in ("RecordType",)}
        clean, errors = registry.validate_record(obj, values)
        if errors:
            return 422, {"error": "Validation failed", "details": errors}
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
        obj = registry.get_object(obj_name)
        rec = obj and store.get(obj_name, rid)
        if not obj or not rec or not security.can(user, "edit", obj_name) \
                or not security.can_see_record(user, rec, obj_name):
            return 404, {"error": "Not found"}
        if automation.pending_request_for(store, obj_name, rid) and not security.is_admin(user):
            return 423, {"error": "Record is locked: an approval request is pending"}
        editable = set(security.editable_fields(user, obj))
        values = {k: v for k, v in body.items() if k in editable}
        clean, errors = registry.validate_record(obj, values, partial=True)
        if errors:
            return 422, {"error": "Validation failed", "details": errors}
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

    @app.post("/api/sobjects/<obj_name>")
    @require_auth
    def create_record(obj_name):
        status, payload = _do_create(
            request.mf_user, obj_name, request.json or {},
            allow_duplicates=request.args.get("allow_duplicates") == "true")
        return jsonify(payload), status

    @app.get("/api/sobjects/<obj_name>/<rid>")
    @require_auth
    def get_record(obj_name, rid):
        user = request.mf_user
        obj = registry.get_object(obj_name)
        rec = obj and store.get(obj_name, rid)
        if not obj or not rec or not security.can(user, "read", obj_name) \
                or not security.can_see_record(user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        return jsonify(serialize(user, obj, rec))

    @app.patch("/api/sobjects/<obj_name>/<rid>")
    @require_auth
    def update_record(obj_name, rid):
        status, payload = _do_update(
            request.mf_user, obj_name, rid, request.json or {},
            allow_duplicates=request.args.get("allow_duplicates") == "true")
        return jsonify(payload), status

    @app.put("/api/sobjects/<obj_name>/upsert/<field_name>/<path:value>")
    @require_auth
    def upsert_record(obj_name, field_name, value):
        """Update the record whose external ID matches, or create it.

        <field_name> must be flagged as an external ID on the object.
        """
        user = request.mf_user
        obj = registry.get_object(obj_name)
        if not obj:
            return jsonify({"error": "Unknown object"}), 404
        fdef = next((f for f in obj.get("fields", [])
                     if f["name"] == field_name and f.get("external_id")), None)
        if not fdef:
            return jsonify({"error": f"'{field_name}' is not an external ID field"
                                     f" on {obj_name}"}), 422
        ok, norm, err = validate_value(fdef, value)
        if not ok:
            return jsonify({"error": err}), 422
        matches = [r for r in store.query(obj_name, owner_ids=None, limit=10000)
                   if r.get(field_name) == norm
                   and security.can_see_record(user, r, obj_name)]
        if len(matches) > 1:
            return jsonify({"error": "Multiple records match this external ID"}), 409
        body = {k: v for k, v in (request.json or {}).items() if k != field_name}
        allow = request.args.get("allow_duplicates") == "true"
        if matches:
            status, payload = _do_update(user, obj_name, matches[0]["id"], body, allow)
        else:
            if not security.can(user, "create", obj_name):
                return jsonify({"error": "Unknown object or no access"}), 404
            status, payload = _do_create(user, obj_name,
                                         {**body, field_name: norm}, allow)
        if status in (200, 201):
            payload = {"created": status == 201, **payload}
        return jsonify(payload), status

    @app.post("/api/sobjects/Lead/<rid>/convert")
    @require_auth
    def convert_lead_ep(rid):
        """Convert a Lead into an Account + Contact (+ Opportunity)."""
        result, err = automation.convert_lead(store, registry, security, rid,
                                              request.mf_user, request.json or {})
        if err:
            return jsonify({"error": err}), 422
        return jsonify(result), 201

    @app.get("/api/sobjects/Lead/<rid>/conversion")
    @require_auth
    def lead_conversion_ep(rid):
        user = request.mf_user
        lead = store.get("Lead", rid)
        if not lead or not security.can_see_record(user, lead, "Lead"):
            return jsonify({"error": "Not found"}), 404
        return jsonify(store.lead_conversion(rid))

    @app.delete("/api/sobjects/<obj_name>/<rid>")
    @require_auth
    def delete_record(obj_name, rid):
        user = request.mf_user
        obj = registry.get_object(obj_name)
        rec = obj and store.get(obj_name, rid)
        if not obj or not rec or not security.can(user, "delete", obj_name) \
                or not security.can_see_record(user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        if automation.pending_request_for(store, obj_name, rid) and not security.is_admin(user):
            return jsonify({"error": "Record is locked: an approval request is pending"}), 423
        automation.dispatch_webhooks(store, obj_name, "delete", serialize(user, obj, rec), user)
        terr = automation.run_triggers(store, registry, security, obj_name,
                                      "before_delete", rec, None, user)
        if terr:
            return jsonify({"error": "Trigger failed", "details": terr}), 422
        store.emit_change(obj_name, rid, "delete", user,
                          snapshot={k: v for k, v in rec.items()
                                    if not _crypto.is_encrypted(v)})
        store.delete(obj_name, rid)
        terr = automation.run_triggers(store, registry, security, obj_name,
                                      "after_delete", rec, None, user)
        # after_delete cannot roll back; errors are surfaced as warnings
        if terr:
            return jsonify({"deleted": True, "warnings": terr})
        return jsonify({"deleted": True})

    @app.get("/api/list-views/<obj_name>")
    @require_auth
    def get_list_views(obj_name):
        user = request.mf_user
        return jsonify([v for v in store.config_all("mf_list_views")
                        if v.get("object") == obj_name and _can_see_view(user, v)])

    @app.post("/api/list-views")
    @require_auth
    def create_list_view():
        user = request.mf_user
        body = request.json or {}
        if not registry.get_object(body.get("object") or ""):
            return jsonify({"error": "Unknown object"}), 422
        if body.get("shared") and not security.is_admin(user):
            return jsonify({"error": "Only admins can create shared list views"}), 403
        body = {"name": body.get("name") or "Untitled view",
                "object": body["object"],
                "columns": body.get("columns") or [],
                "filters": body.get("filters") or {},
                "sort_by": body.get("sort_by"),
                "sort_dir": body.get("sort_dir") or "asc",
                "shared": bool(body.get("shared")),
                "owner": user["id"]}
        rid = store.config_put("mf_list_views", body)
        return jsonify(store.config_get("mf_list_views", rid)), 201

    @app.delete("/api/list-views/<vid>")
    @require_auth
    def delete_list_view(vid):
        user = request.mf_user
        view = store.config_get("mf_list_views", vid)
        if not view:
            return jsonify({"error": "Not found"}), 404
        if view.get("owner") != user["id"] and not security.is_admin(user):
            return jsonify({"error": "Forbidden"}), 403
        return jsonify({"deleted": store.config_delete("mf_list_views", vid)})

    @app.get("/api/sobjects/<obj_name>/<rid>/history")
    @require_auth
    def record_history(obj_name, rid):
        user = request.mf_user
        obj = registry.get_object(obj_name)
        rec = obj and store.get(obj_name, rid)
        if not obj or not rec or not security.can(user, "read", obj_name) \
                or not security.can_see_record(user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        users = {u["id"]: u["name"] for u in security.list_users()}
        out = []
        for h in automation.get_history(store, obj_name, rid):
            out.append({**h, "changed_by_name": users.get(h["changed_by"], h["changed_by"])})
        return jsonify(out)

    # ------------------------------------------------------------ approvals
    @app.get("/api/approvals")
    @require_auth
    def approval_inbox():
        user = request.mf_user
        reqs = automation.pending_for_user(store, security, user)
        users = {u["id"]: u["name"] for u in security.list_users()}
        out = []
        for r in reqs:
            rec = store.get(r["object"], r["record_id"])
            out.append({**r, "submitted_by_name": users.get(r["submitted_by"], "?"),
                        "record_label": (rec or {}).get("Name") or r["record_id"]})
        return jsonify(out)

    @app.post("/api/sobjects/<obj_name>/<rid>/submit-approval")
    @require_auth
    def submit_approval(obj_name, rid):
        user = request.mf_user
        obj = registry.get_object(obj_name)
        rec = obj and store.get(obj_name, rid)
        if not obj or not rec or not security.can_see_record(user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        req, err = automation.submit_for_approval(store, security, obj_name, rec, user)
        if err:
            return jsonify({"error": err}), 422
        return jsonify(req), 201

    @app.post("/api/approvals/<req_id>/approve")
    @require_auth
    def approve(req_id):
        req, err = automation.decide_request(store, security, req_id, request.mf_user, True,
                                             (request.json or {}).get("comment", ""))
        return (jsonify(req), 200) if req else (jsonify({"error": err}), 422)

    @app.post("/api/approvals/<req_id>/reject")
    @require_auth
    def reject(req_id):
        req, err = automation.decide_request(store, security, req_id, request.mf_user, False,
                                             (request.json or {}).get("comment", ""))
        return (jsonify(req), 200) if req else (jsonify({"error": err}), 422)

    # ------------------------------------------------------------ files
    FILES_DIR = os.path.join(os.path.dirname(os.path.abspath(db_path)), "files")
    os.makedirs(FILES_DIR, exist_ok=True)
    MAX_FILE_BYTES = 10 * 1024 * 1024

    @app.post("/api/sobjects/<obj_name>/<rid>/files")
    @require_auth
    def upload_file(obj_name, rid):
        user = request.mf_user
        obj = registry.get_object(obj_name)
        rec = obj and store.get(obj_name, rid)
        if not obj or not rec or not security.can_see_record(user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "No file uploaded"}), 422
        data = f.read()
        if len(data) > MAX_FILE_BYTES:
            return jsonify({"error": "File is too large (10 MB max)"}), 422
        fid = store.file_put(obj_name, rid, secure_filename(f.filename),
                             f.mimetype, len(data), user)
        with open(os.path.join(FILES_DIR, fid), "wb") as fh:
            fh.write(data)
        return jsonify(store.file_get(fid)), 201

    @app.get("/api/sobjects/<obj_name>/<rid>/files")
    @require_auth
    def list_files(obj_name, rid):
        user = request.mf_user
        obj = registry.get_object(obj_name)
        rec = obj and store.get(obj_name, rid)
        if not obj or not rec or not security.can_see_record(user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        return jsonify(store.files_for_record(obj_name, rid))

    @app.get("/api/files/<fid>")
    @require_auth
    def download_file(fid):
        user = request.mf_user
        meta = store.file_get(fid)
        if not meta:
            return jsonify({"error": "Not found"}), 404
        rec = store.get(meta["object_name"], meta["record_id"])
        if not rec or not security.can_see_record(user, rec, meta["object_name"]):
            return jsonify({"error": "Not found"}), 404
        path = os.path.join(FILES_DIR, fid)
        if not os.path.exists(path):
            return jsonify({"error": "File content missing"}), 404
        return send_file(path, download_name=meta["filename"],
                         mimetype=meta["mime_type"])

    @app.delete("/api/files/<fid>")
    @require_auth
    def delete_file(fid):
        user = request.mf_user
        meta = store.file_get(fid)
        if not meta:
            return jsonify({"error": "Not found"}), 404
        rec = store.get(meta["object_name"], meta["record_id"])
        if not rec or not security.can_see_record(user, rec, meta["object_name"]):
            return jsonify({"error": "Not found"}), 404
        if meta["uploaded_by"] != user["id"] and not security.is_admin(user):
            return jsonify({"error": "Only the uploader or an admin can delete "
                                     "this file"}), 403
        store.file_delete(fid)
        try:
            os.remove(os.path.join(FILES_DIR, fid))
        except OSError:
            pass
        return jsonify({"deleted": True})

    # ------------------------------------------------------------ notifications
    @app.get("/api/notifications")
    @require_auth
    def list_notifications():
        user = request.mf_user
        unread_only = request.args.get("unread_only") == "1"
        return jsonify(store.notifications_for(user["id"],
                                               unread_only=unread_only))

    @app.get("/api/notifications/unread-count")
    @require_auth
    def notification_unread_count():
        return jsonify({"count": store.notification_unread_count(
            request.mf_user["id"])})

    @app.post("/api/notifications/read")
    @require_auth
    def notifications_read():
        body = request.json or {}
        ids = None if body.get("all") else body.get("ids")
        store.notifications_mark_read(request.mf_user["id"], ids)
        return jsonify({"ok": True})

    # ------------------------------------------------------------ lead scoring (ML)
    @app.post("/api/admin/ml/train-lead-scoring")
    @require_auth
    @require_admin
    def train_lead_scoring():
        from datetime import datetime, timezone
        from . import ml as _ml
        leads = store.query("Lead", owner_ids=None, limit=10000)
        try:
            model = _ml.train_lead_scoring(leads)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        model["trained_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        model["trained_by"] = request.mf_user["id"]
        store.config_put("mf_ml_models", {"id": "lead_scoring", **model})
        _audit("train", "ml_model", "lead_scoring",
               f"{model['samples']} samples, accuracy {model['accuracy']}")
        return jsonify({k: v for k, v in model.items() if k != "weights"})

    @app.get("/api/sobjects/Lead/<rid>/score")
    @require_auth
    def lead_score(rid):
        from . import ml as _ml
        user = request.mf_user
        rec = store.get("Lead", rid)
        if not rec or not security.can_see_record(user, rec, "Lead"):
            return jsonify({"error": "Not found"}), 404
        model = store.config_get("mf_ml_models", "lead_scoring")
        if not model:
            return jsonify({"error": "Scoring model has not been trained yet"}), 404
        out = _ml.score_lead(model, rec)
        out["trained_at"] = model.get("trained_at")
        out["model_accuracy"] = model.get("accuracy")
        return jsonify(out)

    # ------------------------------------------------------------ global search
    @app.get("/api/search")
    @require_auth
    def global_search():
        user = request.mf_user
        q = (request.args.get("q") or "").strip().lower()
        if not q:
            return jsonify([])
        out = []
        for obj_name in [o["name"] for o in registry.list_objects()]:
            obj = registry.get_object(obj_name)
            if not security.can(user, "read", obj_name):
                continue
            readable = set(security.readable_fields(user, obj))
            text_fields = [f["name"] for f in obj.get("fields", [])
                           if f["name"] in readable
                           and f["type"] in ("Text", "TextArea", "Email", "Phone", "URL")]
            if not text_fields:
                continue
            matches = []
            records, _obj = _visible_records(user, obj_name)
            for rec in records:
                ctx = record_context(rec)  # decrypts encrypted fields for searching
                if any(isinstance(ctx.get(fn), str) and q in ctx[fn].lower()
                       for fn in text_fields):
                    matches.append(serialize(user, obj, rec))
                    if len(matches) >= 5:
                        break
            if matches:
                out.append({"object": obj_name,
                            "label": obj.get("plural") or obj.get("label_plural") or obj_name,
                            "records": matches})
        return jsonify(out)

    # ------------------------------------------------------------ activities
    @app.get("/api/sobjects/<obj_name>/<rid>/activities")
    @require_auth
    def get_activities(obj_name, rid):
        user = request.mf_user
        obj = registry.get_object(obj_name)
        rec = obj and store.get(obj_name, rid)
        if not obj or not rec or not security.can(user, "read", obj_name) \
                or not security.can_see_record(user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        return jsonify(store.get_activities(obj_name, rid))

    @app.post("/api/sobjects/<obj_name>/<rid>/activities")
    @require_auth
    def add_activity(obj_name, rid):
        user = request.mf_user
        obj = registry.get_object(obj_name)
        rec = obj and store.get(obj_name, rid)
        if not obj or not rec or not security.can(user, "read", obj_name) \
                or not security.can_see_record(user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        body = request.json or {}
        atype = (body.get("type") or "note").lower()
        if atype not in ("note", "call", "task", "email"):
            return jsonify({"error": "type must be note, call, task, or email"}), 422
        aid = store.add_activity(obj_name, rid, atype,
                                 body.get("subject", ""), body.get("body", ""), user)
        return jsonify({"id": aid}), 201

    # ------------------------------------------------------------ email
    @app.post("/api/sobjects/<obj_name>/<rid>/send-email")
    @require_auth
    def send_email(obj_name, rid):
        user = request.mf_user
        obj = registry.get_object(obj_name)
        rec = obj and store.get(obj_name, rid)
        if not obj or not rec or not security.can(user, "read", obj_name) \
                or not security.can_see_record(user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        body = request.json or {}
        tpl = store.config_get("mf_email_templates", body.get("template_id")) \
            if body.get("template_id") else None
        merged_tpl = {"subject": body.get("subject") or (tpl or {}).get("subject") or "",
                      "body": body.get("body") or (tpl or {}).get("body") or "",
                      "name": (tpl or {}).get("name", "")}
        recipient, subject = automation.send_templated_email(
            store, obj_name, rec, merged_tpl, user, to_addr=body.get("to") or "")
        # Demo delivery: the email is logged and added to the activity
        # timeline. Point FORCELET_SMTP at a real relay to actually send.
        return jsonify({"sent": True, "to": recipient, "subject": subject,
                        "delivered": bool(os.environ.get("FORCELET_SMTP"))})

    @app.get("/api/email-templates")
    @require_auth
    def email_templates():
        return jsonify(store.config_all("mf_email_templates"))

    # ------------------------------------------------------------ chatter feed
    @app.get("/api/feed")
    @require_auth
    def feed():
        user = request.mf_user
        obj_name = request.args.get("object")
        record_id = request.args.get("record_id")
        if obj_name and record_id:
            obj = registry.get_object(obj_name)
            rec = obj and store.get(obj_name, record_id)
            if not obj or not rec or not security.can(user, "read", obj_name) \
                    or not security.can_see_record(user, rec, obj_name):
                return jsonify({"error": "Not found"}), 404
            posts = store.feed_for_record(obj_name, record_id)
        elif obj_name or record_id:
            return jsonify({"error": "object and record_id are required together"}), 422
        else:
            posts = [p for p in store.feed_home(user["id"])
                     if not p.get("object_name")
                     or (store.get(p["object_name"], p["record_id"])
                         and security.can(user, "read", p["object_name"])
                         and security.can_see_record(
                             user, store.get(p["object_name"], p["record_id"]),
                             p["object_name"]))]
        for p in posts:
            p["liked_by_me"] = store.feed_liked_by(p["id"], user["id"])
        return jsonify(posts)

    @app.post("/api/feed")
    @require_auth
    def feed_post_ep():
        user = request.mf_user
        body = request.json or {}
        post, err = automation.post_to_feed(store, security, user,
                                            body.get("object"), body.get("record_id"),
                                            body.get("body", ""))
        if err:
            return jsonify({"error": err}), 422
        post["liked_by_me"] = False
        for u in automation.find_mentioned_users(store, body.get("body", "")):
            if u["id"] != user["id"]:
                store.notify(u["id"], "mention",
                             f"{user.get('name')} mentioned you",
                             (body.get("body", "") or "")[:140],
                             body.get("object"), body.get("record_id"))
        return jsonify(post), 201

    @app.get("/api/feed/<pid>/comments")
    @require_auth
    def feed_comments(pid):
        if not store.feed_get_post(pid):
            return jsonify({"error": "Not found"}), 404
        return jsonify(store.feed_comments(pid))

    @app.post("/api/feed/<pid>/comments")
    @require_auth
    def feed_add_comment(pid):
        user = request.mf_user
        if not store.feed_get_post(pid):
            return jsonify({"error": "Not found"}), 404
        body = ((request.json or {}).get("body") or "").strip()
        if not body:
            return jsonify({"error": "Comment body is required"}), 422
        cid = store.feed_add_comment(pid, user["id"], body)
        return jsonify({"id": cid}), 201

    @app.post("/api/feed/<pid>/like")
    @require_auth
    def feed_like(pid):
        if not store.feed_get_post(pid):
            return jsonify({"error": "Not found"}), 404
        store.feed_like(pid, request.mf_user["id"])
        return jsonify({"liked": True})

    @app.delete("/api/feed/<pid>/like")
    @require_auth
    def feed_unlike(pid):
        store.feed_unlike(pid, request.mf_user["id"])
        return jsonify({"liked": False})

    @app.post("/api/feed/follow")
    @require_auth
    def feed_follow():
        user = request.mf_user
        body = request.json or {}
        obj_name, record_id = body.get("object"), body.get("record_id")
        obj = registry.get_object(obj_name or "")
        rec = obj and store.get(obj_name, record_id)
        if not obj or not rec or not security.can(user, "read", obj_name) \
                or not security.can_see_record(user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        store.feed_follow(user["id"], obj_name, record_id)
        return jsonify({"following": True}), 201

    @app.delete("/api/feed/follow")
    @require_auth
    def feed_unfollow():
        user = request.mf_user
        ok = store.feed_unfollow(user["id"], request.args.get("object"),
                                 request.args.get("record_id"))
        return jsonify({"following": not ok})

    @app.get("/api/feed/following")
    @require_auth
    def feed_following():
        return jsonify(store.feed_follows_for(request.mf_user["id"]))

    # ------------------------------------------------------------ kanban + path
    @app.get("/api/kanban/<obj_name>")
    @require_auth
    def kanban(obj_name):
        user = request.mf_user
        obj = registry.get_object(obj_name)
        if not obj or not security.can(user, "read", obj_name):
            return jsonify({"error": "Unknown object or no access"}), 404
        group_by = request.args.get("group_by")
        picklists = [f for f in obj.get("fields", [])
                     if f["type"] == "Picklist"
                     and security.can(user, "read", obj_name, f["name"])]
        field = next((f for f in picklists if f["name"] == group_by), None) \
            or (picklists[0] if picklists else None)
        if not field:
            return jsonify({"error": "No picklist field to group by"}), 422
        values = field.get("picklist_values") or []
        columns = [{"value": v, "records": []} for v in values]
        blank = {"value": None, "records": []}
        by_value = {v: c for v, c in zip(values, columns)}
        records, _ = _visible_records(user, obj_name)
        for r in records:
            (by_value.get(r.get(field["name"])) or blank)["records"].append(
                serialize(user, obj, r))
        columns = [c for c in columns + [blank]
                   if c["records"] or c["value"] is not None]
        return jsonify({"object": obj_name, "group_by": field["name"],
                        "group_label": field["label"], "columns": columns})

    @app.get("/api/paths/<obj_name>")
    @require_auth
    def get_path_ep(obj_name):
        user = request.mf_user
        if not registry.get_object(obj_name) or not security.can(user, "read", obj_name):
            return jsonify({"error": "Unknown object or no access"}), 404
        return jsonify(automation.get_path(store, obj_name))

    @app.post("/api/admin/paths")
    @require_auth
    @require_admin
    def create_path():
        body = request.json or {}
        if not registry.get_object(body.get("object") or ""):
            return jsonify({"error": "Unknown object"}), 422
        rid = store.config_put("mf_paths", {
            "name": body.get("name") or f"{body['object']} path",
            "object": body["object"], "field": body.get("field"),
            "guidance": body.get("guidance") or {},
            "active": body.get("active", True)})
        _audit("create", "paths", body.get("object"))
        return jsonify(store.config_get("mf_paths", rid)), 201

    @app.delete("/api/admin/paths/<pid>")
    @require_auth
    @require_admin
    def delete_path(pid):
        old = store.config_get("mf_paths", pid)
        ok = store.config_delete("mf_paths", pid)
        if ok:
            _audit("delete", "paths", (old or {}).get("object") or pid)
        return jsonify({"deleted": ok})

    # ------------------------------------------------------------ web-to-lead (public)
    W2L_FIELDS = ["FirstName", "LastName", "Company", "Email", "Phone", "Rating"]

    @app.get("/api/public/web-to-lead")
    def web_to_lead_form():
        fields = "".join(
            f'<label>{f}</label>'
            f'<input name="{f}"{" required" if f in ("LastName", "Company") else ""}>'
            for f in W2L_FIELDS)
        return Response(
            '<!doctype html><html><head><meta charset="utf-8"><title>Contact us</title>'
            "<style>body{font-family:sans-serif;max-width:480px;margin:40px auto;padding:0 16px}"
            "label{display:block;margin:12px 0 4px;font-weight:600}"
            "input{width:100%;padding:8px;box-sizing:border-box}"
            "button{margin-top:16px;padding:10px 24px}</style></head><body>"
            f"<h2>Contact us</h2><form method='post' action='/api/public/web-to-lead'>"
            f"{fields}<button type='submit'>Submit</button></form></body></html>",
            mimetype="text/html")

    @app.post("/api/public/web-to-lead")
    def web_to_lead_submit():
        data = request.get_json(silent=True) or request.form.to_dict() or {}
        values = {f: data.get(f) for f in W2L_FIELDS if data.get(f) not in (None, "")}
        values["LeadSource"] = "Web"
        # Public submissions flow through the normal pipeline as the admin
        # user: validation, duplicate bypass, assignment rules, flows,
        # auto-response rules, and approval entry all apply.
        admin = security.get_user_by_username("admin")
        status, payload = _do_create(admin, "Lead", values, allow_duplicates=True)
        if status == 201:
            if request.form:
                return Response("<p>Thanks — we got your details and will be in touch.</p>",
                                mimetype="text/html")
            return jsonify({"created": True, "id": payload["Id"]}), 201
        return jsonify(payload), status

    # ------------------------------------------------------------ web-to-case (public)
    W2C_FIELDS = ["FirstName", "LastName", "Email", "Subject", "Priority",
                  "Description"]

    @app.get("/api/public/web-to-case")
    def web_to_case_form():
        fields = "".join(
            f'<label>{f}</label>'
            f'<input name="{f}"{" required" if f in ("LastName", "Subject") else ""}>'
            for f in W2C_FIELDS)
        return Response(
            '<!doctype html><html><head><meta charset="utf-8"><title>Open a support case</title>'
            "<style>body{font-family:sans-serif;max-width:480px;margin:40px auto;padding:0 16px}"
            "label{display:block;margin:12px 0 4px;font-weight:600}"
            "input{width:100%;padding:8px;box-sizing:border-box}"
            "button{margin-top:16px;padding:10px 24px}</style></head><body>"
            f"<h2>Open a support case</h2><form method='post' action='/api/public/web-to-case'>"
            f"{fields}<button type='submit'>Submit</button></form></body></html>",
            mimetype="text/html")

    @app.post("/api/public/web-to-case")
    def web_to_case_submit():
        data = request.get_json(silent=True) or request.form.to_dict() or {}
        values = {f: data.get(f) for f in W2C_FIELDS if data.get(f) not in (None, "")}
        values["Origin"] = "Web"
        values.setdefault("Priority", "Medium")
        # public submissions run through the normal pipeline as the admin user
        admin = security.get_user_by_username("admin")
        status, payload = _do_create(admin, "Case", values, allow_duplicates=True)
        if status == 201:
            if request.form:
                return Response("<p>Thanks — your support case was created. "
                                "We'll be in touch shortly.</p>",
                                mimetype="text/html")
            return jsonify({"created": True, "id": payload["Id"]}), 201
        return jsonify(payload), status

    @app.post("/api/public/email-to-case")
    def email_to_case():
        """Inbound-email webhook: point an email service's inbound-parse
        webhook here. JSON body: {from_name, from_email, subject, body}."""
        data = request.get_json(silent=True) or {}
        subject = (data.get("subject") or "(no subject)")[:120]
        sender = f"{data.get('from_name') or ''} <{data.get('from_email') or ''}>".strip()
        desc = (f"From: {sender}\n\n" if sender.strip("<> ") else "") + (data.get("body") or "")
        values = {"Subject": subject, "Description": desc,
                  "Origin": "Email", "Priority": "Medium"}
        admin = security.get_user_by_username("admin")
        status, payload = _do_create(admin, "Case", values, allow_duplicates=True)
        if status == 201:
            return jsonify({"created": True, "id": payload["Id"]}), 201
        return jsonify(payload), status

    # ------------------------------------------------------------ case milestones
    @app.get("/api/sobjects/<obj_name>/<rid>/milestones")
    @require_auth
    def record_milestones(obj_name, rid):
        obj = registry.get_object(obj_name)
        rec = obj and store.get(obj_name, rid)
        if not obj or not rec or not security.can_see_record(
                request.mf_user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        return jsonify(automation.case_milestones(store, rid))

    # ------------------------------------------------------------ named credentials + callouts
    def _scrub_credential(c):
        c = dict(c)
        secret = c.pop("secret_enc", None)
        c.pop("secret", None)
        c["has_secret"] = bool(secret)
        return c

    @app.get("/api/admin/named-credentials")
    @require_auth
    @require_admin
    def list_named_credentials():
        return jsonify([_scrub_credential(c)
                        for c in store.config_all("mf_named_credentials")])

    @app.post("/api/admin/named-credentials")
    @require_auth
    @require_admin
    def upsert_named_credential():
        body = request.json or {}
        if not body.get("name"):
            return jsonify({"error": "name is required"}), 422
        if not (body.get("url") or "").startswith(("http://", "https://")):
            return jsonify({"error": "url must start with http(s)://"}), 422
        if (body.get("auth_type") or "none") not in ("none", "basic", "bearer", "api_key"):
            return jsonify({"error": "unknown auth_type"}), 422
        existing = next((c for c in store.config_all("mf_named_credentials")
                         if c.get("name") == body["name"]), None)
        rec = dict(existing or {})
        for k in ("name", "url", "auth_type", "username", "api_key_header",
                  "active"):
            if k in body:
                rec[k] = body[k]
        rec.setdefault("auth_type", "none")
        rec.setdefault("active", True)
        if body.get("secret"):
            rec["secret_enc"] = _crypto.encrypt(body["secret"])
        rid = store.config_put("mf_named_credentials", rec)
        _audit("upsert", "named-credentials", body["name"])
        return jsonify(_scrub_credential(
            store.config_get("mf_named_credentials", rid))), 201

    @app.delete("/api/admin/named-credentials/<rid>")
    @require_auth
    @require_admin
    def delete_named_credential(rid):
        old = store.config_get("mf_named_credentials", rid)
        ok = store.config_delete("mf_named_credentials", rid)
        if ok:
            _audit("delete", "named-credentials", (old or {}).get("name") or rid)
        return jsonify({"deleted": ok})

    @app.post("/api/admin/callouts/invoke")
    @require_auth
    @require_admin
    def invoke_callout_ep():
        body = request.json or {}
        res = automation.invoke_callout(
            store, body.get("credential") or "",
            method=body.get("method") or "GET",
            path=body.get("path") or "",
            headers=body.get("headers") or {},
            body=body.get("body"))
        _audit("invoke", "callout", body.get("credential") or "")
        return jsonify(res)

    # ------------------------------------------------------------ forecasts
    @app.get("/api/forecasts")
    @require_auth
    def forecasts():
        try:
            return jsonify(automation.forecast_for_period(
                store, security, request.args.get("period"),
                request.mf_user))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422

    @app.get("/api/admin/forecast-quotas")
    @require_auth
    @require_admin
    def list_forecast_quotas():
        return jsonify(store.config_all("mf_forecast_quotas"))

    @app.post("/api/admin/forecast-quotas")
    @require_auth
    @require_admin
    def upsert_forecast_quota():
        body = request.json or {}
        import re
        if not re.fullmatch(r"\d{4}-\d{2}", body.get("period") or ""):
            return jsonify({"error": "period must be YYYY-MM"}), 422
        if not security.get_user(body.get("user_id") or ""):
            return jsonify({"error": "Unknown user"}), 422
        try:
            quota = float(body.get("quota"))
        except (TypeError, ValueError):
            return jsonify({"error": "quota must be a number"}), 422
        existing = next((q for q in store.config_all("mf_forecast_quotas")
                         if q.get("user_id") == body["user_id"]
                         and q.get("period") == body["period"]), None)
        rec = dict(existing or {})
        rec.update({"user_id": body["user_id"], "period": body["period"],
                    "quota": quota})
        rid = store.config_put("mf_forecast_quotas", rec)
        _audit("upsert", "forecast-quota",
               f"{body['user_id']} {body['period']}")
        return jsonify(store.config_get("mf_forecast_quotas", rid)), 201

    @app.delete("/api/admin/forecast-quotas/<rid>")
    @require_auth
    @require_admin
    def delete_forecast_quota(rid):
        ok = store.config_delete("mf_forecast_quotas", rid)
        return jsonify({"deleted": ok})

    # ------------------------------------------------------------ screen flows
    def _screen_flow_or_404(fid):
        f = store.config_get("mf_flows", fid)
        if not f or f.get("flow_type") != "screen" \
                or not f.get("active", True):
            return None
        return f

    @app.get("/api/screen-flows")
    @require_auth
    def list_screen_flows():
        flows = [f for f in store.config_all("mf_flows")
                 if f.get("flow_type") == "screen" and f.get("active", True)]
        return jsonify([{"id": f["id"], "name": f.get("name"),
                         "screens": len(f.get("screens") or [])}
                        for f in flows])

    @app.post("/api/screen-flows/<fid>/start")
    @require_auth
    def start_screen_flow_ep(fid):
        flow = _screen_flow_or_404(fid)
        if not flow:
            return jsonify({"error": "Unknown screen flow"}), 404
        try:
            run = automation.start_screen_flow(store, flow, request.mf_user)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        screens = flow.get("screens") or []
        return jsonify({"run_id": run["id"],
                        "screen": automation._public_screen(screens[0]),
                        "total_screens": len(screens)}), 201

    @app.get("/api/screen-flows/runs/<rid>")
    @require_auth
    def get_screen_flow_run(rid):
        run = store.config_get("mf_flow_runs", rid)
        if not run or (run.get("user_id") != request.mf_user["id"]
                       and not security.is_admin(request.mf_user)):
            return jsonify({"error": "Not found"}), 404
        flow = store.config_get("mf_flows", run.get("flow_id") or "")
        screens = (flow.get("screens") or []) if flow else []
        idx = int(run.get("current") or 0)
        screen = (automation._public_screen(screens[idx])
                  if run.get("status") == "in_progress" and idx < len(screens)
                  else None)
        return jsonify({"run": run, "screen": screen})

    @app.post("/api/screen-flows/runs/<rid>/next")
    @require_auth
    def advance_screen_flow_ep(rid):
        run = store.config_get("mf_flow_runs", rid)
        if not run or (run.get("user_id") != request.mf_user["id"]
                       and not security.is_admin(request.mf_user)):
            return jsonify({"error": "Not found"}), 404
        if run.get("status") != "in_progress":
            return jsonify({"error": "Flow run is already complete"}), 422
        flow = store.config_get("mf_flows", run.get("flow_id") or "")
        if not flow:
            return jsonify({"error": "Unknown flow"}), 404
        run, screen, result = automation.advance_screen_flow(
            store, registry, security, flow, run,
            (request.json or {}).get("values") or {}, request.mf_user)
        if not result.get("ok") and result.get("errors"):
            return jsonify({"screen": screen,
                            "errors": result["errors"]}), 422
        return jsonify({"run_id": run["id"], "status": run["status"],
                        "screen": screen, "result": result})

    # ------------------------------------------------------------ scheduled jobs
    @app.post("/api/admin/scheduled-jobs/<jid>/run")
    @require_auth
    @require_admin
    def run_scheduled_job_now(jid):
        job = store.config_get("mf_scheduled_jobs", jid)
        if not job:
            return jsonify({"error": "Unknown job"}), 404
        users = {u["username"]: u for u in security.list_users()}
        run_as = users.get(job.get("run_as") or "admin") or users.get("admin")
        res = automation.run_scheduled_job(store, registry, security, job, run_as)
        from datetime import datetime, timezone
        job["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        store.config_put("mf_scheduled_jobs", job)
        return jsonify(res)

    @app.get("/api/admin/scheduled-runs")
    @require_auth
    @require_admin
    def scheduled_runs():
        return jsonify(store.scheduled_runs(request.args.get("job_id")))

    @app.get("/api/admin/email-log")
    @require_auth
    @require_admin
    def email_log():
        return jsonify(store.email_log(limit=int(request.args.get("limit", 100))))

    # ------------------------------------------------------------ packaging
    @app.get("/api/admin/packages/export")
    @require_auth
    @require_admin
    def export_package():
        pkg = automation.build_package(store, registry)
        return Response(json.dumps(pkg, indent=1), mimetype="application/json",
                        headers={"Content-Disposition":
                                 "attachment; filename=forcelet-package.json"})

    @app.post("/api/admin/packages/import")
    @require_auth
    @require_admin
    def import_package_ep():
        body = request.json or {}
        try:
            summary = automation.import_package(store, registry, body.get("package") or {},
                                                request.mf_user)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("import", "package", (body.get("package") or {}).get("name", "package"),
               json.dumps(summary))
        return jsonify(summary)

    # ------------------------------------------------------------ audit trail
    @app.get("/api/admin/audit-trail")
    @require_auth
    @require_admin
    def audit_trail():
        return jsonify(store.audit_trail(limit=int(request.args.get("limit", 200))))

    # ------------------------------------------------------------ change data capture
    @app.get("/api/change-events")
    @require_auth
    def change_events():
        user = request.mf_user
        obj_name = request.args.get("object")
        if obj_name and not security.can(user, "read", obj_name):
            return jsonify({"error": "Not found"}), 404
        events = store.change_events(
            since=int(request.args.get("since", 0)),
            object_name=obj_name,
            record_id=request.args.get("record_id"),
            limit=min(int(request.args.get("limit", 200)), 1000))
        if not obj_name:
            events = [e for e in events if security.can(user, "read", e["object_name"])]
        return jsonify(events)

    # ------------------------------------------------------------ oauth2 tokens
    @app.post("/api/oauth/token")
    def oauth_token():
        import hashlib
        import secrets
        from datetime import datetime, timedelta, timezone
        body = request.json or {}
        grant = body.get("grant_type")
        user = None
        if grant == "password":
            user = security.get_user_by_username(body.get("username", ""))
            if not user or not security.check_password(user, body.get("password", "")):
                return jsonify({"error": "invalid_grant"}), 401
        elif grant == "refresh_token":
            rt = body.get("refresh_token") or ""
            rh = hashlib.sha256(rt.encode()).hexdigest()
            rec = store.get_refresh_token(rh)
            if rec:
                try:
                    exp = datetime.fromisoformat(rec["expires_at"])
                except Exception:
                    exp = datetime.now(timezone.utc)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp > datetime.now(timezone.utc):
                    user = security.get_user(rec["user_id"])
            store.delete_refresh_token(rh)  # rotation: single use
            if not user:
                return jsonify({"error": "invalid_grant"}), 401
        else:
            return jsonify({"error": "unsupported_grant_type"}), 400
        refresh = secrets.token_urlsafe(32)
        store.put_refresh_token(
            hashlib.sha256(refresh.encode()).hexdigest(), user["id"],
            (datetime.now(timezone.utc) + timedelta(days=30)).isoformat())
        return jsonify({"access_token": f"mf-{user['id']}", "token_type": "Bearer",
                        "expires_in": 86400, "refresh_token": refresh})

    # ------------------------------------------------------------ api keys
    @app.get("/api/api-keys")
    @require_auth
    def list_api_keys():
        return jsonify(store.api_keys_for(request.mf_user["id"]))

    @app.post("/api/api-keys")
    @require_auth
    def create_api_key():
        import hashlib
        import secrets
        body = request.json or {}
        raw = "mf_live_" + secrets.token_urlsafe(32)
        kid = store.put_api_key(hashlib.sha256(raw.encode()).hexdigest(),
                                body.get("name") or "api key",
                                request.mf_user["id"])
        return jsonify({"id": kid, "name": body.get("name") or "api key",
                        "key": raw,
                        "warning": "Copy this key now - it is never shown again."}), 201

    @app.delete("/api/api-keys/<kid>")
    @require_auth
    def revoke_api_key(kid):
        return jsonify({"deleted": store.delete_api_key(kid, request.mf_user["id"])})

    # ------------------------------------------------------------ reports
    @app.get("/api/reports")
    @require_auth
    def list_reports():
        return jsonify(store.config_all("mf_reports"))

    @app.post("/api/admin/reports")
    @require_auth
    @require_admin
    def create_report():
        rid = store.config_put("mf_reports", request.json or {})
        _audit("create", "reports", (request.json or {}).get("name") or rid)
        return jsonify(store.config_get("mf_reports", rid)), 201

    @app.get("/api/reports/<rep_id>/run")
    @require_auth
    def run_report(rep_id):
        user = request.mf_user
        rep = store.config_get("mf_reports", rep_id)
        if not rep:
            return jsonify({"error": "Unknown report"}), 404
        obj_name = rep["object"]
        obj = registry.get_object(obj_name)
        if not obj or not security.can(user, "read", obj_name):
            return jsonify({"error": "No access"}), 404
        records, _ = _visible_records(user, obj_name)
        filt = rep.get("filters") or {}
        rows = []
        for r in records:
            try:
                if filt and not eval_expr(filt, record_context(r)):
                    continue
            except Exception:
                continue
            rows.append(serialize(user, obj, r))
        group_by, agg = rep.get("group_by"), rep.get("aggregate") or {}
        groups = None
        if group_by:
            groups = {}
            for row in rows:
                key = row.get(group_by) or "(blank)"
                g = groups.setdefault(key, {"key": key, "count": 0, "aggregate": None, "_sum": 0.0, "_n": 0})
                g["count"] += 1
                if agg.get("func") in ("sum", "avg") and isinstance(row.get(agg.get("field")), (int, float)):
                    g["_sum"] += row[agg["field"]]
                    g["_n"] += 1
            for g in groups.values():
                if agg.get("func") == "sum":
                    g["aggregate"] = g["_sum"]
                elif agg.get("func") == "avg":
                    g["aggregate"] = g["_sum"] / g["_n"] if g["_n"] else None
                del g["_sum"]
                del g["_n"]
            groups = sorted(groups.values(), key=lambda g: g["count"], reverse=True)
        return jsonify({"report": rep["name"], "row_count": len(rows),
                        "columns": rep.get("columns") or [], "rows": rows[:500], "groups": groups})

    # ------------------------------------------------------------ import/export
    @app.get("/api/sobjects/<obj_name>/export")
    @require_auth
    def export_csv(obj_name):
        user = request.mf_user
        records, obj = _visible_records(user, obj_name)
        if not obj or not security.can(user, "read", obj_name):
            return jsonify({"error": "Unknown object or no access"}), 404
        fields = [f for f in obj.get("fields", []) if security.can(user, "read", obj_name, f["name"])]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Id", "RecordType"] + [f["name"] for f in fields])
        for r in records:
            s = serialize(user, obj, r)
            w.writerow([s["Id"], s["RecordType"]] + [s.get(f["name"], "") for f in fields])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={obj_name}.csv"})

    @app.post("/api/admin/import/<obj_name>")
    @require_auth
    @require_admin
    def import_csv(obj_name):
        obj = registry.get_object(obj_name)
        if not obj:
            return jsonify({"error": "Unknown object"}), 404
        file = request.files.get("file")
        if not file:
            return jsonify({"error": "Upload a CSV file as 'file'"}), 422
        reader = csv.DictReader(io.StringIO(file.read().decode("utf-8-sig")))
        mode = (request.args.get("mode") or "insert").lower()
        ext_field = request.args.get("external_id_field") or ""
        if mode == "upsert":
            ext_def = next((f for f in obj.get("fields", [])
                            if f["name"] == ext_field and f.get("external_id")), None)
            if not ext_def:
                return jsonify({"error": f"'{ext_field}' is not an external ID field"
                                         f" on {obj_name}"}), 422
        created, updated, failed, errors = 0, 0, 0, []
        for i, row in enumerate(reader, start=2):
            row = {k: v for k, v in row.items() if k not in ("Id",)}
            if mode == "upsert":
                key = (row.get(ext_field) or "").strip()
                if not key:
                    failed += 1
                    errors.append({"row": i, "details":
                                   [f"Missing external ID '{ext_field}'"]})
                    continue
                match = next((r for r in store.query(obj_name, owner_ids=None, limit=10000)
                              if str(r.get(ext_field) or "") == key), None)
                row.pop(ext_field, None)
                if match:
                    status, payload = _do_update(request.mf_user, obj_name,
                                                 match["id"], row,
                                                 allow_duplicates=True)
                else:
                    row[ext_field] = key
                    status, payload = _do_create(request.mf_user, obj_name, row,
                                                 allow_duplicates=True)
                if status == 201:
                    created += 1
                elif status == 200:
                    updated += 1
                else:
                    failed += 1
                    errors.append({"row": i, "details": [payload.get("error")]})
                continue
            clean, errs = registry.validate_record(obj, {k: v for k, v in row.items() if k not in ("Id",)})
            vr = automation.check_validation_rules(store, obj_name, clean)
            if errs or vr:
                failed += 1
                errors.append({"row": i, "details": errs + vr})
                continue
            clean["owner_id"] = request.mf_user["id"]
            clean["created_by"] = request.mf_user["id"]
            clean.setdefault("record_type", automation.default_record_type(store, obj_name))
            store.insert(obj_name, clean)
            created += 1
        return jsonify({"created": created, "updated": updated,
                        "failed": failed, "errors": errors[:20]})

    # ------------------------------------------------------------ admin: config
    CONFIG_TABLES = {
        "validation-rules": "mf_validation_rules",
        "flows": "mf_flows",
        "approval-processes": "mf_approval_processes",
        "sharing-rules": "mf_sharing_rules",
        "matching-rules": "mf_matching_rules",
        "record-types": "mf_record_types",
        "permission-sets": "mf_permission_sets",
        "webhooks": "mf_webhooks",
        "triggers": "mf_triggers",
        "scheduled-jobs": "mf_scheduled_jobs",
        "assignment-rules": "mf_assignment_rules",
        "email-templates": "mf_email_templates",
        "auto-response-rules": "mf_auto_responses",
        "sla-policies": "mf_sla_policies",
        "escalation-rules": "mf_escalation_rules",
    }

    @app.get("/api/admin/<kind>")
    @require_auth
    @require_admin
    def admin_list_config(kind):
        table = CONFIG_TABLES.get(kind)
        if not table:
            return jsonify({"error": "Unknown config type"}), 404
        return jsonify(store.config_all(table))

    @app.post("/api/admin/<kind>")
    @require_auth
    @require_admin
    def admin_create_config(kind):
        table = CONFIG_TABLES.get(kind)
        if not table:
            return jsonify({"error": "Unknown config type"}), 404
        body = request.json or {}
        if kind == "record-types" and body.get("object") and not registry.get_object(body["object"]):
            return jsonify({"error": "Unknown object"}), 422
        if kind == "triggers":
            if not registry.get_object(body.get("object") or ""):
                return jsonify({"error": "Unknown object"}), 422
            bad = [e for e in (body.get("events") or []) if e not in automation.TRIGGER_EVENTS]
            if bad:
                return jsonify({"error": "Unknown trigger events", "details": bad}), 422
            try:
                compile(body.get("code") or "", "<trigger>", "exec")
            except SyntaxError as e:
                return jsonify({"error": "Trigger code has a syntax error", "details": str(e)}), 422
        if kind == "assignment-rules":
            if not registry.get_object(body.get("object") or ""):
                return jsonify({"error": "Unknown object"}), 422
            a = body.get("assignee") or {}
            if a.get("type") not in ("user", "round_robin"):
                return jsonify({"error": "assignee.type must be 'user' or 'round_robin'"}), 422
        if kind == "scheduled-jobs":
            try:
                compile(body.get("code") or "", "<scheduled>", "exec")
            except SyntaxError as e:
                return jsonify({"error": "Job code has a syntax error", "details": str(e)}), 422
            if int(body.get("interval_minutes") or 0) <= 0:
                return jsonify({"error": "interval_minutes must be positive"}), 422
        rid = store.config_put(table, body)
        _audit("create", kind, body.get("name") or rid)
        return jsonify(store.config_get(table, rid)), 201

    @app.delete("/api/admin/<kind>/<rid>")
    @require_auth
    @require_admin
    def admin_delete_config(kind, rid):
        table = CONFIG_TABLES.get(kind)
        if not table:
            return jsonify({"error": "Unknown config type"}), 404
        old = store.config_get(table, rid)
        ok = store.config_delete(table, rid)
        if ok:
            _audit("delete", kind, (old or {}).get("name") or rid)
        return jsonify({"deleted": ok})

    @app.post("/api/admin/objects")
    @require_auth
    @require_admin
    def admin_create_object():
        body = request.json or {}
        try:
            obj = registry.create_object(body.get("name", ""), body.get("label", ""),
                                         body.get("plural", ""))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("create", "object", obj["name"])
        return jsonify(obj), 201

    @app.post("/api/admin/objects/<obj_name>/fields")
    @require_auth
    @require_admin
    def admin_add_field(obj_name):
        try:
            field = registry.add_field(obj_name, request.json or {})
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("create", "field", f"{obj_name}.{field['name']}")
        return jsonify(field), 201

    @app.get("/api/admin/users")
    @require_auth
    @require_admin
    def admin_list_users():
        return jsonify([{k: v for k, v in u.items() if k != "password_hash"}
                        for u in security.list_users()])

    @app.post("/api/admin/users")
    @require_auth
    @require_admin
    def admin_create_user():
        body = request.json or {}
        try:
            user = security.create_user(body.get("username", ""), body.get("name", ""),
                                        body.get("profile", ""), body.get("role"),
                                        body.get("password", "forcelet"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        user = {k: v for k, v in user.items() if k != "password_hash"}
        _audit("create", "user", user["username"])
        return jsonify(user), 201

    @app.post("/api/admin/users/<uid>/permission-sets")
    @require_auth
    @require_admin
    def admin_assign_ps(uid, ):
        try:
            user = security.assign_permission_set(uid, (request.json or {}).get("permission_set", ""))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("assign", "permission-set", (request.json or {}).get("permission_set", ""),
               f"user={user['username']}")
        return jsonify({k: v for k, v in user.items() if k != "password_hash"})

    @app.get("/api/admin/roles")
    @require_auth
    @require_admin
    def admin_list_roles():
        return jsonify(security.list_roles())

    @app.post("/api/admin/roles")
    @require_auth
    @require_admin
    def admin_create_role():
        body = request.json or {}
        try:
            role = security.create_role(body.get("name", ""), body.get("parent"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("create", "role", role["name"])
        return jsonify(role), 201

    @app.get("/api/admin/profiles")
    @require_auth
    @require_admin
    def admin_list_profiles():
        return jsonify(security.list_profiles())

    @app.post("/api/admin/profiles")
    @require_auth
    @require_admin
    def admin_create_profile():
        body = request.json or {}
        try:
            profile = security.create_profile(body.get("name", ""),
                                              body.get("object_permissions", {}),
                                              body.get("field_permissions", {}))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("create", "profile", profile["name"])
        return jsonify(profile), 201

    @app.post("/api/admin/layouts")
    @require_auth
    @require_admin
    def admin_save_layout():
        body = request.json or {}
        if not body.get("object") or not registry.get_object(body["object"]):
            return jsonify({"error": "Unknown object"}), 422
        store.layout_put(body["object"], body.get("profile", "Default"),
                         {"sections": body.get("sections", []),
                          "related_lists": body.get("related_lists", [])},
                         body.get("record_type", "Default"))
        _audit("save", "layout", f"{body['object']}/{body.get('profile', 'Default')}")
        return jsonify({"saved": True})

    @app.get("/api/admin/webhook-deliveries")
    @require_auth
    @require_admin
    def admin_webhook_deliveries():
        rows = store._execute(
            "SELECT * FROM mf_webhook_deliveries ORDER BY attempted_at DESC LIMIT 100").fetchall()
        return jsonify([dict(r) for r in rows])

    @app.get("/api/field-types")
    @require_auth
    def field_types():
        return jsonify([{"name": n, "description": v["desc"]} for n, v in FIELD_TYPES.items()])

    return app
