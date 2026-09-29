"""Kanban and Path. — Forcelet REST API domain module.

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
from ..expressions import eval_expr, record_context
from ..field_types import FIELD_TYPES, validate_value
from ._shared import (
    _audit, _do_create, _do_update, _visible_records,
    current_user, require_admin, require_auth, serialize, ctx,
)


def register(app: Flask):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security
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
