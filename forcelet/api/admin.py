"""Admin configuration endpoints. — Forcelet REST API domain module.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, jsonify, request, Response, send_file
from werkzeug.utils import secure_filename

from .. import automation
from .. import crypto as _crypto
from .. import delegated as _delegated
from .. import divisions as _divisions
from ..expressions import eval_expr, record_context
from ..field_types import FIELD_TYPES, validate_value
from ..security import reset_login_attempts_for_user
from ..settings import (CHATTER_KEY, LOGIN_KEY, ORG_KEY, PORTAL_KEY, SECURITY_KEY,
                        check_password_policy, get_settings, save_settings)
from ._shared import (
    _audit, _do_create, _do_update, _visible_records,
    current_user, rate_limit, require_admin, require_admin_scope,
    require_auth, serialize, ctx,
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
        "rollup-rules": "mf_rollup_rules",
        "lead-field-mappings": "mf_lead_field_mappings",
        "web-to-forms": "mf_web_to_forms",
        "divisions": "mf_divisions",
    }

    def _delegated_target_ok(scopes, target_role):
        """403 response unless the caller may act on the target user.

        Full admins always pass; delegated admins pass when one of their
        scope entries covers the target's role (see delegated.target_in_scope).
        """
        user = request.mf_user
        if security.is_admin(user):
            return None
        for scope in scopes:
            if _delegated.target_in_scope(store, user, scope, target_role):
                return None
        return jsonify({"error": "Target is outside your delegated "
                                 "administration scope"}), 403

    def _validate_sla_policy(policy):
        """422 response or None. ``policy`` is the merged policy dict."""
        obj = policy.get("object") or "Case"
        if not registry.get_object(obj):
            return jsonify({"error": f"Unknown object '{obj}'"}), 422
        comp = policy.get("completion")
        if comp is not None:
            if (not isinstance(comp, dict) or not comp
                    or any(not isinstance(k, str) for k in comp)):
                return jsonify({"error": (
                    "completion must be a non-empty {field: value} map")}), 422
            fields = {f["name"] for f in
                      (registry.get_object(obj) or {}).get("fields", [])}
            bad = [k for k in comp if k not in fields]
            if bad:
                return jsonify({"error": (
                    f"completion field(s) not on {obj}"),
                    "details": bad}), 422
        return None

    def _validate_rollup_rule(rule):
        """422 response or None. ``rule`` is the merged rule dict."""
        from ._shared import ROLLUP_FUNCS
        for key in ("child_object", "parent_object", "link_field",
                    "parent_field"):
            if not rule.get(key):
                return jsonify(
                    {"error": f"{key} is required"}), 422
        cdef = registry.get_object(rule["child_object"])
        pdef = registry.get_object(rule["parent_object"])
        if not cdef:
            return jsonify({"error": (
                f"Unknown child object '{rule['child_object']}'")}), 422
        if not pdef:
            return jsonify({"error": (
                f"Unknown parent object '{rule['parent_object']}'")}), 422
        cfields = {f["name"]: f for f in cdef.get("fields", [])}
        pfields = {f["name"]: f for f in pdef.get("fields", [])}
        func = rule.get("func") or "sum"
        if func not in ROLLUP_FUNCS:
            return jsonify({"error": (
                f"func must be one of {list(ROLLUP_FUNCS)}")}), 422
        if not cfields.get(rule["link_field"]):
            return jsonify({"error": (
                f"link_field '{rule['link_field']}' is not on "
                f"{rule['child_object']}")}), 422
        pf = pfields.get(rule["parent_field"])
        if not pf:
            return jsonify({"error": (
                f"parent_field '{rule['parent_field']}' is not on "
                f"{rule['parent_object']}")}), 422
        if pf.get("formula") or pf.get("rollup"):
            return jsonify({"error": (
                "parent_field must not be a formula or roll-up field")}), 422
        if func != "count":
            if not rule.get("child_field"):
                return jsonify(
                    {"error": "child_field is required"}), 422
            if not cfields.get(rule["child_field"]):
                return jsonify({"error": (
                    f"child_field '{rule['child_field']}' is not on "
                    f"{rule['child_object']}")}), 422
        return None

    def _validate_lead_field_mapping(m):
        """422 response or None. ``m`` is the merged mapping dict."""
        for key in ("lead_field", "target_object", "target_field"):
            if not m.get(key):
                return jsonify(
                    {"error": f"{key} is required"}), 422
        if m["target_object"] not in ("Account", "Contact", "Opportunity"):
            return jsonify({"error": (
                "target_object must be Account, Contact or Opportunity")}), 422
        lead_fields = {f["name"] for f in
                       (registry.get_object("Lead") or {}).get("fields", [])}
        if m["lead_field"] not in lead_fields:
            return jsonify({"error": (
                f"lead_field '{m['lead_field']}' is not on Lead")}), 422
        target_fields = {f["name"]: f for f in
                         (registry.get_object(m["target_object"]) or {})
                         .get("fields", [])}
        tf = target_fields.get(m["target_field"])
        if not tf:
            return jsonify({"error": (
                f"target_field '{m['target_field']}' is not on "
                f"{m['target_object']}")}), 422
        if tf.get("formula") or tf.get("rollup"):
            return jsonify({"error": (
                "target_field must not be a formula or roll-up field")}), 422
        return None

    def _validate_web_to_form(f):
        """422 response or None. ``f`` is the merged form dict."""
        if not f.get("key"):
            return jsonify({"error": "key is required"}), 422
        obj_name = f.get("object") or "Lead"
        obj = registry.get_object(obj_name)
        if not obj:
            return jsonify({"error": f"Unknown object '{obj_name}'"}), 422
        fields = f.get("fields") or []
        if not isinstance(fields, list) or not fields:
            return jsonify({"error": "fields must be a non-empty list"}), 422
        obj_fields = {fd["name"]: fd for fd in obj.get("fields", [])}
        for fn in fields:
            fd = obj_fields.get(fn)
            if not fd:
                return jsonify({"error": (
                    f"field '{fn}' is not on {obj_name}")}), 422
            if fd.get("formula") or fd.get("rollup"):
                return jsonify({"error": (
                    f"field '{fn}' is computed and cannot be web-submitted")}), 422
        for fn in f.get("required") or []:
            if fn not in fields:
                return jsonify({"error": (
                    f"required field '{fn}' is not in fields")}), 422
        for dk in (f.get("defaults") or {}):
            if dk not in obj_fields:
                return jsonify({"error": (
                    f"default field '{dk}' is not on {obj_name}")}), 422
        return None

    @app.get("/api/admin/<kind>")
    @require_auth
    @require_admin
    def admin_list_config(kind):
        table = CONFIG_TABLES.get(kind)
        if not table:
            return jsonify({"error": "Unknown config type"}), 404
        rows = store.config_all(table)
        if "limit" in request.args or "offset" in request.args:
            # Paginated envelope; without these params the legacy array
            # shape is returned unchanged.
            limit = max(1, min(500, int(request.args.get("limit", 50))))
            offset = max(0, int(request.args.get("offset", 0)))
            return jsonify({"rows": rows[offset:offset + limit],
                            "total": len(rows), "limit": limit,
                            "offset": offset})
        return jsonify(rows)

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

    @app.get("/api/admin/<kind>/<rid>")
    @require_auth
    @require_admin
    def admin_get_config(kind, rid):
        table = CONFIG_TABLES.get(kind)
        if not table:
            return jsonify({"error": "Unknown config type"}), 404
        row = store.config_get(table, rid)
        if not row:
            return jsonify({"error": "Not found"}), 404
        return jsonify(row)

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
        if kind == "flows" and body.get("trigger") == "scheduled":
            if "criteria" in body and "condition" not in body:
                body["condition"] = body.pop("criteria")
            sched = dict(body.get("schedule") or {})
            if sched.get("frequency") == "weekly" and "day_of_week" not in sched:
                sched["day_of_week"] = 1
            body["schedule"] = sched
            err = automation.validate_scheduled_flow(body, registry)
            if err:
                return jsonify({"error": err}), 422
            body["next_run"] = automation.compute_next_run(sched)
        if kind == "divisions":
            name = (body.get("name") or "").strip()
            if not name:
                return jsonify({"error": "Division name is required"}), 422
            if _divisions.get_division_by_name(store, name):
                return jsonify({"error": f"A division named '{name}' "
                                          "already exists"}), 422
            body["name"] = name
            body.setdefault("active", True)
            body.setdefault("description", "")
        if kind == "sla-policies":
            body.setdefault("object", "Case")
            err = _validate_sla_policy(body)
            if err:
                return err
        if kind == "rollup-rules":
            body.setdefault("func", "sum")
            body.setdefault("active", True)
            err = _validate_rollup_rule(body)
            if err:
                return err
        if kind == "lead-field-mappings":
            body.setdefault("active", True)
            err = _validate_lead_field_mapping(body)
            if err:
                return err
        if kind == "web-to-forms":
            body.setdefault("active", True)
            err = _validate_web_to_form(body)
            if err:
                return err
            dup = [r for r in store.config_all(table)
                   if r.get("key") == body.get("key")]
            if dup:
                return jsonify({"error": (
                    f"key '{body.get('key')}' already exists")}), 422
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
        if kind == "divisions" and "name" in body:
            name = (body.get("name") or "").strip()
            if not name:
                return jsonify({"error": "Division name is required"}), 422
            other = _divisions.get_division_by_name(store, name)
            if other and other["id"] != rid:
                return jsonify({"error": f"A division named '{name}' "
                                          "already exists"}), 422
            body["name"] = name
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
            if "criteria" in body and "condition" not in body:
                body["condition"] = body.pop("criteria")
            merged = {**old, **body}
            if merged.get("trigger") == "scheduled":
                sched = dict(merged.get("schedule") or {})
                if sched.get("frequency") == "weekly" and "day_of_week" not in sched:
                    sched["day_of_week"] = 1
                merged["schedule"] = sched
                err = automation.validate_scheduled_flow(merged, registry)
                if err:
                    return jsonify({"error": err}), 422
                if body.get("schedule") and body["schedule"] != old.get("schedule"):
                    merged["next_run"] = automation.compute_next_run(sched)
                elif not merged.get("next_run"):
                    merged["next_run"] = automation.compute_next_run(sched)
        else:
            merged = {**old, **body}
        if kind == "sla-policies":
            err = _validate_sla_policy(merged)
            if err:
                return err
        if kind == "rollup-rules":
            err = _validate_rollup_rule(merged)
            if err:
                return err
        if kind == "lead-field-mappings":
            err = _validate_lead_field_mapping(merged)
            if err:
                return err
        if kind == "web-to-forms":
            err = _validate_web_to_form(merged)
            if err:
                return err
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
        if kind == "divisions" and old:
            n = _divisions.count_records(store, rid)
            if n:
                return jsonify({"error": f"Division has {n} assigned record(s) — "
                                         "move them to another division first"}), 409
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

    # ------------------------------------------------- field & object manager
    @app.get("/api/admin/objects/<obj_name>/fields")
    @require_auth
    @require_admin
    def admin_list_fields(obj_name):
        """Admin field list — includes deactivated fields hidden from describe."""
        try:
            obj = registry._managed_object(obj_name)
        except KeyError:
            return jsonify({"error": f"Unknown object '{obj_name}'"}), 404
        except PermissionError as e:
            return jsonify({"error": str(e)}), 403
        out = []
        for f in obj.get("fields", []):
            f2 = dict(f)
            if not (f.get("formula") or f.get("rollup")):
                f2["value_count"] = registry.field_value_count(obj_name, f["name"])
            out.append(f2)
        return jsonify(out)

    @app.put("/api/admin/objects/<obj_name>/fields/<fname>")
    @require_auth
    @require_admin
    def admin_update_field(obj_name, fname):
        try:
            field = registry.update_field(obj_name, fname, request.json or {})
        except KeyError:
            return jsonify({"error": f"Unknown field '{fname}' on {obj_name}"}), 404
        except PermissionError as e:
            return jsonify({"error": str(e)}), 403
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("update", "field", f"{obj_name}.{fname}")
        return jsonify(field)

    @app.delete("/api/admin/objects/<obj_name>/fields/<fname>")
    @require_auth
    @require_admin
    def admin_delete_field(obj_name, fname):
        try:
            registry.delete_field(obj_name, fname)
        except KeyError:
            return jsonify({"error": f"Unknown field '{fname}' on {obj_name}"}), 404
        except PermissionError as e:
            return jsonify({"error": str(e)}), 403
        except ValueError as e:
            return jsonify({"error": str(e)}), 409
        _audit("delete", "field", f"{obj_name}.{fname}")
        return jsonify({"deleted": True})

    @app.delete("/api/admin/objects/<obj_name>")
    @require_auth
    @require_admin
    def admin_delete_object(obj_name):
        try:
            registry.delete_object(obj_name)
        except KeyError:
            return jsonify({"error": f"Unknown object '{obj_name}'"}), 404
        except PermissionError as e:
            return jsonify({"error": str(e)}), 403
        except ValueError as e:
            return jsonify({"error": str(e)}), 409
        _audit("delete", "object", obj_name)
        return jsonify({"deleted": True})

    # ----------------------------------------- object-level settings (A2)
    #: Settings an admin may change on an existing object definition.
    OBJECT_SETTINGS = ("label", "plural", "calendar_date_field",
                       "calendar_label_field", "calendar_color")

    @app.put("/api/admin/objects/<obj_name>")
    @require_auth
    @require_admin
    def admin_update_object(obj_name):
        obj = registry.get_object(obj_name)
        if not obj:
            return jsonify({"error": f"Unknown object '{obj_name}'"}), 404
        body = request.json or {}
        if body.get("name") and body["name"] != obj_name:
            return jsonify(
                {"error": "Renaming an object is not supported"}), 422
        fields = {f["name"]: f for f in obj.get("fields", [])}
        updates = {}
        if "label" in body or "plural" in body:
            for key in ("label", "plural"):
                if key in body:
                    val = (body.get(key) or "").strip()
                    if not val:
                        return jsonify(
                            {"error": f"{key} must not be blank"}), 422
                    updates[key] = val
        if "calendar_date_field" in body:
            # "" = explicitly removed from the calendar;
            # null = clear the override (built-ins fall back to their default)
            raw = body.get("calendar_date_field")
            val = (raw or "").strip() if raw is not None else ""
            if raw is None:
                updates["calendar_date_field"] = None
            else:
                if val:
                    f = fields.get(val)
                    if not f or f.get("type") not in ("Date", "DateTime"):
                        return jsonify({"error": (
                            "calendar_date_field must be an existing "
                            "Date or DateTime field")}), 422
                updates["calendar_date_field"] = val
        if "calendar_label_field" in body:
            val = (body.get("calendar_label_field") or "").strip()
            if val and val not in fields:
                return jsonify(
                    {"error": "calendar_label_field must be an existing "
                             "field"}), 422
            updates["calendar_label_field"] = val or None
        if "calendar_color" in body:
            val = (body.get("calendar_color") or "").strip()
            if val and not re.fullmatch(
                    r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", val):
                return jsonify(
                    {"error": "calendar_color must be a hex color like "
                             "#0176d3"}), 422
            updates["calendar_color"] = val or None
        unknown = [k for k in body if k not in OBJECT_SETTINGS + ("name",)]
        if unknown:
            return jsonify(
                {"error": f"Unknown object setting(s): {', '.join(unknown)}"}
            ), 422
        obj.update(updates)
        store.meta_put("mf_objects", obj_name, obj)
        _audit("update", "object", obj_name)
        return jsonify(obj)

    @app.get("/api/admin/users")
    @require_auth
    @require_admin_scope("users")
    def admin_list_users():
        return jsonify([{k: v for k, v in u.items() if k != "password_hash"}
                        for u in security.list_users()])

    @app.post("/api/admin/users")
    @require_auth
    @require_admin_scope("users")
    def admin_create_user():
        body = request.json or {}
        denied = _delegated_target_ok(("users",), body.get("role"))
        if denied:
            return denied
        password = body.get("password") or "forcelet"
        sec = get_settings(store, SECURITY_KEY)
        perr = check_password_policy(password, body.get("username", ""), sec)
        if perr:
            return jsonify({"error": perr}), 422
        try:
            user = security.create_user(body.get("username", ""), body.get("name", ""),
                                        body.get("profile", ""), body.get("role"),
                                        password, email=body.get("email", ""))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        user = {k: v for k, v in user.items() if k != "password_hash"}
        _audit("create", "user", user["username"])
        return jsonify(user), 201

    @app.put("/api/admin/users/<uid>")
    @require_auth
    @require_admin_scope("users")
    def admin_update_user(uid):
        target = security.get_user(uid)
        if not target:
            return jsonify({"error": "Unknown user"}), 404
        denied = _delegated_target_ok(
            ("users",), (request.json or {}).get("role", target.get("role")))
        if denied:
            return denied
        body = request.json or {}
        if target["id"] == request.mf_user["id"] and "is_active" in body \
                and not body["is_active"]:
            return jsonify({"error": "You cannot deactivate your own account"}), 422
        patch = {k: body[k] for k in ("name", "email", "role", "profile", "is_active")
                 if k in body}
        try:
            user = security.update_user(uid, patch)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        if "is_active" in patch and not patch["is_active"]:
            # Deactivation takes effect immediately: kill all sessions.
            store.delete_user_sessions(uid)
        _audit("update", "user", user["username"])
        return jsonify({k: v for k, v in user.items() if k != "password_hash"})

    @app.delete("/api/admin/users/<uid>")
    @require_auth
    @require_admin_scope("users")
    def admin_delete_user(uid):
        target = security.get_user(uid)
        if not target:
            return jsonify({"error": "Unknown user"}), 404
        denied = _delegated_target_ok(("users",), target.get("role"))
        if denied:
            return denied
        if target["id"] == request.mf_user["id"]:
            return jsonify({"error": "You cannot delete your own account"}), 422
        try:
            rows, _total = store.login_history(user_id=uid, success=True, limit=1)
        except Exception:
            rows = []
        if rows:
            return jsonify({"error": "This user has signed in before — "
                                     "deactivate the account instead of deleting it"}), 422
        security.delete_user(uid)
        store.delete_user_sessions(uid)
        _audit("delete", "user", target["username"])
        return jsonify({"deleted": True})

    @app.post("/api/admin/users/<uid>/reset-password")
    @require_auth
    @require_admin_scope("users", "passwords")
    def admin_reset_password(uid):
        target = security.get_user(uid)
        if not target:
            return jsonify({"error": "Unknown user"}), 404
        denied = _delegated_target_ok(("users", "passwords"), target.get("role"))
        if denied:
            return denied
        if target["id"] == request.mf_user["id"]:
            return jsonify({"error": "Use Change Password for your own account"}), 422
        temp = secrets.token_urlsafe(12)
        security.set_password(uid, temp)
        fresh = security.get_user(uid)
        fresh["must_change_password"] = True
        store.meta_put("mf_users", uid, fresh)
        # A password reset invalidates every existing session.
        store.delete_user_sessions(uid)
        _audit("reset-password", "user", target["username"])
        return jsonify({"temporary_password": temp,
                        "message": "Copy this now — it is shown only once. "
                                   "The user must change it at next sign-in."})

    @app.post("/api/admin/users/<uid>/unlock")
    @require_auth
    @require_admin_scope("users", "passwords")
    def admin_unlock_user(uid):
        target = security.get_user(uid)
        if not target:
            return jsonify({"error": "Unknown user"}), 404
        denied = _delegated_target_ok(("users", "passwords"), target.get("role"))
        if denied:
            return denied
        if target["id"] == request.mf_user["id"]:
            return jsonify({"error": "You cannot unlock your own account"}), 422
        reset_login_attempts_for_user(target["username"])
        _audit("unlock", "user", target["username"])
        return jsonify({"unlocked": True})

    @app.post("/api/admin/users/<uid>/permission-sets")
    @require_auth
    @require_admin_scope("users")
    def admin_assign_ps(uid, ):
        target = security.get_user(uid)
        if not target:
            return jsonify({"error": "Unknown user"}), 404
        denied = _delegated_target_ok(("users",), target.get("role"))
        if denied:
            return denied
        try:
            user = security.assign_permission_set(uid, (request.json or {}).get("permission_set", ""))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("assign", "permission-set", (request.json or {}).get("permission_set", ""),
               f"user={user['username']}")
        return jsonify({k: v for k, v in user.items() if k != "password_hash"})

    @app.get("/api/admin/users/<uid>/permission-sets")
    @require_auth
    @require_admin_scope("users")
    def admin_user_ps(uid):
        user = security.get_user(uid)
        if not user:
            return jsonify({"error": "Unknown user"}), 404
        out = []
        for pid in user.get("permission_sets", []):
            ps = store.config_get("mf_permission_sets", pid)
            out.append({"id": pid, "name": (ps or {}).get("name", pid),
                        "label": (ps or {}).get("label", ""),
                        "missing": ps is None})
        return jsonify(out)

    @app.delete("/api/admin/users/<uid>/permission-sets/<psid>")
    @require_auth
    @require_admin_scope("users")
    def admin_unassign_ps(uid, psid):
        target = security.get_user(uid)
        if not target:
            return jsonify({"error": "Unknown user"}), 404
        denied = _delegated_target_ok(("users",), target.get("role"))
        if denied:
            return denied
        try:
            user = security.unassign_permission_set(uid, psid)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("unassign", "permission-set", psid, f"user={user['username']}")
        return jsonify({"unassigned": psid})

    @app.get("/api/admin/roles")
    @require_auth
    @require_admin_scope("roles")
    def admin_list_roles():
        return jsonify(security.list_roles())

    @app.post("/api/admin/roles")
    @require_auth
    @require_admin_scope("roles")
    def admin_create_role():
        body = request.json or {}
        try:
            role = security.create_role(body.get("name", ""), body.get("parent"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("create", "role", role["name"])
        return jsonify(role), 201

    @app.put("/api/admin/roles/<name>")
    @require_auth
    @require_admin_scope("roles")
    def admin_update_role(name):
        body = request.json or {}
        try:
            role = security.update_role(
                name,
                body.get("name"),
                body["parent"] if "parent" in body else security._UNSET,
            )
        except KeyError as e:
            return jsonify({"error": str(e)}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("update", "role", role["name"])
        return jsonify(role)

    @app.delete("/api/admin/roles/<name>")
    @require_auth
    @require_admin_scope("roles")
    def admin_delete_role(name):
        try:
            security.delete_role(name)
        except KeyError as e:
            return jsonify({"error": str(e)}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 409
        _audit("delete", "role", name)
        return jsonify({"deleted": name})

    @app.get("/api/admin/profiles")
    @require_auth
    @require_admin_scope("profiles")
    def admin_list_profiles():
        return jsonify(security.list_profiles())

    @app.post("/api/admin/profiles")
    @require_auth
    @require_admin_scope("profiles")
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

    @app.get("/api/admin/layouts")
    @require_auth
    @require_admin
    def admin_list_layouts():
        """Every saved page layout with its assignment (object/profile/record type)."""
        return jsonify([{"object": l["object"], "profile": l["profile"],
                         "record_type": l.get("record_type", "Default"),
                         "sections": len(l.get("sections", [])),
                         "related_lists": len(l.get("related_lists", []))}
                        for l in store.layouts_all()])

    @app.delete("/api/admin/layouts")
    @require_auth
    @require_admin
    def admin_delete_layout():
        obj = request.args.get("object", "")
        profile = request.args.get("profile", "Default")
        rt = request.args.get("record_type", "Default")
        if not obj:
            return jsonify({"error": "object is required"}), 422
        ok = store.layout_delete(obj, profile, rt)
        if ok:
            _audit("delete", "layout", f"{obj}/{profile}/{rt}")
        return jsonify({"deleted": ok})

    @app.get("/api/admin/profiles/<name>")
    @require_auth
    @require_admin_scope("profiles")
    def admin_get_profile(name):
        prof = security.get_profile(name)
        if not prof:
            return jsonify({"error": "Unknown profile"}), 404
        return jsonify(prof)

    @app.put("/api/admin/profiles/<name>")
    @require_auth
    @require_admin_scope("profiles")
    def admin_update_profile(name):
        prof = security.get_profile(name)
        if not prof:
            return jsonify({"error": "Unknown profile"}), 404
        body = request.json or {}
        if body.get("name") and body["name"] != name:
            return jsonify({"error": "Renaming a profile is not supported"}), 422
        obj_perms = body.get("object_permissions", prof.get("object_permissions") or {})
        field_perms = body.get("field_permissions", prof.get("field_permissions") or {})
        # Structured validation: objects and fields must exist.
        for obj_name, perms in (obj_perms or {}).items():
            if obj_name != "*" and not registry.get_object(obj_name):
                return jsonify({"error": f"Unknown object '{obj_name}'"}), 422
            for act in (perms or {}):
                if act not in ("create", "read", "edit", "delete"):
                    return jsonify({"error": f"Unknown permission '{act}'"}), 422
        for obj_name, fields in (field_perms or {}).items():
            obj = registry.get_object(obj_name)
            if not obj:
                return jsonify({"error": f"Unknown object '{obj_name}'"}), 422
            fmap = registry.field_map(obj)
            for fname, fp in (fields or {}).items():
                if fname not in fmap:
                    return jsonify({"error": f"Unknown field '{obj_name}.{fname}'"}), 422
                for k in (fp or {}):
                    if k not in ("read", "edit"):
                        return jsonify({"error": f"Unknown field permission '{k}'"}), 422
        prof["object_permissions"] = {o: {a: bool(v) for a, v in (p or {}).items()}
                                     for o, p in (obj_perms or {}).items()}
        prof["field_permissions"] = {o: {f: {k: bool(v) for k, v in (fp or {}).items()}
                                         for f, fp in (fs or {}).items()}
                                     for o, fs in (field_perms or {}).items()}
        if "default_division" in body:
            div_id = (body.get("default_division") or "").strip()
            if div_id:
                div = _divisions.get_division(store, div_id)
                if not div:
                    return jsonify({"error": f"Unknown division '{div_id}'"}), 422
                prof["default_division"] = div_id
            else:
                prof.pop("default_division", None)
        store.meta_put("mf_profiles", name, prof)
        _audit("update", "profile", name)
        return jsonify(prof)

    # ------------------------------------------------- org & admin settings
    _SETTING_BLOBS = {"org": ORG_KEY, "security": SECURITY_KEY,
                      "portal": PORTAL_KEY, "chatter": CHATTER_KEY,
                      "login": LOGIN_KEY}

    @app.get("/api/admin/settings/<blob>")
    @require_auth
    @require_admin
    def admin_get_settings(blob):
        key = _SETTING_BLOBS.get(blob)
        if not key:
            return jsonify({"error": "Unknown settings area"}), 404
        return jsonify(get_settings(store, key))

    @app.put("/api/admin/settings/<blob>")
    @require_auth
    @require_admin
    def admin_put_settings(blob):
        key = _SETTING_BLOBS.get(blob)
        if not key:
            return jsonify({"error": "Unknown settings area"}), 404
        try:
            merged = save_settings(store, key, request.json or {})
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("update", "settings", blob)
        return jsonify(merged)

    # ------------------------------------------------------- login history
    @app.get("/api/admin/login-history")
    @require_auth
    @require_admin
    def admin_login_history():
        args = request.args
        success = args.get("success")
        if success in ("1", "true", "True"):
            success_b = True
        elif success in ("0", "false", "False"):
            success_b = False
        else:
            success_b = None
        try:
            limit = max(1, min(int(args.get("limit", 50)), 200))
            offset = max(0, int(args.get("offset", 0)))
        except ValueError:
            return jsonify({"error": "limit/offset must be numbers"}), 422
        rows, total = store.login_history(
            username=args.get("username") or None,
            user_id=args.get("user_id") or None,
            success=success_b,
            since=args.get("from") or None,
            until=args.get("to") or None,
            limit=limit, offset=offset)
        return jsonify({"rows": rows, "total": total,
                        "limit": limit, "offset": offset})

    # ------------------------------------------------------------- storage
    @app.get("/api/admin/storage")
    @require_auth
    @require_admin
    def admin_storage():
        objects = []
        total_records = 0
        for obj in registry.list_objects():
            try:
                n = store.count(obj["name"])
            except Exception:
                n = None
            if isinstance(n, int):
                total_records += n
            objects.append({"object": obj["name"], "label": obj.get("label"),
                            "records": n, "custom": bool(obj.get("is_custom"))})
        objects.sort(key=lambda o: (o["records"] is None, -(o["records"] or 0)))

        def _fsize(p):
            try:
                return os.path.getsize(p)
            except OSError:
                return 0

        def _dir_size(p):
            total = 0
            if os.path.isdir(p):
                for _root, _dirs, files in os.walk(p):
                    for f in files:
                        total += _fsize(os.path.join(_root, f))
            return total

        db_path = store.db_path
        db_bytes = _fsize(db_path) + _fsize(db_path + "-wal") + _fsize(db_path + "-shm")
        files_bytes = _dir_size(os.path.join(os.path.dirname(os.path.abspath(db_path)), "files"))
        bdir = _backup_dir()
        backups = []
        if os.path.isdir(bdir):
            for bname in sorted(os.listdir(bdir), reverse=True):
                if bname.startswith("forcelet-") and bname.endswith(".db"):
                    backups.append({"name": bname,
                                    "size_bytes": _fsize(os.path.join(bdir, bname))})
        backups_bytes = sum(b["size_bytes"] for b in backups)
        return jsonify({"objects": objects, "total_records": total_records,
                        "database_bytes": db_bytes, "files_bytes": files_bytes,
                        "backups": backups, "backups_bytes": backups_bytes})

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
