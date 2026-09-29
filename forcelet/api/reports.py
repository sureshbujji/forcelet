"""Reports. — Forcelet REST API domain module.

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
    # ------------------------------------------------------------ reports
    @app.get("/api/reports")
    @require_auth
    def list_reports():
        return jsonify(store.config_all("mf_reports"))

    @app.post("/api/admin/reports")
    @require_auth
    @require_admin
    def create_report():
        rid = store.config_put("mf_reports", request.json or {})
        _audit("create", "reports", (request.json or {}).get("name") or rid)
        return jsonify(store.config_get("mf_reports", rid)), 201

    @app.get("/api/reports/<rep_id>/run")
    @require_auth
    def run_report(rep_id):
        user = request.mf_user
        rep = store.config_get("mf_reports", rep_id)
        if not rep:
            return jsonify({"error": "Unknown report"}), 404
        obj_name = rep["object"]
        obj = registry.get_object(obj_name)
        if not obj or not security.can(user, "read", obj_name):
            return jsonify({"error": "No access"}), 404
        records, _ = _visible_records(user, obj_name)
        filt = rep.get("filters") or {}
        rows = []
        for r in records:
            try:
                if filt and not eval_expr(filt, record_context(r)):
                    continue
            except Exception:
                continue
            rows.append(serialize(user, obj, r))
        group_by, agg = rep.get("group_by"), rep.get("aggregate") or {}
        groups = None
        if group_by:
            groups = {}
            for row in rows:
                key = row.get(group_by) or "(blank)"
                g = groups.setdefault(key, {"key": key, "count": 0, "aggregate": None, "_sum": 0.0, "_n": 0})
                g["count"] += 1
                if agg.get("func") in ("sum", "avg") and isinstance(row.get(agg.get("field")), (int, float)):
                    g["_sum"] += row[agg["field"]]
                    g["_n"] += 1
            for g in groups.values():
                if agg.get("func") == "sum":
                    g["aggregate"] = g["_sum"]
                elif agg.get("func") == "avg":
                    g["aggregate"] = g["_sum"] / g["_n"] if g["_n"] else None
                del g["_sum"]
                del g["_n"]
            groups = sorted(groups.values(), key=lambda g: g["count"], reverse=True)
        return jsonify({"report": rep["name"], "row_count": len(rows),
                        "columns": rep.get("columns") or [], "rows": rows[:500], "groups": groups})

    # ------------------------------------------------------------ dashboards
    @app.get("/api/dashboards")
    @require_auth
    def list_dashboards():
        return jsonify(store.config_all("mf_dashboards"))

    @app.post("/api/dashboards")
    @require_auth
    @require_admin
    def create_dashboard():
        body = request.json or {}
        rid = store.config_put("mf_dashboards", {
            "name": body.get("name") or "Dashboard",
            "widgets": body.get("widgets") or [],
        })
        _audit("create", "dashboards", body.get("name") or rid)
        return jsonify(store.config_get("mf_dashboards", rid)), 201

    @app.put("/api/dashboards/<did>")
    @require_auth
    @require_admin
    def update_dashboard(did):
        dash = store.config_get("mf_dashboards", did)
        if not dash:
            return jsonify({"error": "Unknown dashboard"}), 404
        body = request.json or {}
        dash["name"] = body.get("name", dash.get("name"))
        dash["widgets"] = body.get("widgets", dash.get("widgets") or [])
        store.config_put("mf_dashboards", dash)
        _audit("update", "dashboards", dash["name"])
        return jsonify(dash)

    @app.delete("/api/dashboards/<did>")
    @require_auth
    @require_admin
    def delete_dashboard(did):
        if not store.config_delete("mf_dashboards", did):
            return jsonify({"error": "Unknown dashboard"}), 404
        _audit("delete", "dashboards", did)
        return jsonify({"ok": True})
