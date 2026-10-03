"""Platform services API: field history tracking, cron scheduling helpers,
change sets, and semantic search. — Forcelet REST API domain module.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import json

from flask import Flask, jsonify, request, Response

from .. import automation
from .. import changesets
from .. import cron as _cron
from .. import history_tracking
from .. import semantic as _semantic
from ._shared import _audit, require_admin, require_auth


def register(app: Flask):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security

    # --------------------------------------- field history tracking
    @app.get("/api/admin/history-tracking")
    @require_auth
    @require_admin
    def ht_list():
        return jsonify(history_tracking.list_configs(store, registry))

    @app.put("/api/admin/history-tracking/<obj_name>")
    @require_auth
    @require_admin
    def ht_put(obj_name):
        if not registry.get_object(obj_name):
            return jsonify({"error": f"unknown object {obj_name}"}), 404
        body = request.json or {}
        fields = body.get("fields") or []
        obj = registry.get_object(obj_name)
        known = {f["name"] for f in obj.get("fields", [])}
        bad = [f for f in fields if f not in known]
        if bad:
            return jsonify({"error": f"unknown fields: {', '.join(bad)}"}), 422
        try:
            cfg = history_tracking.set_config(
                store, obj_name,
                enabled=bool(body.get("enabled", True)),
                fields=fields,
                retention_days=int(body.get("retention_days")
                                   or history_tracking.DEFAULT_RETENTION_DAYS))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        return jsonify(cfg)

    @app.post("/api/admin/history-tracking/purge")
    @require_auth
    @require_admin
    def ht_purge():
        return jsonify({"purged": history_tracking.purge_old_history(store)})

    # --------------------------------------- cron scheduling helpers
    @app.post("/api/admin/scheduled-jobs/validate-cron")
    @require_auth
    @require_admin
    def cron_validate():
        expr = ((request.json or {}).get("cron") or "").strip()
        try:
            spec = _cron.parse(expr)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)})
        return jsonify({"ok": True, "description": spec.describe()})

    @app.get("/api/admin/scheduled-jobs/<job_id>/runs")
    @require_auth
    @require_admin
    def job_runs(job_id):
        return jsonify(store.scheduled_runs(job_id,
                                            limit=int(request.args.get("limit", 50))))

    # --------------------------------------- change sets
    @app.get("/api/admin/change-sets")
    @require_auth
    @require_admin
    def cs_list():
        return jsonify([changesets._public(c)
                        for c in changesets.list_changesets(store)])

    @app.post("/api/admin/change-sets")
    @require_auth
    @require_admin
    def cs_create():
        body = request.json or {}
        try:
            cs = changesets.create_changeset(
                store, body.get("name") or "", body.get("description") or "",
                request.mf_user.get("username", ""),
                body.get("status") or "Draft")
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("create", "change-set", cs["name"])
        return jsonify(changesets._public(cs)), 201

    @app.get("/api/admin/change-sets/<cs_id>")
    @require_auth
    @require_admin
    def cs_get(cs_id):
        cs = changesets.get_changeset(store, cs_id)
        if not cs:
            return jsonify({"error": "not found"}), 404
        return jsonify(changesets._public(cs))

    @app.post("/api/admin/change-sets/<cs_id>/components")
    @require_auth
    @require_admin
    def cs_components(cs_id):
        body = request.json or {}
        action = (body.get("action") or "add").lower()
        try:
            if action == "remove":
                cs = changesets.remove_component(store, cs_id,
                                                 body.get("type") or "",
                                                 body.get("ref") or "")
            else:
                cs = changesets.add_component(store, cs_id,
                                              body.get("type") or "",
                                              body.get("ref") or "")
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit(action, "change-set",
               f"{cs['name']}: {body.get('type') or ''}/{body.get('ref') or ''}")
        return jsonify(changesets._public(cs))

    @app.post("/api/admin/change-sets/<cs_id>/status")
    @require_auth
    @require_admin
    def cs_status(cs_id):
        cs = changesets.get_changeset(store, cs_id)
        if not cs:
            return jsonify({"error": "not found"}), 404
        status = (request.json or {}).get("status") or ""
        if status not in changesets.STATUSES:
            return jsonify({"error": f"status must be one of {changesets.STATUSES}"}), 422
        cs["status"] = status
        changesets._save(store, cs)
        _audit("status-change", "change-set", f"{cs['name']} -> {status}")
        return jsonify(changesets._public(cs))

    @app.get("/api/admin/change-sets/components/available")
    @require_auth
    @require_admin
    def cs_available():
        return jsonify(changesets.available_components(store, registry))

    @app.get("/api/admin/change-sets/<cs_id>/download")
    @require_auth
    @require_admin
    def cs_download(cs_id):
        try:
            doc = changesets.export_changeset(store, registry, cs_id)
        except ValueError as e:
            return jsonify({"error": str(e)}), 404
        return Response(json.dumps(doc, indent=1), mimetype="application/json",
                        headers={"Content-Disposition":
                                 f"attachment; filename=changeset-{cs_id}.json"})

    @app.post("/api/admin/change-sets/upload")
    @require_auth
    @require_admin
    def cs_upload():
        doc = (request.json or {}).get("changeset")
        try:
            cs = changesets.import_changeset_doc(
                store, doc, request.mf_user.get("username", ""))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("upload", "change-set", cs.get("name") or "Uploaded change set")
        return jsonify(cs)

    @app.post("/api/admin/change-sets/<cs_id>/validate")
    @require_auth
    @require_admin
    def cs_validate(cs_id):
        try:
            result = changesets.validate_changeset(store, registry, cs_id)
        except ValueError as e:
            return jsonify({"error": str(e)}), 404
        cs = changesets.get_changeset(store, cs_id)
        _audit("validate", "change-set", (cs or {}).get("name") or cs_id)
        return jsonify(result)

    @app.post("/api/admin/change-sets/<cs_id>/deploy")
    @require_auth
    @require_admin
    def cs_deploy(cs_id):
        cs = changesets.get_changeset(store, cs_id)
        if not cs:
            return jsonify({"error": "not found"}), 404
        try:
            dep = changesets.deploy_changeset(store, registry, cs_id,
                                              request.mf_user)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("deploy", "change-set",
               f"{cs['name']} ({dep.get('status')})")
        return jsonify(dep)

    @app.get("/api/admin/change-sets/<cs_id>/deployments")
    @require_auth
    @require_admin
    def cs_deployments(cs_id):
        return jsonify(changesets.list_deployments(store, cs_id))

    @app.delete("/api/admin/change-sets/<cs_id>")
    @require_auth
    @require_admin
    def cs_delete(cs_id):
        # Change sets are SQL rows, not records: delete + audit, no recycle
        # bin. Deployment history must not be orphaned: a change set that
        # was ever deployed (or deploy-attempted) is frozen.
        cs = changesets.get_changeset(store, cs_id)
        if not cs:
            return jsonify({"error": "not found"}), 404
        deployments = changesets.list_deployments(store, cs_id)
        if deployments:
            latest = deployments[0].get("status")
            return jsonify({"error": f"Change set has {len(deployments)} "
                                     f"deployment record(s) (latest: {latest}) "
                                     "— deployment history is kept, the "
                                     "change set cannot be deleted"}), 409
        changesets.delete_changeset(store, cs_id)
        _audit("delete", "change-set", cs["name"])
        return jsonify({"deleted": cs_id})

    @app.get("/api/admin/deployments")
    @require_auth
    @require_admin
    def deployments_all():
        return jsonify(changesets.list_deployments(store))

    # --------------------------------------- semantic search
    @app.get("/api/search/semantic")
    @require_auth
    def semantic_search_ep():
        user = request.mf_user
        q = request.args.get("q") or ""
        try:
            limit = max(1, min(int(request.args.get("limit", 25)), 100))
        except ValueError:
            limit = 25
        return jsonify(_semantic.semantic_search(store, registry, security,
                                                user, q, limit))
