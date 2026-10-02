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
    current_user, recompute_stored_rollups, require_admin, require_auth, serialize, ctx,
)
from . import relquery


def register(app: Flask):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security
    # ------------------------------------------------------------ records
    def _can_see_view(user, view):
        return bool(view.get("shared")) or view.get("owner") == user["id"] \
            or security.is_admin(user)

    def _record_view(user, obj_name, obj, record):
        """Serialize one record, honoring ?select= / ?children= when present."""
        data = serialize(user, obj, record)
        select_param = request.args.get("select")
        children_param = request.args.get("children")
        if select_param or children_param:
            data = relquery.apply_record_view(
                user, obj_name, record, data, select_param, children_param)
        return data

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
        div_param = request.args.get("division")
        if div_param is not None:
            # Explicit division filter: a division id or name, or "global"
            # for records with no division.
            from .. import divisions as _divisions
            if div_param.strip().lower() in ("global", "none", ""):
                rows = [r for r in rows
                        if not _divisions.record_division_id(
                            store, obj_name, r.get("id") or "")]
            else:
                div = _divisions.get_division(store, div_param) \
                    or _divisions.get_division_by_name(store, div_param)
                want = div["id"] if div else div_param
                rows = [r for r in rows
                        if _divisions.record_division_id(
                            store, obj_name, r.get("id") or "") == want]
        sort_key = request.args.get("sort") or (view or {}).get("sort_by")
        reverse = (request.args.get("dir") or (view or {}).get("sort_dir") or "asc").lower() == "desc"
        if sort_key:
            rows.sort(key=lambda r: (r.get(sort_key) is None, r.get(sort_key)), reverse=reverse)
        if "limit" in request.args or "offset" in request.args:
            # Paginated envelope; without these params the legacy array
            # shape (capped at 200) is returned unchanged.
            limit = max(1, min(500, int(request.args.get("limit", 50))))
            offset = max(0, int(request.args.get("offset", 0)))
            page = rows[offset:offset + limit]
            return jsonify({"rows": [_record_view(user, obj_name, obj, r) for r in page],
                            "total": len(rows), "limit": limit,
                            "offset": offset})
        return jsonify([_record_view(user, obj_name, obj, r) for r in rows[:200]])

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
        return jsonify(_record_view(user, obj_name, obj, rec))

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
        if obj_name == "Contract" and (rec.get("Status") or "Draft") != "Draft":
            return jsonify({"error": "Only Draft Contracts can be deleted"}), 422
        blocker = datamodel.check_delete_blockers(store, registry, obj_name, rid)
        if blocker:
            return jsonify({"error": blocker}), 422
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
        try:
            cascaded = datamodel.cascade_delete(store, registry, user, obj_name, rid)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        store.recycle_put(obj_name, rec, user["id"])
        store.delete(obj_name, rid)
        recompute_stored_rollups(user, obj_name, rec)
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

    def _remap_restored_fk(child_entry, parent_obj, old_pid, new_pid):
        """Point a restored child's FKs at its parent's new id (in-memory).

        Used when the parent's original id was taken and it was restored as
        a copy: without remapping, the restored children would dangle.
        """
        codef = registry.get_object(child_entry["object_name"])
        if not codef:
            return
        data = json.loads(child_entry["data"])
        for f in datamodel.relationship_fields(codef):
            ref = f.get("reference_to")
            refs = ref if isinstance(ref, list) else [ref]
            if parent_obj in refs and str(data.get(f["name"]) or "") == str(old_pid):
                data[f["name"]] = new_pid
        child_entry["data"] = json.dumps(data)

    def _restore_entry(user, entry):
        """Restore one recycle-bin entry plus its cascade-deleted children.

        The parent is restored first so FK targets exist, then each child is
        restored recursively (depth-first). Returns (new_record_id,
        restored_count). Children the user may not create are left in the bin
        and not counted.
        """
        obj_name = entry["object_name"]
        record = json.loads(entry["data"])
        old_id = record.get("id")
        if old_id and store.get(obj_name, old_id):
            record.pop("id", None)  # id taken (e.g. re-created); restore as a copy
        new_rid = store.insert(obj_name, record)
        count = 1
        if old_id:
            for child in store.recycle_children(obj_name, old_id):
                if not security.can(user, "create", child["object_name"]):
                    continue  # leave it in the bin; not counted
                if new_rid != old_id:
                    _remap_restored_fk(child, obj_name, old_id, new_rid)
                _cid, _n = _restore_entry(user, child)
                count += _n
        store.recycle_delete(entry["id"])
        return new_rid, count

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
        new_rid, count = _restore_entry(user, entry)
        return jsonify({"restored": True, "Id": new_rid, "restored_count": count})

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
