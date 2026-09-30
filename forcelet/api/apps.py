"""Custom applications (App Manager) endpoints. — Forcelet REST API domain module.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

from flask import Flask, jsonify, request

from .. import apps as apps_mod
from ._shared import (
    _audit, current_user, require_admin, require_auth, ctx,
)


def register(app: Flask):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security

    # ------------------------------------------------------- user-facing
    @app.get("/api/apps")
    @require_auth
    def list_visible_apps():
        user = request.mf_user
        visible = apps_mod.visible_apps(store, security, user)
        if not store.config_all(apps_mod.APP_TABLE):
            return jsonify({"apps": [], "legacy": True,
                            "default_app_id": None})
        return jsonify({"apps": visible, "legacy": False,
                        "default_app_id": apps_mod.default_app_id(visible)})

    # ------------------------------------------------------- admin CRUD
    @app.get("/api/admin/apps")
    @require_auth
    @require_admin
    def admin_list_apps():
        return jsonify(apps_mod.list_apps(store))

    @app.post("/api/admin/apps")
    @require_auth
    @require_admin
    def admin_create_app():
        user = request.mf_user
        try:
            saved = apps_mod.save_app(store, request.get_json(force=True) or {})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        _audit(user, "create", "app", saved["name"])
        return jsonify(saved), 201

    @app.get("/api/admin/apps/<app_id>")
    @require_auth
    @require_admin
    def admin_get_app(app_id):
        found = apps_mod.get_app(store, app_id)
        if not found:
            return jsonify({"error": "App not found"}), 404
        return jsonify(found)

    @app.put("/api/admin/apps/<app_id>")
    @require_auth
    @require_admin
    def admin_update_app(app_id):
        user = request.mf_user
        found = apps_mod.get_app(store, app_id)
        if not found:
            return jsonify({"error": "App not found"}), 404
        body = request.get_json(force=True) or {}
        body["id"] = app_id
        try:
            saved = apps_mod.save_app(store, {**found, **body})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        _audit(user, "update", "app", saved["name"])
        return jsonify(saved)

    @app.delete("/api/admin/apps/<app_id>")
    @require_auth
    @require_admin
    def admin_delete_app(app_id):
        user = request.mf_user
        found = apps_mod.get_app(store, app_id)
        if not found:
            return jsonify({"error": "App not found"}), 404
        apps_mod.delete_app(store, app_id)
        _audit(user, "delete", "app", found.get("name", app_id))
        return jsonify({"deleted": app_id})

    @app.post("/api/admin/apps/seed")
    @require_auth
    @require_admin
    def admin_seed_apps():
        user = request.mf_user
        created = apps_mod.seed_apps_if_missing(store)
        if created:
            _audit(user, "create", "app",
                   ", ".join(a["name"] for a in created))
        return jsonify({"created": [a["name"] for a in created],
                        "total": len(store.config_all(apps_mod.APP_TABLE))})

    # ------------------------------------------------- layout override hook
    # The /api/layout/<obj> endpoint lives in metadata.py; it calls
    # apps.resolve_layout() when ?app= is passed. Nothing to register here.
