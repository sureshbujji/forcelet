"""Forcelet REST API package.

Domain modules (auth, records, chatter, ...) each expose ``register(app)``
and are wired up by :func:`create_app`.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import logging
import os
import time

from flask import Flask, jsonify, request, send_from_directory

from ..bootstrap import bootstrap
from ..migrations import run_migrations

from . import (
    admin, api_keys, approvals, apps, audit, auth, callouts, cdc, chatter,
    datamodel, email, enhancements, experience, field_service, files, flows, forecasts, impexp, jobs, kanban,
    metadata, notifications, oauth, packaging, platform, platform_core, records, reports, scoring,
    search, sla, activities, web_to, devops, dynamic_forms, sales_core, service_core, p2_admin,
    currency, omni,
)

APP_VERSION = "0.7.0"

_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "font-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'self'")


def _configure_logging():
    logging.basicConfig(
        level=os.environ.get("FORCELET_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Werkzeug's request log is noisy; keep warnings/errors only.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def create_app(db_path: str) -> Flask:
    """Create the Forcelet Flask app, backed by the SQLite DB at db_path."""
    _configure_logging()
    log = logging.getLogger("forcelet")
    store, registry, security = bootstrap(db_path)
    schema_v = run_migrations(store)
    app = Flask(__name__)
    app.mf_store, app.mf_registry, app.mf_security = store, registry, security
    # Cap request bodies (uploads) to blunt trivial DoS; override with
    # FORCELET_MAX_UPLOAD_MB. Flask answers 413 when exceeded.
    try:
        max_mb = float(os.environ.get("FORCELET_MAX_UPLOAD_MB", "16"))
    except ValueError:
        max_mb = 16.0
    app.config["MAX_CONTENT_LENGTH"] = int(max_mb * 1024 * 1024)

    # Serve the single-page UI from the app itself so dev (run.py) and
    # production (gunicorn/wsgi.py) behave identically. /r/<object>/<id>
    # deep links serve the same page; the frontend router takes it from there.
    web_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "web")

    @app.get("/")
    def _spa_index():
        return send_from_directory(web_dir, "index.html")

    @app.get("/r/<path:_subpath>")
    def _spa_record(_subpath):
        return send_from_directory(web_dir, "index.html")

    @app.get("/portal")
    def _portal():
        """Standalone customer community portal page."""
        return send_from_directory(web_dir, "portal.html")
    for mod in (admin, api_keys, approvals, apps, audit, auth, callouts, cdc,
                chatter, datamodel, email, enhancements, experience, field_service, files, flows, forecasts,
                impexp, jobs, kanban, metadata, notifications, oauth, packaging,
                platform, platform_core, records, reports, scoring, search, sla, activities,
                web_to, devops, dynamic_forms, sales_core, service_core, p2_admin,
                currency, omni):
        mod.register(app)

    @app.get("/api/health")
    def health():
        """Unauthenticated liveness check for load balancers / monitoring."""
        try:
            store._execute("SELECT 1").fetchone()
            db = "ok"
        except Exception:
            db = "error"
        return jsonify({"ok": db == "ok", "version": APP_VERSION,
                        "schema_version": schema_v, "db": db,
                        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}), \
            (200 if db == "ok" else 503)

    @app.after_request
    def _security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["Content-Security-Policy"] = _CSP
        # Ignored by browsers on plain HTTP; enforced once behind TLS.
        resp.headers["Strict-Transport-Security"] = \
            "max-age=31536000; includeSubDomains"
        return resp

    @app.after_request
    def _log_failures(resp):
        # Structured visibility into client/server errors without logging
        # every successful request.
        if resp.status_code >= 400:
            logging.getLogger("forcelet.http").warning(
                "%s %s -> %s", request.method, request.path, resp.status_code)
        return resp

    log.info("forcelet %s ready (schema v%s)", APP_VERSION, schema_v)
    return app
