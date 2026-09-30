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
            ft = automation.get_forecast_type(store, request.args.get("type"))
            if request.args.get("type") and not ft:
                return jsonify({"error": "Unknown forecast type"}), 422
            return jsonify(automation.forecast_for_period(
                store, security, request.args.get("period"),
                request.mf_user, ft))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422

    # ------------------------------------------------------------ forecast types
    @app.get("/api/admin/forecast-types")
    @require_auth
    def list_forecast_types():
        return jsonify(automation.list_forecast_types(store))

    @app.post("/api/admin/forecast-types")
    @require_auth
    @require_admin
    def create_forecast_type():
        body = request.json or {}
        err = automation.validate_forecast_type(store, registry, body)
        if err:
            return jsonify({"error": err}), 422
        rec = {"name": body["name"].strip(), "object": body["object"],
               "amount_field": body["amount_field"],
               "date_field": body["date_field"],
               "category_field": body["category_field"],
               "won_values": body["won_values"],
               "lost_values": body["lost_values"],
               "probability_field": body.get("probability_field")}
        rid = store.config_put("mf_forecast_types", rec)
        _audit("create", "forecast-type", rec["name"])
        return jsonify(store.config_get("mf_forecast_types", rid)), 201

    @app.patch("/api/admin/forecast-types/<rid>")
    @require_auth
    @require_admin
    def update_forecast_type(rid):
        old = store.config_get("mf_forecast_types", rid)
        if not old:
            return jsonify({"error": "Not found"}), 404
        body = request.json or {}
        body.pop("id", None)
        merged = {**old, **body}
        err = automation.validate_forecast_type(store, registry, merged, rid)
        if err:
            return jsonify({"error": err}), 422
        store.config_put("mf_forecast_types", merged)
        _audit("update", "forecast-type", merged.get("name") or rid)
        return jsonify(store.config_get("mf_forecast_types", rid))

    @app.delete("/api/admin/forecast-types/<rid>")
    @require_auth
    @require_admin
    def delete_forecast_type(rid):
        ok = store.config_delete("mf_forecast_types", rid)
        return jsonify({"deleted": ok})

    @app.get("/api/admin/forecast-quotas")
    @require_auth
    @require_admin
    def list_forecast_quotas():
        type_id = request.args.get("type") or "builtin"
        return jsonify([q for q in store.config_all("mf_forecast_quotas")
                        if (q.get("forecast_type_id") or "builtin") == type_id])

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
        type_id = body.get("forecast_type_id") or "builtin"
        if type_id != "builtin" and not store.config_get("mf_forecast_types", type_id):
            return jsonify({"error": "Unknown forecast type"}), 422
        existing = next((q for q in store.config_all("mf_forecast_quotas")
                         if q.get("user_id") == body["user_id"]
                         and q.get("period") == body["period"]
                         and (q.get("forecast_type_id") or "builtin") == type_id), None)
        rec = dict(existing or {})
        rec.update({"user_id": body["user_id"], "period": body["period"],
                    "quota": quota, "forecast_type_id": type_id})
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
