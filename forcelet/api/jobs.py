"""Scheduled jobs. — Forcelet REST API domain module.

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
    # ------------------------------------------------------------ scheduled jobs
    @app.post("/api/admin/scheduled-jobs/<jid>/run")
    @require_auth
    @require_admin
    def run_scheduled_job_now(jid):
        job = store.config_get("mf_scheduled_jobs", jid)
        if not job:
            return jsonify({"error": "Unknown job"}), 404
        users = {u["username"]: u for u in security.list_users()}
        run_as = users.get(job.get("run_as") or "admin") or users.get("admin")
        res = automation.run_scheduled_job(store, registry, security, job, run_as)
        from datetime import datetime, timezone
        job["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        store.config_put("mf_scheduled_jobs", job)
        return jsonify(res)

    @app.get("/api/admin/scheduled-runs")
    @require_auth
    @require_admin
    def scheduled_runs():
        return jsonify(store.scheduled_runs(request.args.get("job_id")))

    @app.get("/api/admin/email-log")
    @require_auth
    @require_admin
    def email_log():
        return jsonify(store.email_log(limit=int(request.args.get("limit", 100))))
