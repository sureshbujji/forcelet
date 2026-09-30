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
from ..security import (hash_token,
                        login_locked_out, new_session_token,
                        record_failed_login, reset_login_attempts,
                        session_timeouts, totp_required_for_user)
from ..settings import SECURITY_KEY, check_password_policy, get_settings
from ..store import utcnow
from ._shared import (
    _audit, _client_ip, _do_create, _do_update, _visible_records,
    current_user, issue_session, rate_limit, require_admin, require_auth,
    serialize, ctx,
)

log = logging.getLogger("forcelet.auth")


def _apply_password_expiry(store, security, user, sec) -> None:
    """Force a password change at login when the password is older than the
    configured expiry. Users without a recorded set-time start the clock now
    instead of being forced immediately."""
    days = int(sec.get("password_expiry_days", 0) or 0)
    if not days or user.get("must_change_password"):
        return
    set_at = user.get("password_set_at")
    if not set_at:
        user["password_set_at"] = utcnow()
        store.meta_put("mf_users", user["id"], user)
        return
    try:
        born = datetime.fromisoformat(set_at)
    except (ValueError, TypeError):
        return
    if born.tzinfo is None:
        born = born.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if born.tzinfo is None:
        born = born.replace(tzinfo=timezone.utc)
    if (now - born).days >= days:
        user["must_change_password"] = True
        store.meta_put("mf_users", user["id"], user)


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
        ua = request.headers.get("User-Agent", "")
        sec = get_settings(store, SECURITY_KEY)
        if login_locked_out(ip, username, sec):
            mins = sec["lockout_duration_minutes"]
            log.warning("login locked out ip=%s username=%s", ip, username)
            store.record_login(None, username, ip, ua, False, "locked out")
            return jsonify({"error": f"Too many failed attempts. "
                                     f"Try again in {mins} minutes."}), 429
        user = security.get_user_by_username(username)
        if not user or not security.check_password(user, body.get("password", "")):
            record_failed_login(ip, username, sec)
            log.warning("login failed ip=%s username=%s", ip, username)
            store.record_login(user["id"] if user else None, username,
                               ip, ua, False, "invalid credentials")
            return jsonify({"error": "Invalid username or password"}), 401
        if not user.get("is_active", True):
            store.record_login(user["id"], username, ip, ua, False,
                               "account deactivated")
            return jsonify({"error": "This account has been deactivated. "
                                     "Contact your administrator."}), 403
        reset_login_attempts(ip, username)
        _apply_password_expiry(store, security, user, sec)
        store.record_login(user["id"], username, ip, ua, True)
        must_change = bool(user.get("must_change_password"))
        # Org 2FA policy: required but not enrolled -> limited session that
        # can only finish enrollment; the app stays unusable until then.
        totp_needed = totp_required_for_user(store, user) \
            and not user.get("totp_secret")
        token = issue_session(store, user, limited=(must_change or totp_needed))
        resp = jsonify({"token": token,
                        "must_change_password": must_change,
                        "totp_setup_required": totp_needed,
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
        sec = get_settings(store, SECURITY_KEY)
        err = check_password_policy(new, user.get("username"), sec)
        if err:
            return jsonify({"error": err}), 422
        security.set_password(user["id"], new)
        user = security.get_user(user["id"])
        user.pop("must_change_password", None)
        store.meta_put("mf_users", user["id"], user)
        sess = getattr(request, "mf_session", None)
        # If 2FA is required and still not enrolled, keep the session limited
        # so the user can only finish enrollment.
        totp_needed = totp_required_for_user(store, user) \
            and not user.get("totp_secret")
        if sess and not totp_needed:
            # This session is now fully trusted; kill every other session so
            # a compromised old password cannot linger anywhere.
            store.unlimit_session(sess["token_hash"])
            store.delete_user_sessions(user["id"], except_hash=sess["token_hash"])
        _audit("password_change", "User", user["username"])
        return jsonify({"changed": True, "totp_setup_required": totp_needed})
