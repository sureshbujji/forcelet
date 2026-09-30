"""Admin configuration endpoints. — Forcelet REST API domain module.

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
    current_user, rate_limit, require_admin, require_auth, serialize, ctx,
)


def _backup_dir() -> str:
    return os.environ.get("FORCELET_BACKUP_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backups")


def register(app: Flask):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security
    # ------------------------------------------------------------ admin: config
    CONFIG_TABLES = {
        "validation-rules": "mf_validation_rules",
        "flows": "mf_flows",
        "approval-processes": "mf_approval_processes",
        "sharing-rules": "mf_sharing_rules",
        "matching-rules": "mf_matching_rules",
        "record-types": "mf_record_types",
        "permission-sets": "mf_permission_sets",
        "webhooks": "mf_webhooks",
        "triggers": "mf_triggers",
        "scheduled-jobs": "mf_scheduled_jobs",
        "assignment-rules": "mf_assignment_rules",
        "email-templates": "mf_email_templates",
        "auto-response-rules": "mf_auto_responses",
        "sla-policies": "mf_sla_policies",
        "escalation-rules": "mf_escalation_rules",
    }

    @app.get("/api/admin/<kind>")
    @require_auth
    @require_admin
    def admin_list_config(kind):
        table = CONFIG_TABLES.get(kind)
        if not table:
            return jsonify({"error": "Unknown config type"}), 404
        return jsonify(store.config_all(table))

    # ------------------------------------------------------------ backups
    @app.get("/api/admin/backups")
    @require_auth
    @require_admin
    def backup_list():
        bdir = _backup_dir()
        files = []
        if os.path.isdir(bdir):
            for name in sorted(os.listdir(bdir), reverse=True):
                if name.startswith("forcelet-") and name.endswith(".db"):
                    p = os.path.join(bdir, name)
                    files.append({"name": name,
                                  "size_bytes": os.path.getsize(p),
                                  "created": name[len("forcelet-"):-len(".db")]})
        return jsonify(files)

    @app.post("/api/admin/backups")
    @require_auth
    @require_admin
    @rate_limit(max_requests=6, window_seconds=3600)
    def backup_create():
        bdir = _backup_dir()
        os.makedirs(bdir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(bdir, f"forcelet-{stamp}.db")
        # VACUUM INTO writes a consistent snapshot while the DB stays online.
        store._execute("VACUUM INTO ?", (dest,))
        # Retention: keep the newest N backups.
        keep = int(os.environ.get("FORCELET_BACKUP_KEEP", "14"))
        existing = sorted(f for f in os.listdir(bdir)
                          if f.startswith("forcelet-") and f.endswith(".db"))
        for old in existing[:-keep]:
            os.remove(os.path.join(bdir, old))
        _audit("backup_create", "Database", os.path.basename(dest))
        return jsonify({"backup": os.path.basename(dest)}), 201

    @app.post("/api/admin/<kind>")
    @require_auth
    @require_admin
    def admin_create_config(kind):
        table = CONFIG_TABLES.get(kind)
        if not table:
            return jsonify({"error": "Unknown config type"}), 404
        body = request.json or {}
        if kind == "record-types" and body.get("object") and not registry.get_object(body["object"]):
            return jsonify({"error": "Unknown object"}), 422
        if kind == "triggers":
            if not registry.get_object(body.get("object") or ""):
                return jsonify({"error": "Unknown object"}), 422
            bad = [e for e in (body.get("events") or []) if e not in automation.TRIGGER_EVENTS]
            if bad:
                return jsonify({"error": "Unknown trigger events", "details": bad}), 422
            try:
                compile(body.get("code") or "", "<trigger>", "exec")
            except SyntaxError as e:
                return jsonify({"error": "Trigger code has a syntax error", "details": str(e)}), 422
        if kind == "assignment-rules":
            if not registry.get_object(body.get("object") or ""):
                return jsonify({"error": "Unknown object"}), 422
            a = body.get("assignee") or {}
            if a.get("type") not in ("user", "round_robin"):
                return jsonify({"error": "assignee.type must be 'user' or 'round_robin'"}), 422
        if kind == "scheduled-jobs":
            try:
                compile(body.get("code") or "", "<scheduled>", "exec")
            except SyntaxError as e:
                return jsonify({"error": "Job code has a syntax error", "details": str(e)}), 422
            cron_expr = (body.get("cron") or "").strip()
            if cron_expr:
                from .. import cron as _cron
                try:
                    _cron.parse(cron_expr)
                except ValueError as e:
                    return jsonify({"error": f"Bad cron expression: {e}"}), 422
            elif int(body.get("interval_minutes") or 0) <= 0:
                return jsonify({"error": "interval_minutes must be positive"}), 422
        rid = store.config_put(table, body)
        _audit("create", kind, body.get("name") or rid)
        return jsonify(store.config_get(table, rid)), 201

    @app.patch("/api/admin/<kind>/<rid>")
    @require_auth
    @require_admin
    def admin_update_config(kind, rid):
        """Partial update of a config record (edit, activate/deactivate)."""
        table = CONFIG_TABLES.get(kind)
        if not table:
            return jsonify({"error": "Unknown config type"}), 404
        old = store.config_get(table, rid)
        if not old:
            return jsonify({"error": "Not found"}), 404
        body = request.json or {}
        body.pop("id", None)
        if kind == "scheduled-jobs" and (body.get("cron") or "").strip():
            from .. import cron as _cron
            try:
                _cron.parse(body["cron"].strip())
            except ValueError as e:
                return jsonify({"error": f"Bad cron expression: {e}"}), 422
        if kind == "flows":
            # snapshot the pre-change definition for version history
            from .enhancements import _snapshot_flow_version
            _snapshot_flow_version(store, rid, old, request.mf_user)
        merged = {**old, **body}
        store.config_put(table, {**merged, "id": rid})
        _audit("update", kind, merged.get("name") or rid)
        return jsonify(store.config_get(table, rid))

    @app.delete("/api/admin/<kind>/<rid>")
    @require_auth
    @require_admin
    def admin_delete_config(kind, rid):
        table = CONFIG_TABLES.get(kind)
        if not table:
            return jsonify({"error": "Unknown config type"}), 404
        old = store.config_get(table, rid)
        ok = store.config_delete(table, rid)
        if ok:
            _audit("delete", kind, (old or {}).get("name") or rid)
        return jsonify({"deleted": ok})

    @app.post("/api/admin/objects")
    @require_auth
    @require_admin
    def admin_create_object():
        body = request.json or {}
        try:
            obj = registry.create_object(body.get("name", ""), body.get("label", ""),
                                         body.get("plural", ""),
                                         big_object=bool(body.get("big_object")))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("create", "object", obj["name"])
        return jsonify(obj), 201

    @app.post("/api/admin/objects/<obj_name>/fields")
    @require_auth
    @require_admin
    def admin_add_field(obj_name):
        try:
            field = registry.add_field(obj_name, request.json or {})
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("create", "field", f"{obj_name}.{field['name']}")
        return jsonify(field), 201

    @app.get("/api/admin/users")
    @require_auth
    @require_admin
    def admin_list_users():
        return jsonify([{k: v for k, v in u.items() if k != "password_hash"}
                        for u in security.list_users()])

    @app.post("/api/admin/users")
    @require_auth
    @require_admin
    def admin_create_user():
        body = request.json or {}
        try:
            user = security.create_user(body.get("username", ""), body.get("name", ""),
                                        body.get("profile", ""), body.get("role"),
                                        body.get("password", "forcelet"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        user = {k: v for k, v in user.items() if k != "password_hash"}
        _audit("create", "user", user["username"])
        return jsonify(user), 201

    @app.post("/api/admin/users/<uid>/permission-sets")
    @require_auth
    @require_admin
    def admin_assign_ps(uid, ):
        try:
            user = security.assign_permission_set(uid, (request.json or {}).get("permission_set", ""))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("assign", "permission-set", (request.json or {}).get("permission_set", ""),
               f"user={user['username']}")
        return jsonify({k: v for k, v in user.items() if k != "password_hash"})

    @app.get("/api/admin/roles")
    @require_auth
    @require_admin
    def admin_list_roles():
        return jsonify(security.list_roles())

    @app.post("/api/admin/roles")
    @require_auth
    @require_admin
    def admin_create_role():
        body = request.json or {}
        try:
            role = security.create_role(body.get("name", ""), body.get("parent"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("create", "role", role["name"])
        return jsonify(role), 201

    @app.get("/api/admin/profiles")
    @require_auth
    @require_admin
    def admin_list_profiles():
        return jsonify(security.list_profiles())

    @app.post("/api/admin/profiles")
    @require_auth
    @require_admin
    def admin_create_profile():
        body = request.json or {}
        try:
            profile = security.create_profile(body.get("name", ""),
                                              body.get("object_permissions", {}),
                                              body.get("field_permissions", {}))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("create", "profile", profile["name"])
        return jsonify(profile), 201

    @app.post("/api/admin/layouts")
    @require_auth
    @require_admin
    def admin_save_layout():
        body = request.json or {}
        if not body.get("object") or not registry.get_object(body["object"]):
            return jsonify({"error": "Unknown object"}), 422
        store.layout_put(body["object"], body.get("profile", "Default"),
                         {"sections": body.get("sections", []),
                          "related_lists": body.get("related_lists", [])},
                         body.get("record_type", "Default"))
        _audit("save", "layout", f"{body['object']}/{body.get('profile', 'Default')}")
        return jsonify({"saved": True})

    @app.get("/api/admin/webhook-deliveries")
    @require_auth
    @require_admin
    def admin_webhook_deliveries():
        rows = store._execute(
            "SELECT * FROM mf_webhook_deliveries ORDER BY attempted_at DESC LIMIT 100").fetchall()
        return jsonify([dict(r) for r in rows])

    @app.get("/api/field-types")
    @require_auth
    def field_types():
        return jsonify([{"name": n, "description": v["desc"]} for n, v in FIELD_TYPES.items()])
