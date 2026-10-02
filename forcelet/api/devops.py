"""DevOps REST API: Bulk API 2.0, streaming (SSE), sandboxes, source tracking,
custom metadata, managed packages, external objects.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import json

from flask import Flask, jsonify, request, Response

from .. import devops
from ._shared import (
    _audit, current_user, filter_change_event, require_admin, require_auth,
)


def _job_public(job: dict) -> dict:
    job = dict(job)
    job["csv_text"] = "<uploaded>" if job.get("csv_text") else ""
    job["row_count"] = job.get("row_count", 0)
    job.pop("results", None)
    return job


def register(app: Flask):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security
    devops.register_expression_resolvers(store)

    # ------------------------------------------------------------ Bulk API 2.0
    @app.post("/api/bulk/jobs")
    @require_auth
    @require_admin
    def bulk_create_job():
        body = request.json or {}
        obj_name = body.get("object")
        if not obj_name or not registry.get_object(obj_name):
            return jsonify({"error": "Unknown object"}), 422
        try:
            job = devops.create_ingest_job(
                store, request.mf_user, obj_name, body.get("operation") or "insert",
                body.get("externalIdFieldName"), registry=registry)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("create", "bulk-job", f"{job['operation']} {obj_name}")
        return jsonify(_job_public(job)), 201

    @app.get("/api/bulk/jobs")
    @require_auth
    @require_admin
    def bulk_list_jobs():
        return jsonify([_job_public(j)
                        for j in store.config_all(devops.BULK_JOB_TABLE)])

    @app.get("/api/bulk/jobs/<jid>")
    @require_auth
    @require_admin
    def bulk_get_job(jid):
        job = store.config_get(devops.BULK_JOB_TABLE, jid)
        if not job:
            return jsonify({"error": "Not found"}), 404
        return jsonify(_job_public(job))

    @app.put("/api/bulk/jobs/<jid>")
    @require_auth
    @require_admin
    def bulk_upload(jid):
        data = request.get_data(as_text=True) or ""
        try:
            job = devops.upload_job_data(store, jid, data)
        except KeyError:
            return jsonify({"error": "Not found"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        return jsonify(_job_public(job))

    @app.patch("/api/bulk/jobs/<jid>")
    @require_auth
    @require_admin
    def bulk_patch_job(jid):
        state = (request.json or {}).get("state")
        try:
            if state == "UploadComplete":
                job = devops.close_job(store, jid)
            elif state == "Aborted":
                job = devops.abort_job(store, jid)
            else:
                return jsonify({"error": "state must be UploadComplete or Aborted"}), 422
        except KeyError:
            return jsonify({"error": "Not found"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("update", "bulk-job", f"{job['object']} -> {job['state']}")
        return jsonify(_job_public(job))

    @app.delete("/api/bulk/jobs/<jid>")
    @require_auth
    @require_admin
    def bulk_delete_job(jid):
        try:
            job = devops.abort_job(store, jid)
        except KeyError:
            return jsonify({"error": "Not found"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        return jsonify(_job_public(job))

    @app.get("/api/bulk/jobs/<jid>/successful")
    @require_auth
    @require_admin
    def bulk_successful(jid):
        try:
            csv_text = devops.job_results_csv(store, jid, "successful")
        except KeyError:
            return jsonify({"error": "Not found"}), 404
        return Response(csv_text, mimetype="text/csv",
                        headers={"Content-Disposition":
                                 f"attachment; filename=job-{jid}-successful.csv"})

    @app.get("/api/bulk/jobs/<jid>/failed")
    @require_auth
    @require_admin
    def bulk_failed(jid):
        try:
            csv_text = devops.job_results_csv(store, jid, "failed")
        except KeyError:
            return jsonify({"error": "Not found"}), 404
        return Response(csv_text, mimetype="text/csv",
                        headers={"Content-Disposition":
                                 f"attachment; filename=job-{jid}-failed.csv"})

    # ------------------------------------------------------------ streaming (SSE)
    # Security: the broker payloads are shared across subscribers, so every
    # event is filtered per subscriber connection — record-change events are
    # dropped when the subscriber cannot see the record (sharing rules) and
    # snapshots are masked to fields the subscriber may read (FLS).
    # Scrubbed copies are built; broker payloads are never mutated in place.
    def _scrub_platform_event(user, payload):
        """Scrub records embedded in a platform-event payload.

        Convention: a payload embedding a record carries "object_name" plus
        either "record" (dict) or "record_id". The broker wraps published
        platform events as {"event_name", "payload", "published_by"}, so the
        embedded record is looked for both at the top level and inside the
        nested "payload" envelope. Payloads without an embedded record remain
        visible to any signed-in user, as before.
        """
        if not isinstance(payload, dict):
            return payload
        inner = payload.get("payload")
        target = inner if isinstance(inner, dict) else payload
        obj = target.get("object_name")
        embedded = target.get("record")
        rid = target.get("record_id")
        if not obj or (not isinstance(embedded, dict) and not rid):
            return payload
        got = filter_change_event(
            user, obj, rid if not isinstance(embedded, dict) else None,
            embedded if isinstance(embedded, dict) else None, None)
        if got is None:
            return None
        scrubbed, _fields = got
        out = dict(payload)
        if isinstance(embedded, dict):
            inner_out = dict(target)
            inner_out["record"] = scrubbed
            if inner is target:
                out["payload"] = inner_out
            else:
                out["record"] = scrubbed
        return out

    def _visible_event(user, e):
        """Return a scrubbed payload copy the subscriber may see, or None."""
        topic, payload = e["topic"], e["payload"]
        if topic.startswith("/data/") and topic.endswith("ChangeEvent"):
            obj = topic[len("/data/"):-len("ChangeEvent")]
            got = filter_change_event(
                user, obj, payload.get("record_id"),
                payload.get("snapshot"), payload.get("changed_fields"))
            if got is None:
                return None
            scrubbed, fields = got
            out = dict(payload)
            out["snapshot"] = scrubbed
            out["changed_fields"] = fields
            return out
        if topic.startswith("/event/"):
            return _scrub_platform_event(user, payload)
        return payload

    @app.get("/api/streaming")
    @require_auth
    def streaming():
        user = request.mf_user
        topics = {t.strip() for t in
                  (request.args.get("topics") or "").split(",") if t.strip()} or None
        try:
            since = int(request.args.get("since")
                        or request.headers.get("Last-Event-ID") or 0)
        except ValueError:
            since = 0

        def gen():
            last = since
            yield ": connected\nretry: 5000\n\n"
            for e in devops.broker.events_since(last, topics):
                last = max(last, e["seq"])
                payload = _visible_event(user, e)
                if payload is None:
                    continue
                yield (f"id: {e['seq']}\nevent: {e['topic']}\n"
                       f"data: {json.dumps(payload)}\n\n")
            while True:
                devops.broker.wait(25)
                for e in devops.broker.events_since(last, topics):
                    last = max(last, e["seq"])
                    payload = _visible_event(user, e)
                    if payload is None:
                        continue
                    yield (f"id: {e['seq']}\nevent: {e['topic']}\n"
                           f"data: {json.dumps(payload)}\n\n")

        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    @app.post("/api/streaming/events")
    @require_auth
    def publish_event():
        body = request.json or {}
        try:
            event = devops.publish_platform_event(
                body.get("name") or "", body.get("payload") or {},
                request.mf_user)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        return jsonify(event), 201

    # ------------------------------------------------------------ sandboxes
    @app.get("/api/admin/sandboxes")
    @require_auth
    @require_admin
    def list_sandboxes():
        return jsonify(devops.list_sandboxes(store))

    @app.post("/api/admin/sandboxes")
    @require_auth
    @require_admin
    def create_sandbox():
        body = request.json or {}
        try:
            sb = devops.create_sandbox(
                store, request.mf_user, body.get("name") or "",
                body.get("kind") or "developer",
                scratch=bool(body.get("scratch")),
                expires_in_days=body.get("expires_in_days"),
                sanitize=bool(body.get("sanitize", True)))
        except (ValueError, FileExistsError) as e:
            return jsonify({"error": str(e)}), 422
        _audit("create", "sandbox", sb["name"], sb["kind"])
        return jsonify(sb), 201

    @app.post("/api/admin/sandboxes/<sid>/refresh")
    @require_auth
    @require_admin
    def refresh_sandbox(sid):
        body = request.get_json(silent=True) or {}
        try:
            sb = devops.refresh_sandbox(
                store, sid, sanitize=bool(body.get("sanitize", True)))
        except KeyError:
            return jsonify({"error": "Not found"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("refresh", "sandbox", sb["name"])
        return jsonify(sb)

    @app.delete("/api/admin/sandboxes/<sid>")
    @require_auth
    @require_admin
    def delete_sandbox(sid):
        sb = store.config_get(devops.SANDBOX_TABLE, sid)
        if not devops.delete_sandbox(store, sid):
            return jsonify({"error": "Not found"}), 404
        _audit("delete", "sandbox", (sb or {}).get("name", sid))
        return jsonify({"deleted": True})

    @app.get("/api/admin/sandboxes/<sid>/login")
    @require_auth
    @require_admin
    def sandbox_login(sid):
        sb = store.config_get(devops.SANDBOX_TABLE, sid)
        if not sb:
            return jsonify({"error": "Not found"}), 404
        return jsonify({
            "name": sb["name"], "kind": sb["kind"], "db_path": sb["db_path"],
            "hint": "Serve this sandbox with its own server process, e.g.: "
                    f"FORCELET_DB={sb['db_path']} forcelet serve --port 8081",
        })

    # ------------------------------------------------------------ source tracking
    @app.get("/api/admin/source/changes")
    @require_auth
    @require_admin
    def source_changes():
        return jsonify(devops.source_changes(
            store, request.args.get("since"),
            limit=request.args.get("limit", 1000)))

    # ------------------------------------------------------------ custom metadata types
    @app.get("/api/admin/metadata-types")
    @require_auth
    @require_admin
    def list_cmdt():
        return jsonify(store.config_all(devops.CMDT_TABLE))

    @app.post("/api/admin/metadata-types")
    @require_auth
    @require_admin
    def create_cmdt():
        try:
            rec = devops.create_cmdt(store, request.mf_user, request.json or {})
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("create", "custom-metadata-type", rec["api_name"])
        return jsonify(rec), 201

    @app.get("/api/admin/metadata-types/<tid>")
    @require_auth
    @require_admin
    def get_cmdt(tid):
        rec = store.config_get(devops.CMDT_TABLE, tid)
        if not rec:
            return jsonify({"error": "Not found"}), 404
        rec = dict(rec)
        rec["records"] = [r for r in
                          store.config_all(devops.CMDT_RECORD_TABLE)
                          if r.get("type_id") == tid]
        return jsonify(rec)

    @app.put("/api/admin/metadata-types/<tid>")
    @require_auth
    @require_admin
    def update_cmdt(tid):
        try:
            rec = devops.update_cmdt(store, tid, request.json or {})
        except KeyError:
            return jsonify({"error": "Not found"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("update", "custom-metadata-type", rec["api_name"])
        return jsonify(rec)

    @app.delete("/api/admin/metadata-types/<tid>")
    @require_auth
    @require_admin
    def delete_cmdt(tid):
        rec = store.config_get(devops.CMDT_TABLE, tid)
        if not devops.delete_cmdt(store, tid):
            return jsonify({"error": "Not found"}), 404
        _audit("delete", "custom-metadata-type", (rec or {}).get("api_name", tid))
        return jsonify({"deleted": True})

    @app.post("/api/admin/metadata-types/<tid>/records")
    @require_auth
    @require_admin
    def create_cmdt_record(tid):
        body = request.json or {}
        try:
            rec = devops.create_cmdt_record(
                store, request.mf_user, tid,
                body.get("developer_name") or "", body.get("values") or {})
        except KeyError:
            return jsonify({"error": "Not found"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("create", "custom-metadata-record",
               f"{rec['type_api_name']}.{rec['developer_name']}")
        return jsonify(rec), 201

    @app.put("/api/admin/metadata-records/<rid>")
    @require_auth
    @require_admin
    def update_cmdt_record(rid):
        body = request.json or {}
        try:
            rec = devops.update_cmdt_record(
                store, rid, body.get("developer_name") or "",
                body.get("values") or {})
        except KeyError:
            return jsonify({"error": "Not found"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        return jsonify(rec)

    @app.delete("/api/admin/metadata-records/<rid>")
    @require_auth
    @require_admin
    def delete_cmdt_record(rid):
        if not store.config_delete(devops.CMDT_RECORD_TABLE, rid):
            return jsonify({"error": "Not found"}), 404
        return jsonify({"deleted": True})

    # ------------------------------------------------------------ custom settings
    @app.get("/api/admin/custom-settings")
    @require_auth
    @require_admin
    def list_custom_settings():
        return jsonify(store.config_all(devops.CUSTOM_SETTING_TABLE))

    @app.post("/api/admin/custom-settings")
    @require_auth
    @require_admin
    def upsert_custom_setting():
        try:
            rec, created = devops.upsert_custom_setting(store, request.mf_user,
                                                        request.json or {})
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("upsert", "custom-setting", rec["name"])
        return jsonify(rec), 201 if created else 200

    @app.delete("/api/admin/custom-settings/<sid>")
    @require_auth
    @require_admin
    def delete_custom_setting(sid):
        rec = store.config_get(devops.CUSTOM_SETTING_TABLE, sid)
        if not store.config_delete(devops.CUSTOM_SETTING_TABLE, sid):
            return jsonify({"error": "Not found"}), 404
        _audit("delete", "custom-setting", (rec or {}).get("name", sid))
        return jsonify({"deleted": True})

    # ------------------------------------------------------------ managed packages
    @app.get("/api/admin/packages/installed")
    @require_auth
    @require_admin
    def list_installed_packages():
        return jsonify(store.config_all(devops.INSTALLED_PACKAGE_TABLE))

    @app.delete("/api/admin/packages/installed/<ns>")
    @require_auth
    @require_admin
    def deregister_package(ns):
        existing = next((p for p in store.config_all(devops.INSTALLED_PACKAGE_TABLE)
                         if p.get("namespace", "").lower() == (ns or "").lower()), None)
        if not existing:
            return jsonify({"error": "Not found"}), 404
        store.config_delete(devops.INSTALLED_PACKAGE_TABLE, existing["id"])
        _audit("deregister", "package", ns,
               "install record removed; installed metadata was left in place")
        return jsonify({"deregistered": True})

    # ------------------------------------------------------------ external objects
    @app.get("/api/admin/external-objects")
    @require_auth
    @require_admin
    def list_external_objects():
        return jsonify(store.config_all(devops.EXTERNAL_OBJECT_TABLE))

    @app.post("/api/admin/external-objects")
    @require_auth
    @require_admin
    def upsert_external_object():
        try:
            rec, created = devops.upsert_external_object(store, request.mf_user,
                                                         request.json or {})
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("upsert", "external-object", rec["api_name"])
        return jsonify(rec), 201 if created else 200

    @app.delete("/api/admin/external-objects/<oid>")
    @require_auth
    @require_admin
    def delete_external_object(oid):
        rec = store.config_get(devops.EXTERNAL_OBJECT_TABLE, oid)
        if not store.config_delete(devops.EXTERNAL_OBJECT_TABLE, oid):
            return jsonify({"error": "Not found"}), 404
        _audit("delete", "external-object", (rec or {}).get("api_name", oid))
        return jsonify({"deleted": True})

    @app.get("/api/xdata/<api_name>")
    @require_auth
    def query_external_object(api_name):
        try:
            return jsonify(devops.query_external_object(
                store, api_name, dict(request.args)))
        except KeyError:
            return jsonify({"error": "Not found"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 502
