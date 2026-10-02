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
    _audit, _do_create, _do_update, _visible_records, filter_change_event,
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
        # Sharing + FLS: drop events for records the caller cannot see and
        # mask snapshots/changed-fields to what the caller may read.
        visible = []
        for e in events:
            got = filter_change_event(
                user, e["object_name"], e.get("record_id"),
                e.get("snapshot"), e.get("changed_fields"))
            if got is None:
                continue
            scrubbed, fields = got
            e = dict(e)
            e["snapshot"] = scrubbed
            e["changed_fields"] = fields
            visible.append(e)
        return jsonify(visible)
