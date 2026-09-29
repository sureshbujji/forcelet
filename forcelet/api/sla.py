"""Case SLA milestones. — Forcelet REST API domain module.

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
    # ------------------------------------------------------------ case milestones
    @app.get("/api/sobjects/<obj_name>/<rid>/milestones")
    @require_auth
    def record_milestones(obj_name, rid):
        obj = registry.get_object(obj_name)
        rec = obj and store.get(obj_name, rid)
        if not obj or not rec or not security.can_see_record(
                request.mf_user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        return jsonify(automation.case_milestones(store, rid))
