"""Change data capture. — Forcelet REST API domain module.

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
