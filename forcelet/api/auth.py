"""Authentication: login and password change. — Forcelet REST API domain module.

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
    # ------------------------------------------------------------ auth
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
