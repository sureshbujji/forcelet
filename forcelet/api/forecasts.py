"""Forecasting. — Forcelet REST API domain module.

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
    # ------------------------------------------------------------ forecasts
    @app.get("/api/forecasts")
    @require_auth
    def forecasts():
        try:
            return jsonify(automation.forecast_for_period(
                store, security, request.args.get("period"),
                request.mf_user))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422

    @app.get("/api/admin/forecast-quotas")
    @require_auth
    @require_admin
    def list_forecast_quotas():
        return jsonify(store.config_all("mf_forecast_quotas"))

    @app.post("/api/admin/forecast-quotas")
    @require_auth
    @require_admin
    def upsert_forecast_quota():
        body = request.json or {}
        import re
        if not re.fullmatch(r"\d{4}-\d{2}", body.get("period") or ""):
            return jsonify({"error": "period must be YYYY-MM"}), 422
        if not security.get_user(body.get("user_id") or ""):
            return jsonify({"error": "Unknown user"}), 422
        try:
            quota = float(body.get("quota"))
        except (TypeError, ValueError):
            return jsonify({"error": "quota must be a number"}), 422
        existing = next((q for q in store.config_all("mf_forecast_quotas")
                         if q.get("user_id") == body["user_id"]
                         and q.get("period") == body["period"]), None)
        rec = dict(existing or {})
        rec.update({"user_id": body["user_id"], "period": body["period"],
                    "quota": quota})
        rid = store.config_put("mf_forecast_quotas", rec)
        _audit("upsert", "forecast-quota",
               f"{body['user_id']} {body['period']}")
        return jsonify(store.config_get("mf_forecast_quotas", rid)), 201

    @app.delete("/api/admin/forecast-quotas/<rid>")
    @require_auth
    @require_admin
    def delete_forecast_quota(rid):
        ok = store.config_delete("mf_forecast_quotas", rid)
        return jsonify({"deleted": ok})
