"""Activity timeline. — Forcelet REST API domain module.

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
