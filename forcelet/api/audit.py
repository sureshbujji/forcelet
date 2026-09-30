"""Setup audit trail. — Forcelet REST API domain module.

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
    # ------------------------------------------------------------ audit trail
    @app.get("/api/admin/audit-trail")
    @require_auth
    @require_admin
    def audit_trail():
        limit = max(1, min(500, int(request.args.get("limit", 50))))
        offset = max(0, int(request.args.get("offset", 0)))
        rows, total = store.audit_trail_search(
            username=request.args.get("user") or None,
            action=request.args.get("action") or None,
            entity=request.args.get("entity") or None,
            date_from=request.args.get("from") or None,
            date_to=request.args.get("to") or None,
            limit=limit, offset=offset)
        return jsonify({"rows": rows, "total": total,
                        "limit": limit, "offset": offset})

    @app.get("/api/admin/audit-trail/export")
    @require_auth
    @require_admin
    def audit_trail_export():
        rows, _ = store.audit_trail_search(
            username=request.args.get("user") or None,
            action=request.args.get("action") or None,
            entity=request.args.get("entity") or None,
            date_from=request.args.get("from") or None,
            date_to=request.args.get("to") or None,
            limit=10000, offset=0)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["When", "User", "Action", "Entity type", "Entity name",
                    "Details"])
        for a in rows:
            w.writerow([a.get("at"), a.get("username"), a.get("action"),
                        a.get("entity_type"), a.get("entity_name"),
                        a.get("details")])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition":
                                 "attachment; filename=audit-trail.csv"})
