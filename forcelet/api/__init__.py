"""Forcelet REST API package.

Domain modules (auth, records, chatter, ...) each expose ``register(app)``
and are wired up by :func:`create_app`.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

from flask import Flask

from ..bootstrap import bootstrap

from . import (
    admin, api_keys, approvals, audit, auth, callouts, cdc, chatter,
    email, files, flows, forecasts, impexp, jobs, kanban, metadata,
    notifications, oauth, packaging, records, reports, scoring, search,
    sla, activities, web_to,
)


def create_app(db_path: str) -> Flask:
    """Create the Forcelet Flask app, backed by the SQLite DB at db_path."""
    store, registry, security = bootstrap(db_path)
    app = Flask(__name__)
    app.mf_store, app.mf_registry, app.mf_security = store, registry, security
    for mod in (admin, api_keys, approvals, audit, auth, callouts, cdc,
                chatter, email, files, flows, forecasts, impexp, jobs,
                kanban, metadata, notifications, oauth, packaging, records,
                reports, scoring, search, sla, activities, web_to):
        mod.register(app)
    return app
