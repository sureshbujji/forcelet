"""Named credentials and HTTP callouts. — Forcelet REST API domain module.

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
    # ------------------------------------------------------------ named credentials + callouts
    def _scrub_credential(c):
        c = dict(c)
        secret = c.pop("secret_enc", None)
        c.pop("secret", None)
        c["has_secret"] = bool(secret)
        return c

    @app.get("/api/admin/named-credentials")
    @require_auth
    @require_admin
    def list_named_credentials():
        return jsonify([_scrub_credential(c)
                        for c in store.config_all("mf_named_credentials")])

    @app.post("/api/admin/named-credentials")
    @require_auth
    @require_admin
    def upsert_named_credential():
        body = request.json or {}
        if not body.get("name"):
            return jsonify({"error": "name is required"}), 422
        if not (body.get("url") or "").startswith(("http://", "https://")):
            return jsonify({"error": "url must start with http(s)://"}), 422
        if (body.get("auth_type") or "none") not in ("none", "basic", "bearer", "api_key"):
            return jsonify({"error": "unknown auth_type"}), 422
        existing = next((c for c in store.config_all("mf_named_credentials")
                         if c.get("name") == body["name"]), None)
        rec = dict(existing or {})
        for k in ("name", "url", "auth_type", "username", "api_key_header",
                  "active"):
            if k in body:
                rec[k] = body[k]
        rec.setdefault("auth_type", "none")
        rec.setdefault("active", True)
        if body.get("secret"):
            rec["secret_enc"] = _crypto.encrypt(body["secret"])
        rid = store.config_put("mf_named_credentials", rec)
        _audit("upsert", "named-credentials", body["name"])
        return jsonify(_scrub_credential(
            store.config_get("mf_named_credentials", rid))), 201

    @app.delete("/api/admin/named-credentials/<rid>")
    @require_auth
    @require_admin
    def delete_named_credential(rid):
        old = store.config_get("mf_named_credentials", rid)
        ok = store.config_delete("mf_named_credentials", rid)
        if ok:
            _audit("delete", "named-credentials", (old or {}).get("name") or rid)
        return jsonify({"deleted": ok})

    @app.post("/api/admin/callouts/invoke")
    @require_auth
    @require_admin
    def invoke_callout_ep():
        body = request.json or {}
        res = automation.invoke_callout(
            store, body.get("credential") or "",
            method=body.get("method") or "GET",
            path=body.get("path") or "",
            headers=body.get("headers") or {},
            body=body.get("body"))
        _audit("invoke", "callout", body.get("credential") or "")
        return jsonify(res)
