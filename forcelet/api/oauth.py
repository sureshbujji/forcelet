"""OAuth2 tokens. — Forcelet REST API domain module.

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
from ..security import login_locked_out, record_failed_login, reset_login_attempts
from ._shared import (
    _audit, _client_ip, _do_create, _do_update, _visible_records,
    current_user, issue_session, rate_limit, require_admin, require_auth,
    serialize, ctx,
)


def register(app: Flask):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security
    # ------------------------------------------------------------ oauth2 tokens
    @app.post("/api/oauth/token")
    @rate_limit(max_requests=10, window_seconds=300,
                key_fn=lambda: "oauth:" + _client_ip())
    def oauth_token():
        import hashlib
        import secrets
        from datetime import datetime, timedelta, timezone
        body = request.json or {}
        grant = body.get("grant_type")
        user = None
        if grant == "password":
            username = (body.get("username") or "").strip()
            ip = _client_ip()
            if login_locked_out(ip, username):
                return jsonify({"error": "too many failed attempts"}), 429
            user = security.get_user_by_username(username)
            if not user or not security.check_password(user, body.get("password", "")):
                record_failed_login(ip, username)
                return jsonify({"error": "invalid_grant"}), 401
            reset_login_attempts(ip, username)
        elif grant == "refresh_token":
            rt = body.get("refresh_token") or ""
            rh = hashlib.sha256(rt.encode()).hexdigest()
            rec = store.get_refresh_token(rh)
            if rec:
                try:
                    exp = datetime.fromisoformat(rec["expires_at"])
                except Exception:
                    exp = datetime.now(timezone.utc)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp > datetime.now(timezone.utc):
                    user = security.get_user(rec["user_id"])
            store.delete_refresh_token(rh)  # rotation: single use
            if not user:
                return jsonify({"error": "invalid_grant"}), 401
        else:
            return jsonify({"error": "unsupported_grant_type"}), 400
        refresh = secrets.token_urlsafe(32)
        store.put_refresh_token(
            hashlib.sha256(refresh.encode()).hexdigest(), user["id"],
            (datetime.now(timezone.utc) + timedelta(days=30)).isoformat())
        must_change = bool(user.get("must_change_password"))
        return jsonify({"access_token": issue_session(store, user, limited=must_change),
                        "token_type": "Bearer",
                        "expires_in": 12 * 3600, "refresh_token": refresh,
                        "must_change_password": must_change})
