"""CSV import/export. — Forcelet REST API domain module.

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
    # ------------------------------------------------------------ import/export
    @app.get("/api/sobjects/<obj_name>/export")
    @require_auth
    def export_csv(obj_name):
        user = request.mf_user
        records, obj = _visible_records(user, obj_name)
        if not obj or not security.can(user, "read", obj_name):
            return jsonify({"error": "Unknown object or no access"}), 404
        fields = [f for f in obj.get("fields", []) if security.can(user, "read", obj_name, f["name"])]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Id", "RecordType"] + [f["name"] for f in fields])
        for r in records:
            s = serialize(user, obj, r)
            w.writerow([s["Id"], s["RecordType"]] + [s.get(f["name"], "") for f in fields])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={obj_name}.csv"})

    @app.post("/api/admin/import/<obj_name>")
    @require_auth
    @require_admin
    def import_csv(obj_name):
        obj = registry.get_object(obj_name)
        if not obj:
            return jsonify({"error": "Unknown object"}), 404
        file = request.files.get("file")
        if not file:
            return jsonify({"error": "Upload a CSV file as 'file'"}), 422
        reader = csv.DictReader(io.StringIO(file.read().decode("utf-8-sig")))
        mode = (request.args.get("mode") or "insert").lower()
        ext_field = request.args.get("external_id_field") or ""
        if mode == "upsert":
            ext_def = next((f for f in obj.get("fields", [])
                            if f["name"] == ext_field and f.get("external_id")), None)
            if not ext_def:
                return jsonify({"error": f"'{ext_field}' is not an external ID field"
                                         f" on {obj_name}"}), 422
        created, updated, failed, errors = 0, 0, 0, []
        for i, row in enumerate(reader, start=2):
            row = {k: v for k, v in row.items() if k not in ("Id",)}
            if mode == "upsert":
                key = (row.get(ext_field) or "").strip()
                if not key:
                    failed += 1
                    errors.append({"row": i, "details":
                                   [f"Missing external ID '{ext_field}'"]})
                    continue
                match = next((r for r in store.query(obj_name, owner_ids=None, limit=10000)
                              if str(r.get(ext_field) or "") == key), None)
                row.pop(ext_field, None)
                if match:
                    status, payload = _do_update(request.mf_user, obj_name,
                                                 match["id"], row,
                                                 allow_duplicates=True)
                else:
                    row[ext_field] = key
                    status, payload = _do_create(request.mf_user, obj_name, row,
                                                 allow_duplicates=True)
                if status == 201:
                    created += 1
                elif status == 200:
                    updated += 1
                else:
                    failed += 1
                    errors.append({"row": i, "details": [payload.get("error")]})
                continue
            clean, errs = registry.validate_record(obj, {k: v for k, v in row.items() if k not in ("Id",)})
            vr = automation.check_validation_rules(store, obj_name, clean)
            if errs or vr:
                failed += 1
                errors.append({"row": i, "details": errs + vr})
                continue
            clean["owner_id"] = request.mf_user["id"]
            clean["created_by"] = request.mf_user["id"]
            clean.setdefault("record_type", automation.default_record_type(store, obj_name))
            store.insert(obj_name, clean)
            created += 1
        return jsonify({"created": created, "updated": updated,
                        "failed": failed, "errors": errors[:20]})
