"""Metadata packaging. — Forcelet REST API domain module.

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
