"""Dynamic Forms: conditional field visibility rules. — Forcelet REST API module.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

from flask import Flask, jsonify, request

from .. import dynamic_forms as _df
from ._shared import (
    _audit, current_user, require_admin, require_auth, ctx,
)


def register(app: Flask):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security
    # ------------------------------------------------------------ dynamic forms
    @app.get("/api/dynamic-forms/<obj_name>")
    @require_auth
    def df_list_rules(obj_name):
        if not registry.get_object(obj_name):
            return jsonify({"error": "Unknown object"}), 404
        return jsonify({"rules": _df.rules_for(store, obj_name,
                                               active_only=False)})

    @app.post("/api/dynamic-forms/<obj_name>/rules")
    @require_auth
    @require_admin
    def df_create_rule(obj_name):
        body = request.json or {}
        rule = {"name": body.get("name"), "object": obj_name,
                "when": body.get("when"), "then": body.get("then"),
                "active": body.get("active", True)}
        errors = _df.validate_rule(rule, registry)
        if errors:
            return jsonify({"error": "Invalid rule", "details": errors}), 422
        rid = store.config_put(_df.TABLE, rule)
        rule["id"] = rid
        _audit("dynamic_form_create", obj_name, rule["name"], "")
        return jsonify(rule), 201

    @app.put("/api/dynamic-forms/<obj_name>/rules/<rid>")
    @require_auth
    @require_admin
    def df_update_rule(obj_name, rid):
        cur = store.config_get(_df.TABLE, rid)
        if not cur or cur.get("object") != obj_name:
            return jsonify({"error": "Rule not found"}), 404
        body = request.json or {}
        rule = {**cur,
                "name": body.get("name", cur.get("name")),
                "when": body.get("when", cur.get("when")),
                "then": body.get("then", cur.get("then")),
                "active": body.get("active", cur.get("active", True))}
        errors = _df.validate_rule(rule, registry)
        if errors:
            return jsonify({"error": "Invalid rule", "details": errors}), 422
        store.config_put(_df.TABLE, rule)
        _audit("dynamic_form_update", obj_name, rule["name"], "")
        return jsonify(rule)

    @app.delete("/api/dynamic-forms/<obj_name>/rules/<rid>")
    @require_auth
    @require_admin
    def df_delete_rule(obj_name, rid):
        cur = store.config_get(_df.TABLE, rid)
        if not cur or cur.get("object") != obj_name:
            return jsonify({"error": "Rule not found"}), 404
        store.config_delete(_df.TABLE, rid)
        _audit("dynamic_form_delete", obj_name, cur.get("name") or rid, "")
        return jsonify({"ok": True})
