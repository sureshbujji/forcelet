"""CSV import/export. — Forcelet REST API domain module.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime, timezone
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
        # Optional list-view filters (?view=) and text search (?search=),
        # mirroring GET /api/sobjects/<obj>.
        view = None
        view_id = request.args.get("view")
        if view_id:
            view = store.config_get("mf_list_views", view_id)
            if not view or view.get("object") != obj_name or not (
                    view.get("shared") or view.get("owner") == user["id"]
                    or security.is_admin(user)):
                view = None
        filt = (view or {}).get("filters") or None
        rows = []
        for r in records:
            if filt:
                try:
                    if not eval_expr(filt, record_context(r), user=user):
                        continue
                except Exception:
                    continue
            rows.append(r)
        q = (request.args.get("search") or "").strip().lower()
        if q:
            text_fields = {f["name"] for f in obj.get("fields", [])
                           if f["type"] in ("Text", "TextArea", "Email", "Phone", "URL")}
            rows = [r for r in rows
                    if any(q in str(r.get(fn) or "").lower() for fn in text_fields)]
        readable = [f for f in obj.get("fields", [])
                    if security.can(user, "read", obj_name, f["name"])]
        wanted = request.args.get("fields")
        if wanted:
            want = {w.strip() for w in wanted.split(",") if w.strip()}
            fields = [f for f in readable if f["name"] in want]
        else:
            fields = readable
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Id", "RecordType"] + [f["name"] for f in fields])
        for r in rows:
            s = serialize(user, obj, r)
            w.writerow([s["Id"], s["RecordType"]] + [s.get(f["name"], "") for f in fields])
        filename = secure_filename(request.args.get("filename") or f"{obj_name}.csv") \
            or f"{obj_name}.csv"
        if not filename.lower().endswith(".csv"):
            filename += ".csv"
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={filename}"})

    @app.post("/api/admin/import/<obj_name>")
    @require_auth
    @require_admin
    def import_csv(obj_name):
        """Bulk CSV import.

        Query params:
        - mode: "insert" (default) or "upsert" (match by external_id_field)
        - external_id_field: required for upsert mode
        - on_duplicate: "insert" (default, current behavior), "skip",
          "update" (apply the row to the first matching record) or "report"
          (validate only, list duplicates without writing)

        Every run is persisted to mf_import_runs with per-row errors; failed
        rows can be downloaded as CSV and retried.
        """
        obj = registry.get_object(obj_name)
        if not obj:
            return jsonify({"error": "Unknown object"}), 404
        file = request.files.get("file")
        if not file:
            return jsonify({"error": "Upload a CSV file as 'file'"}), 422
        reader = csv.DictReader(io.StringIO(file.read().decode("utf-8-sig")))
        headers = [h for h in (reader.fieldnames or []) if h != "Id"]
        mode = (request.args.get("mode") or "insert").lower()
        ext_field = request.args.get("external_id_field") or ""
        on_duplicate = (request.args.get("on_duplicate") or "insert").lower()
        if on_duplicate not in ("insert", "skip", "update", "report"):
            return jsonify({"error": "on_duplicate must be one of: insert, "
                                     "skip, update, report"}), 422
        if mode == "upsert":
            ext_def = next((f for f in obj.get("fields", [])
                            if f["name"] == ext_field and f.get("external_id")), None)
            if not ext_def:
                return jsonify({"error": f"'{ext_field}' is not an external ID field"
                                         f" on {obj_name}"}), 422

        def process_row(row):
            """Process one CSV row. Returns (outcome, info dict)."""
            row = {k: v for k, v in (row or {}).items() if k not in ("Id",)}
            if mode == "upsert":
                key = (row.get(ext_field) or "").strip()
                if not key:
                    return "failed", {"details":
                                      [f"Missing external ID '{ext_field}'"]}
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
                    return "created", {}
                if status == 200:
                    return "updated", {}
                return "failed", {"details": [payload.get("error")]}
            # insert mode
            clean, errs = registry.validate_record(obj, row)
            vr = automation.check_validation_rules(store, obj_name, clean)
            if errs or vr:
                return "failed", {"details": errs + vr}
            dups = automation.check_duplicates(store, obj_name, clean)
            if dups and on_duplicate != "insert":
                if on_duplicate == "skip":
                    return "skipped", {"duplicates": dups}
                if on_duplicate == "report":
                    return "failed", {"details": ["Possible duplicates found"],
                                      "duplicates": dups}
                # update: apply the row onto the first matching record
                status, payload = _do_update(
                    request.mf_user, obj_name, dups[0]["record_id"], row,
                    allow_duplicates=True)
                if status == 200:
                    return "updated", {}
                return "failed", {"details": [payload.get("error")]}
            # insert mode: run the full create pipeline (_do_create) so that
            # validation, duplicate rules, assignment rules, AutoNumber
            # assignment, triggers, flows, roll-ups, webhooks, approvals,
            # emails and divisions all fire — just like an interactive create.
            status, payload = _do_create(request.mf_user, obj_name, row,
                                         allow_duplicates=(on_duplicate == "insert"))
            if status == 201:
                return "created", {}
            details = payload.get("details") or [payload.get("error")]
            return "failed", {"details": details}

        created, updated, skipped, failed = 0, 0, 0, 0
        errors, error_rows = [], []
        for i, row in enumerate(reader, start=2):
            raw = {k: v for k, v in row.items() if k not in ("Id",)}
            outcome, info = process_row(row)
            if outcome == "created":
                created += 1
            elif outcome == "updated":
                updated += 1
            elif outcome == "skipped":
                skipped += 1
            else:
                failed += 1
                details = info.get("details") or ["Unknown error"]
                errors.append({"row": i, "details": details})
                if len(error_rows) < 500:
                    error_rows.append({"row": i, "details": details,
                                       "raw": raw})
        run_id = store.config_put("mf_import_runs", {
            "object": obj_name, "mode": mode, "on_duplicate": on_duplicate,
            "external_id_field": ext_field,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by": request.mf_user["id"],
            "headers": headers,
            "created": created, "updated": updated,
            "skipped": skipped, "failed": failed,
            "error_rows": error_rows, "retries": [],
        })
        _audit("import", obj_name,
               f"{created} created, {updated} updated, {skipped} skipped, "
               f"{failed} failed")
        return jsonify({"created": created, "updated": updated,
                        "skipped": skipped, "failed": failed,
                        "errors": errors[:20], "run_id": run_id,
                        "error_csv_url":
                            f"/api/admin/import-runs/{run_id}/errors.csv"})

    def _run_summary(run):
        return {k: run.get(k) for k in (
            "id", "object", "mode", "on_duplicate", "external_id_field",
            "created_at", "created_by", "headers",
            "created", "updated", "skipped", "failed", "retries")}

    @app.get("/api/admin/import-runs")
    @require_auth
    @require_admin
    def import_run_list():
        runs = sorted(store.config_all("mf_import_runs"),
                      key=lambda r: r.get("created_at") or "", reverse=True)
        return jsonify([_run_summary(r) for r in runs[:50]])

    @app.get("/api/admin/import-runs/<rid>")
    @require_auth
    @require_admin
    def import_run_get(rid):
        run = store.config_get("mf_import_runs", rid)
        if not run:
            return jsonify({"error": "Unknown import run"}), 404
        out = _run_summary(run)
        out["error_rows"] = [{k: e[k] for k in ("row", "details") if k in e}
                             for e in run.get("error_rows", [])]
        return jsonify(out)

    @app.get("/api/admin/import-runs/<rid>/errors.csv")
    @require_auth
    @require_admin
    def import_run_errors_csv(rid):
        run = store.config_get("mf_import_runs", rid)
        if not run:
            return jsonify({"error": "Unknown import run"}), 404
        headers = run.get("headers") or []
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["csv_row", "error"] + headers)
        for e in run.get("error_rows", []):
            raw = e.get("raw") or {}
            w.writerow([e.get("row"), "; ".join(e.get("details") or [])]
                       + [raw.get(h, "") for h in headers])
        name = secure_filename(f"import-errors-{run.get('object', '')}-{rid[:8]}.csv")
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition":
                                 f"attachment; filename={name}"})

    @app.post("/api/admin/import-runs/<rid>/retry")
    @require_auth
    @require_admin
    def import_run_retry(rid):
        """Retry the failed rows of an import run with the same options."""
        run = store.config_get("mf_import_runs", rid)
        if not run:
            return jsonify({"error": "Unknown import run"}), 404
        obj_name = run.get("object")
        obj = registry.get_object(obj_name)
        if not obj:
            return jsonify({"error": f"Unknown object '{obj_name}'"}), 422
        mode = run.get("mode") or "insert"
        ext_field = run.get("external_id_field") or ""
        on_duplicate = run.get("on_duplicate") or "insert"

        def retry_row(row):
            row = {k: v for k, v in (row or {}).items() if k not in ("Id",)}
            if mode == "upsert":
                key = (row.get(ext_field) or "").strip()
                if not key:
                    return "failed", [f"Missing external ID '{ext_field}'"]
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
                if status in (200, 201):
                    return ("updated" if status == 200 else "created"), []
                return "failed", [payload.get("error")]
            clean, errs = registry.validate_record(obj, row)
            vr = automation.check_validation_rules(store, obj_name, clean)
            if errs or vr:
                return "failed", errs + vr
            dups = automation.check_duplicates(store, obj_name, clean)
            if dups and on_duplicate != "insert":
                if on_duplicate == "skip":
                    return "skipped", []
                if on_duplicate == "report":
                    return "failed", ["Possible duplicates found"]
                status, payload = _do_update(
                    request.mf_user, obj_name, dups[0]["record_id"], row,
                    allow_duplicates=True)
                if status == 200:
                    return "updated", []
                return "failed", [payload.get("error")]
            # insert mode: run the full create pipeline (_do_create), same as
            # the initial import path above.
            status, payload = _do_create(request.mf_user, obj_name, row,
                                         allow_duplicates=(on_duplicate == "insert"))
            if status == 201:
                return "created", []
            return "failed", payload.get("details") or [payload.get("error")]

        created, updated, skipped, failed = 0, 0, 0, 0
        still_failing = []
        for e in run.get("error_rows", []):
            outcome, details = retry_row(e.get("raw"))
            if outcome == "created":
                created += 1
            elif outcome == "updated":
                updated += 1
            elif outcome == "skipped":
                skipped += 1
            else:
                failed += 1
                still_failing.append({"row": e.get("row"), "details": details,
                                      "raw": e.get("raw")})
        attempt = {"at": datetime.now(timezone.utc).isoformat(),
                   "created": created, "updated": updated,
                   "skipped": skipped, "failed": failed}
        run["retries"] = (run.get("retries") or []) + [attempt]
        run["error_rows"] = still_failing[:500]
        run["created"] = run.get("created", 0) + created
        run["updated"] = run.get("updated", 0) + updated
        run["skipped"] = run.get("skipped", 0) + skipped
        run["failed"] = failed
        store.config_put("mf_import_runs", run)
        _audit("import_retry", obj_name,
               f"run {rid[:8]}: {created} created, {updated} updated, "
               f"{failed} still failing")
        return jsonify(attempt)
