"""Email templates and sending. — Forcelet REST API domain module.

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
    # ------------------------------------------------------------ email
    @app.post("/api/sobjects/<obj_name>/<rid>/send-email")
    @require_auth
    def send_email(obj_name, rid):
        user = request.mf_user
        obj = registry.get_object(obj_name)
        rec = obj and store.get(obj_name, rid)
        if not obj or not rec or not security.can(user, "read", obj_name) \
                or not security.can_see_record(user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        body = request.json or {}
        tpl = store.config_get("mf_email_templates", body.get("template_id")) \
            if body.get("template_id") else None
        merged_tpl = {"subject": body.get("subject") or (tpl or {}).get("subject") or "",
                      "body": body.get("body") or (tpl or {}).get("body") or "",
                      "name": (tpl or {}).get("name", "")}
        recipient, subject = automation.send_templated_email(
            store, obj_name, rec, merged_tpl, user, to_addr=body.get("to") or "")
        # Demo delivery: the email is logged and added to the activity
        # timeline. Point FORCELET_SMTP at a real relay to actually send.
        return jsonify({"sent": True, "to": recipient, "subject": subject,
                        "delivered": bool(os.environ.get("FORCELET_SMTP"))})

    @app.get("/api/email-templates")
    @require_auth
    def email_templates():
        return jsonify(store.config_all("mf_email_templates"))
