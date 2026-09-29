"""Chatter feed. — Forcelet REST API domain module.

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
    # ------------------------------------------------------------ chatter feed
    @app.get("/api/feed")
    @require_auth
    def feed():
        user = request.mf_user
        obj_name = request.args.get("object")
        record_id = request.args.get("record_id")
        if obj_name and record_id:
            obj = registry.get_object(obj_name)
            rec = obj and store.get(obj_name, record_id)
            if not obj or not rec or not security.can(user, "read", obj_name) \
                    or not security.can_see_record(user, rec, obj_name):
                return jsonify({"error": "Not found"}), 404
            posts = store.feed_for_record(obj_name, record_id)
        elif obj_name or record_id:
            return jsonify({"error": "object and record_id are required together"}), 422
        else:
            posts = [p for p in store.feed_home(user["id"])
                     if not p.get("object_name")
                     or (store.get(p["object_name"], p["record_id"])
                         and security.can(user, "read", p["object_name"])
                         and security.can_see_record(
                             user, store.get(p["object_name"], p["record_id"]),
                             p["object_name"]))]
        for p in posts:
            p["liked_by_me"] = store.feed_liked_by(p["id"], user["id"])
        return jsonify(posts)

    @app.post("/api/feed")
    @require_auth
    def feed_post_ep():
        user = request.mf_user
        body = request.json or {}
        post, err = automation.post_to_feed(store, security, user,
                                            body.get("object"), body.get("record_id"),
                                            body.get("body", ""))
        if err:
            return jsonify({"error": err}), 422
        post["liked_by_me"] = False
        for u in automation.find_mentioned_users(store, body.get("body", "")):
            if u["id"] != user["id"]:
                store.notify(u["id"], "mention",
                             f"{user.get('name')} mentioned you",
                             (body.get("body", "") or "")[:140],
                             body.get("object"), body.get("record_id"))
        return jsonify(post), 201

    @app.get("/api/feed/<pid>/comments")
    @require_auth
    def feed_comments(pid):
        if not store.feed_get_post(pid):
            return jsonify({"error": "Not found"}), 404
        return jsonify(store.feed_comments(pid))

    @app.post("/api/feed/<pid>/comments")
    @require_auth
    def feed_add_comment(pid):
        user = request.mf_user
        if not store.feed_get_post(pid):
            return jsonify({"error": "Not found"}), 404
        body = ((request.json or {}).get("body") or "").strip()
        if not body:
            return jsonify({"error": "Comment body is required"}), 422
        cid = store.feed_add_comment(pid, user["id"], body)
        return jsonify({"id": cid}), 201

    @app.post("/api/feed/<pid>/like")
    @require_auth
    def feed_like(pid):
        if not store.feed_get_post(pid):
            return jsonify({"error": "Not found"}), 404
        store.feed_like(pid, request.mf_user["id"])
        return jsonify({"liked": True})

    @app.delete("/api/feed/<pid>/like")
    @require_auth
    def feed_unlike(pid):
        store.feed_unlike(pid, request.mf_user["id"])
        return jsonify({"liked": False})

    @app.post("/api/feed/follow")
    @require_auth
    def feed_follow():
        user = request.mf_user
        body = request.json or {}
        obj_name, record_id = body.get("object"), body.get("record_id")
        obj = registry.get_object(obj_name or "")
        rec = obj and store.get(obj_name, record_id)
        if not obj or not rec or not security.can(user, "read", obj_name) \
                or not security.can_see_record(user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        store.feed_follow(user["id"], obj_name, record_id)
        return jsonify({"following": True}), 201

    @app.delete("/api/feed/follow")
    @require_auth
    def feed_unfollow():
        user = request.mf_user
        ok = store.feed_unfollow(user["id"], request.args.get("object"),
                                 request.args.get("record_id"))
        return jsonify({"following": not ok})

    @app.get("/api/feed/following")
    @require_auth
    def feed_following():
        return jsonify(store.feed_follows_for(request.mf_user["id"]))
