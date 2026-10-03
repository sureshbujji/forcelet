"""Approval processes: inbox and decisions. — Forcelet REST API domain module.

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
    # ------------------------------------------------------------ approvals
    @app.get("/api/approvals")
    @require_auth
    def approval_inbox():
        user = request.mf_user
        reqs = automation.pending_for_user(store, security, user)
        users = {u["id"]: u["name"] for u in security.list_users()}
        out = []
        for r in reqs:
            rec = store.get(r["object"], r["record_id"])
            out.append({**r, "submitted_by_name": users.get(r["submitted_by"], "?"),
                        "record_label": (rec or {}).get("Name") or r["record_id"]})
        return jsonify(out)

    @app.post("/api/sobjects/<obj_name>/<rid>/submit-approval")
    @require_auth
    def submit_approval(obj_name, rid):
        user = request.mf_user
        obj = registry.get_object(obj_name)
        rec = obj and store.get(obj_name, rid)
        if not obj or not rec or not security.can_see_record(user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        req, err = automation.submit_for_approval(
            store, security, obj_name, rec, user,
            (request.get_json(silent=True) or {}).get("comment", ""))
        if err:
            return jsonify({"error": err}), 422
        return jsonify(req), 201

    @app.post("/api/approvals/<req_id>/approve")
    @require_auth
    def approve(req_id):
        req, err = automation.decide_request(store, security, req_id, request.mf_user, True,
                                             (request.json or {}).get("comment", ""))
        return (jsonify(req), 200) if req else (jsonify({"error": err}), 422)

    @app.post("/api/approvals/<req_id>/reject")
    @require_auth
    def reject(req_id):
        req, err = automation.decide_request(store, security, req_id, request.mf_user, False,
                                             (request.json or {}).get("comment", ""))
        return (jsonify(req), 200) if req else (jsonify({"error": err}), 422)
