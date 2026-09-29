"""API keys. — Forcelet REST API domain module.

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
    # ------------------------------------------------------------ api keys
    @app.get("/api/api-keys")
    @require_auth
    def list_api_keys():
        return jsonify(store.api_keys_for(request.mf_user["id"]))

    @app.post("/api/api-keys")
    @require_auth
    def create_api_key():
        import hashlib
        import secrets
        body = request.json or {}
        raw = "mf_live_" + secrets.token_urlsafe(32)
        kid = store.put_api_key(hashlib.sha256(raw.encode()).hexdigest(),
                                body.get("name") or "api key",
                                request.mf_user["id"])
        return jsonify({"id": kid, "name": body.get("name") or "api key",
                        "key": raw,
                        "warning": "Copy this key now - it is never shown again."}), 201

    @app.delete("/api/api-keys/<kid>")
    @require_auth
    def revoke_api_key(kid):
        return jsonify({"deleted": store.delete_api_key(kid, request.mf_user["id"])})
