"""Global search. — Forcelet REST API domain module.

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
    # ------------------------------------------------------------ global search
    @app.get("/api/search")
    @require_auth
    def global_search():
        user = request.mf_user
        q = (request.args.get("q") or "").strip().lower()
        if not q:
            return jsonify([])
        out = []
        for obj_name in [o["name"] for o in registry.list_objects()]:
            obj = registry.get_object(obj_name)
            if not security.can(user, "read", obj_name):
                continue
            readable = set(security.readable_fields(user, obj))
            text_fields = [f["name"] for f in obj.get("fields", [])
                           if f["name"] in readable
                           and f["type"] in ("Text", "TextArea", "Email", "Phone", "URL")]
            if not text_fields:
                continue
            matches = []
            records, _obj = _visible_records(user, obj_name)
            for rec in records:
                ctx = record_context(rec)  # decrypts encrypted fields for searching
                if any(isinstance(ctx.get(fn), str) and q in ctx[fn].lower()
                       for fn in text_fields):
                    matches.append(serialize(user, obj, rec))
                    if len(matches) >= 5:
                        break
            if matches:
                out.append({"object": obj_name,
                            "label": obj.get("plural") or obj.get("label_plural") or obj_name,
                            "records": matches})
        return jsonify(out)
