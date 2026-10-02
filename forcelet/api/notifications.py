"""In-app notifications. — Forcelet REST API domain module.

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
    from .. import queues as _queues_mod
    # ------------------------------------------------------------ notifications
    @app.get("/api/notifications")
    @require_auth
    def list_notifications():
        user = request.mf_user
        unread_only = request.args.get("unread_only") == "1"
        return jsonify(store.notifications_for(user["id"],
                                               unread_only=unread_only))

    @app.get("/api/notifications/unread-count")
    @require_auth
    def notification_unread_count():
        return jsonify({"count": store.notification_unread_count(
            request.mf_user["id"])})

    @app.post("/api/notifications/read")
    @require_auth
    def notifications_read():
        body = request.json or {}
        ids = None if body.get("all") else body.get("ids")
        store.notifications_mark_read(request.mf_user["id"], ids)
        return jsonify({"ok": True})

    # --------------------------------------------- custom notification types
    # Admin-managed notification builders: named title/body templates that
    # flows, approvals and API callers can send to users / roles / queues.
    @app.get("/api/notification-types")
    @require_auth
    def list_notification_types():
        return jsonify(store.config_all(automation.NOTIFICATION_TYPE_TABLE))

    @app.post("/api/notification-types")
    @require_auth
    @require_admin
    def create_notification_type():
        body = request.json or {}
        err = automation.validate_notification_type(body)
        if err:
            return jsonify({"error": err}), 400
        body.setdefault("active", True)
        nid = store.config_put(automation.NOTIFICATION_TYPE_TABLE, body)
        _audit("create", "NotificationType", nid)
        return jsonify(store.config_get(automation.NOTIFICATION_TYPE_TABLE, nid)), 201

    @app.get("/api/notification-types/<nid>")
    @require_auth
    def get_notification_type(nid):
        nt = automation.get_notification_type(store, nid)
        if not nt:
            return jsonify({"error": "Not found"}), 404
        return jsonify(nt)

    @app.put("/api/notification-types/<nid>")
    @require_auth
    @require_admin
    def update_notification_type(nid):
        nt = store.config_get(automation.NOTIFICATION_TYPE_TABLE, nid)
        if not nt:
            return jsonify({"error": "Not found"}), 404
        body = request.json or {}
        nt.update({k: v for k, v in body.items() if k != "id"})
        err = automation.validate_notification_type(nt)
        if err:
            return jsonify({"error": err}), 400
        store.config_put(automation.NOTIFICATION_TYPE_TABLE, nt)
        _audit("update", "NotificationType", nid)
        return jsonify(nt)

    @app.delete("/api/notification-types/<nid>")
    @require_auth
    @require_admin
    def delete_notification_type(nid):
        if not store.config_delete(automation.NOTIFICATION_TYPE_TABLE, nid):
            return jsonify({"error": "Not found"}), 404
        _audit("delete", "NotificationType", nid)
        return jsonify({"ok": True})

    @app.post("/api/notification-types/<nid>/send")
    @require_auth
    def send_notification_type(nid):
        """Send a notification type immediately.

        Body: {"title": <override?>, "body": <override?>,
               "recipients": {"users": [...], "roles": [...], "queues": [...],
                              "owner": bool, "submitter": bool},
               "object_name": ..., "record_id": ...}
        Non-admins may only target themselves via {"users": [own id]}.
        """
        nt = automation.get_notification_type(store, nid)
        if not nt:
            return jsonify({"error": "Not found"}), 404
        body = request.json or {}
        user = request.mf_user
        recipients = body.get("recipients") or {}
        if not security.is_admin(user):
            allowed = set(recipients.get("users") or [])
            if allowed - {user["id"]} or recipients.get("roles") \
                    or recipients.get("queues") or recipients.get("owner"):
                return jsonify({"error": "Only admins can notify other users"}), 403
        record = None
        if body.get("object_name") and body.get("record_id"):
            try:
                record = store.get(body["object_name"], body["record_id"])
            except Exception:
                record = None
        result = automation.send_custom_notification(store, security, {
            "notification_type": nid,
            "title": body.get("title"),
            "body": body.get("body"),
            "recipients": recipients,
            "object_name": body.get("object_name"),
            "record_id": body.get("record_id"),
        }, record, user)
        _audit("send", "NotificationType", nid)
        return jsonify(result)

    # ---------------------------------------------------------------- queues
    @app.get("/api/queues")
    @require_auth
    def list_user_queues():
        return jsonify(store.config_all(_queues_mod.QUEUE_TABLE))

    @app.post("/api/queues")
    @require_auth
    @require_admin
    def create_user_queue():
        body = request.json or {}
        err = _queues_mod.validate_queue(body)
        if err:
            return jsonify({"error": err}), 400
        qid = store.config_put(_queues_mod.QUEUE_TABLE, body)
        _audit("create", "Queue", qid)
        return jsonify(store.config_get(_queues_mod.QUEUE_TABLE, qid)), 201

    @app.get("/api/queues/<qid>")
    @require_auth
    def get_user_queue(qid):
        q = _queues_mod.get_queue(store, qid) or _queues_mod.find_queue(store, qid)
        if not q:
            return jsonify({"error": "Not found"}), 404
        return jsonify(q)

    @app.put("/api/queues/<qid>")
    @require_auth
    @require_admin
    def update_user_queue(qid):
        q = store.config_get(_queues_mod.QUEUE_TABLE, qid)
        if not q:
            return jsonify({"error": "Not found"}), 404
        body = request.json or {}
        q.update({k: v for k, v in body.items() if k != "id"})
        err = _queues_mod.validate_queue(q)
        if err:
            return jsonify({"error": err}), 400
        store.config_put(_queues_mod.QUEUE_TABLE, q)
        _audit("update", "Queue", qid)
        return jsonify(q)

    @app.delete("/api/queues/<qid>")
    @require_auth
    @require_admin
    def delete_user_queue(qid):
        if not store.config_delete(_queues_mod.QUEUE_TABLE, qid):
            return jsonify({"error": "Not found"}), 404
        _audit("delete", "Queue", qid)
        return jsonify({"ok": True})
