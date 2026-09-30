"""Record CRUD, recycle bin, merge duplicates, list views, assistant. — Forcelet REST API domain module.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import csv
import io
import json
import os
from functools import wraps

from flask import Flask, jsonify, request, Response, send_file
from werkzeug.utils import secure_filename

from .. import automation
from .. import crypto as _crypto
from .. import datamodel
from ..expressions import eval_expr, record_context
from ..field_types import FIELD_TYPES, validate_value
from ._shared import (
    _audit, _do_create, _do_update, _visible_records,
    current_user, require_admin, require_auth, serialize, ctx,
)


def register(app: Flask):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security
    # ------------------------------------------------------------ records
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
        try:
            datamodel.assert_mutable(obj)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        automation.dispatch_webhooks(store, obj_name, "delete", serialize(user, obj, rec), user)
        terr = automation.run_triggers(store, registry, security, obj_name,
                                      "before_delete", rec, None, user)
        if terr:
            return jsonify({"error": "Trigger failed", "details": terr}), 422
        store.emit_change(obj_name, rid, "delete", user,
                          snapshot={k: v for k, v in rec.items()
                                    if not _crypto.is_encrypted(v)})
        cascaded = datamodel.cascade_delete(store, registry, user, obj_name, rid)
        store.recycle_put(obj_name, rec, user["id"])
        store.delete(obj_name, rid)
        terr = automation.run_triggers(store, registry, security, obj_name,
                                      "after_delete", rec, None, user)
        # after_delete cannot roll back; errors are surfaced as warnings
        if terr:
            return jsonify({"deleted": True, "warnings": terr,
                            "cascaded": cascaded})
        return jsonify({"deleted": True, "cascaded": cascaded})

    # ------------------------------------------------------- recycle bin
    @app.get("/api/openapi.json")
    def openapi_spec():
        from ..openapi import build_spec
        return jsonify(build_spec(app))

    @app.get("/api/recycle-bin")
    @require_auth
    def list_recycle_bin():
        user = request.mf_user
        scope = None if security.is_admin(user) else user["id"]
        return jsonify(store.recycle_list(deleted_by=scope))

    @app.post("/api/recycle-bin/<bid>/restore")
    @require_auth
    def restore_recycle_bin(bid):
        user = request.mf_user
        entry = store.recycle_get(bid)
        if not entry or (not security.is_admin(user) and entry["deleted_by"] != user["id"]):
            return jsonify({"error": "Not found"}), 404
        obj_name = entry["object_name"]
        if not security.can(user, "create", obj_name):
            return jsonify({"error": "Not permitted"}), 403
        record = json.loads(entry["data"])
        if store.get(obj_name, record.get("id")):
            record.pop("id", None)  # id taken (e.g. re-created); restore as a copy
        new_rid = store.insert(obj_name, record)
        store.recycle_delete(bid)
        return jsonify({"restored": True, "Id": new_rid})

    @app.delete("/api/recycle-bin/<bid>")
    @require_auth
    def purge_recycle_entry(bid):
        user = request.mf_user
        entry = store.recycle_get(bid)
        if not entry or (not security.is_admin(user) and entry["deleted_by"] != user["id"]):
            return jsonify({"error": "Not found"}), 404
        return jsonify({"deleted": store.recycle_delete(bid)})

    @app.delete("/api/recycle-bin")
    @require_auth
    def empty_recycle_bin():
        user = request.mf_user
        if not security.is_admin(user):
            return jsonify({"error": "Admin only"}), 403
        return jsonify({"deleted": store.recycle_clear()})

    # ------------------------------------------------------- merge duplicates
    @app.post("/api/assistant")
    @require_auth
    def assistant_chat():
        user = request.mf_user
        body = request.get_json(force=True) or {}
        message = (body.get("message") or "").strip()
        if not message:
            return jsonify({"error": "message required"}), 400
        from ..assistant import answer
        try:
            result = answer(store, registry, security, user, message)
        except Exception as e:  # never leak a stack trace to the UI
            result = {"reply": f"Something went wrong: {e}", "data": {}}
        return jsonify({"reply": result.get("reply", ""), "data": result.get("data", {})})

    @app.get("/api/sobjects/<obj_name>/<rid>/duplicates")
    @require_auth
    def record_duplicates(obj_name, rid):
        user = request.mf_user
        obj = registry.get_object(obj_name)
        rec = obj and store.get(obj_name, rid)
        if not obj or not rec or not security.can_see_record(user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        dups = automation.check_duplicates(store, obj_name, rec, exclude_id=rid)
        out = []
        for d in dups:
            hit = store.get(obj_name, d.get("record_id"))
            if hit:
                out.append(serialize(user, obj, hit))
        return jsonify(out)

    @app.post("/api/sobjects/<obj_name>/<rid>/merge")
    @require_auth
    def merge_records(obj_name, rid):
        user = request.mf_user
        obj = registry.get_object(obj_name)
        winner = obj and store.get(obj_name, rid)
        if not obj or not winner or not security.can(user, "edit", obj_name) \
                or not security.can(user, "delete", obj_name) \
                or not security.can_see_record(user, winner, obj_name):
            return jsonify({"error": "Not found"}), 404
        body = request.get_json(force=True) or {}
        merge_ids = [m for m in body.get("merge_ids", []) if m != rid]
        if not merge_ids:
            return jsonify({"error": "merge_ids required"}), 400
        fields = {k: v for k, v in (body.get("fields") or {}).items()
                  if k in registry.field_map(obj)}
        if fields:
            store.update(obj_name, rid, fields)
        # re-parent lookups pointing at the losers
        reparented = 0
        lookups = []  # (child_obj, field_name)
        for cdef in registry.list_objects():
            for f in cdef.get("fields", []):
                if f.get("type") == "Lookup" and f.get("reference_to") == obj_name \
                        and cdef["name"] != obj_name:
                    lookups.append((cdef["name"], f["name"]))
        merged = []
        for lid in merge_ids:
            loser = store.get(obj_name, lid)
            if not loser or not security.can_see_record(user, loser, obj_name):
                continue
            for child_obj, field_name in lookups:
                for child in store.query(child_obj, owner_ids=None, limit=10000):
                    if child.get(field_name) == lid:
                        store.update(child_obj, child["id"], {field_name: rid})
                        reparented += 1
            store.recycle_put(obj_name, loser, user["id"])
            store.delete(obj_name, lid)
            merged.append(lid)
        return jsonify({"merged": True, "winner": rid, "merged_ids": merged,
                        "reparented": reparented})

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
