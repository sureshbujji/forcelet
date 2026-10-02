"""Screen flows. — Forcelet REST API domain module.

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
    # ------------------------------------------------------------ screen flows
    @app.get("/api/invocable-actions")
    @require_auth
    def list_invocable_actions_ep():
        """Code-registered actions usable via {type: invocable, name, inputs}."""
        return jsonify(automation.list_invocable_actions())

    def _screen_flow_or_404(fid):
        f = store.config_get("mf_flows", fid)
        if not f or f.get("flow_type") != "screen" \
                or not f.get("active", True):
            return None
        return f

    @app.get("/api/screen-flows")
    @require_auth
    def list_screen_flows():
        flows = [f for f in store.config_all("mf_flows")
                 if f.get("flow_type") == "screen" and f.get("active", True)]
        return jsonify([{"id": f["id"], "name": f.get("name"),
                         "screens": len(f.get("screens") or [])}
                        for f in flows])

    @app.post("/api/screen-flows/<fid>/start")
    @require_auth
    def start_screen_flow_ep(fid):
        flow = _screen_flow_or_404(fid)
        if not flow:
            return jsonify({"error": "Unknown screen flow"}), 404
        try:
            run = automation.start_screen_flow(store, flow, request.mf_user)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        screens = flow.get("screens") or []
        return jsonify({"run_id": run["id"],
                        "screen": automation._public_screen(screens[0]),
                        "total_screens": len(screens)}), 201

    @app.get("/api/screen-flows/runs/<rid>")
    @require_auth
    def get_screen_flow_run(rid):
        run = store.config_get("mf_flow_runs", rid)
        if not run or (run.get("user_id") != request.mf_user["id"]
                       and not security.is_admin(request.mf_user)):
            return jsonify({"error": "Not found"}), 404
        flow = store.config_get("mf_flows", run.get("flow_id") or "")
        screens = (flow.get("screens") or []) if flow else []
        idx = int(run.get("current") or 0)
        screen = (automation._public_screen(screens[idx])
                  if run.get("status") == "in_progress" and idx < len(screens)
                  else None)
        return jsonify({"run": run, "screen": screen})

    @app.post("/api/screen-flows/runs/<rid>/next")
    @require_auth
    def advance_screen_flow_ep(rid):
        run = store.config_get("mf_flow_runs", rid)
        if not run or (run.get("user_id") != request.mf_user["id"]
                       and not security.is_admin(request.mf_user)):
            return jsonify({"error": "Not found"}), 404
        if run.get("status") != "in_progress":
            return jsonify({"error": "Flow run is already complete"}), 422
        flow = store.config_get("mf_flows", run.get("flow_id") or "")
        if not flow:
            return jsonify({"error": "Unknown flow"}), 404
        run, screen, result = automation.advance_screen_flow(
            store, registry, security, flow, run,
            (request.json or {}).get("values") or {}, request.mf_user)
        if not result.get("ok") and result.get("errors"):
            return jsonify({"screen": screen,
                            "errors": result["errors"]}), 422
        return jsonify({"run_id": run["id"], "status": run["status"],
                        "screen": screen, "result": result})
