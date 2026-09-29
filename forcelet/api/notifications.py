"""In-app notifications. — Forcelet REST API domain module.

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
    # ------------------------------------------------------------ notifications
    @app.get("/api/notifications")
    @require_auth
    def list_notifications():
        user = request.mf_user
        unread_only = request.args.get("unread_only") == "1"
        return jsonify(store.notifications_for(user["id"],
                                               unread_only=unread_only))

    @app.get("/api/notifications/unread-count")
    @require_auth
    def notification_unread_count():
        return jsonify({"count": store.notification_unread_count(
            request.mf_user["id"])})

    @app.post("/api/notifications/read")
    @require_auth
    def notifications_read():
        body = request.json or {}
        ids = None if body.get("all") else body.get("ids")
        store.notifications_mark_read(request.mf_user["id"], ids)
        return jsonify({"ok": True})
