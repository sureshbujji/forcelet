"""Metadata discovery: objects, describe, layouts. — Forcelet REST API domain module.

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
