"""Authentication: login, logout, and password change. — Forcelet REST API domain module.

Session model: login mints an unguessable token (``mf_sess_<random>``); only
its SHA-256 hash is stored server-side with an expiry. Tokens are validated
on every request (see ``_shared.current_user``) and can be revoked via
/logout or by changing the password.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, jsonify, request, Response, send_file
from werkzeug.utils import secure_filename

from .. import automation
from .. import crypto as _crypto
from ..expressions import eval_expr, record_context
from ..field_types import FIELD_TYPES, validate_value
from ..security import (SESSION_MAX_SECONDS, SESSION_TTL_SECONDS, hash_token,
                        login_locked_out, new_session_token,
                        record_failed_login, reset_login_attempts)
from ._shared import (
    _audit, _client_ip, _do_create, _do_update, _visible_records,
    current_user, issue_session, rate_limit, require_admin, require_auth,
    serialize, ctx,
)

log = logging.getLogger("forcelet.auth")


def register(app: Flask):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security
    # ------------------------------------------------------------ auth
    @app.post("/api/login")
    @rate_limit(max_requests=10, window_seconds=300,
                key_fn=lambda: "login:" + _client_ip())
    def login():
        body = request.json or {}
        username = (body.get("username") or "").strip()
        ip = _client_ip()
        if login_locked_out(ip, username):
            log.warning("login locked out ip=%s username=%s", ip, username)
            return jsonify({"error": "Too many failed attempts. Try again in 15 minutes."}), 429
        user = security.get_user_by_username(username)
        if not user or not security.check_password(user, body.get("password", "")):
            record_failed_login(ip, username)
            log.warning("login failed ip=%s username=%s", ip, username)
            return jsonify({"error": "Invalid username or password"}), 401
        reset_login_attempts(ip, username)
        must_change = bool(user.get("must_change_password"))
        token = issue_session(store, user, limited=must_change)
        resp = jsonify({"token": token,
                        "must_change_password": must_change,
                        "user": {"id": user["id"], "username": user["username"],
                                 "name": user["name"], "profile": user["profile"],
                                 "role": user["role"]}})
        # Never allow login responses (which carry a fresh token) to be cached.
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Pragma"] = "no-cache"
        return resp

    @app.post("/api/logout")
    @require_auth
    def logout():
        sess = getattr(request, "mf_session", None)
        if sess:
            store.delete_session(sess["token_hash"])
        return jsonify({"ok": True})

    @app.get("/api/session")
    @require_auth
    def session_info():
        sess = getattr(request, "mf_session", None)
        if not sess:
            return jsonify({"type": "api_key"})
        return jsonify({"type": "session",
                        "expires_at": sess["expires_at"],
                        "last_seen_at": sess["last_seen_at"],
                        "limited": bool(sess.get("limited"))})

    @app.post("/api/change-password")
    @require_auth
    def change_password():
        body = request.json or {}
        user = request.mf_user
        if not security.check_password(user, body.get("current", "")):
            return jsonify({"error": "Current password is incorrect"}), 403
        new = body.get("new", "")
        if len(new) < 8:
            return jsonify({"error": "New password must be at least 8 characters"}), 422
        if new == user.get("username"):
            return jsonify({"error": "New password must differ from the username"}), 422
        security.set_password(user["id"], new)
        user = security.get_user(user["id"])
        user.pop("must_change_password", None)
        store.meta_put("mf_users", user["id"], user)
        sess = getattr(request, "mf_session", None)
        if sess:
            # This session is now fully trusted; kill every other session so
            # a compromised old password cannot linger anywhere.
            store.unlimit_session(sess["token_hash"])
            store.delete_user_sessions(user["id"], except_hash=sess["token_hash"])
        _audit("password_change", "User", user["username"])
        return jsonify({"changed": True})
