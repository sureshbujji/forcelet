"""File attachments. — Forcelet REST API domain module.

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
    # ------------------------------------------------------------ files
    FILES_DIR = os.path.join(os.path.dirname(os.path.abspath(store.db_path)), "files")
    os.makedirs(FILES_DIR, exist_ok=True)
    MAX_FILE_BYTES = 10 * 1024 * 1024

    @app.post("/api/sobjects/<obj_name>/<rid>/files")
    @require_auth
    def upload_file(obj_name, rid):
        user = request.mf_user
        obj = registry.get_object(obj_name)
        rec = obj and store.get(obj_name, rid)
        if not obj or not rec or not security.can_see_record(user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "No file uploaded"}), 422
        data = f.read()
        if len(data) > MAX_FILE_BYTES:
            return jsonify({"error": "File is too large (10 MB max)"}), 422
        fid = store.file_put(obj_name, rid, secure_filename(f.filename),
                             f.mimetype, len(data), user)
        with open(os.path.join(FILES_DIR, fid), "wb") as fh:
            fh.write(data)
        return jsonify(store.file_get(fid)), 201

    @app.get("/api/sobjects/<obj_name>/<rid>/files")
    @require_auth
    def list_files(obj_name, rid):
        user = request.mf_user
        obj = registry.get_object(obj_name)
        rec = obj and store.get(obj_name, rid)
        if not obj or not rec or not security.can_see_record(user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        return jsonify(store.files_for_record(obj_name, rid))

    @app.get("/api/files/<fid>")
    @require_auth
    def download_file(fid):
        user = request.mf_user
        meta = store.file_get(fid)
        if not meta:
            return jsonify({"error": "Not found"}), 404
        rec = store.get(meta["object_name"], meta["record_id"])
        if not rec or not security.can_see_record(user, rec, meta["object_name"]):
            return jsonify({"error": "Not found"}), 404
        path = os.path.join(FILES_DIR, fid)
        if not os.path.exists(path):
            return jsonify({"error": "File content missing"}), 404
        return send_file(path, download_name=meta["filename"],
                         mimetype=meta["mime_type"])

    @app.delete("/api/files/<fid>")
    @require_auth
    def delete_file(fid):
        user = request.mf_user
        meta = store.file_get(fid)
        if not meta:
            return jsonify({"error": "Not found"}), 404
        rec = store.get(meta["object_name"], meta["record_id"])
        if not rec or not security.can_see_record(user, rec, meta["object_name"]):
            return jsonify({"error": "Not found"}), 404
        if meta["uploaded_by"] != user["id"] and not security.is_admin(user):
            return jsonify({"error": "Only the uploader or an admin can delete "
                                     "this file"}), 403
        store.file_delete(fid)
        try:
            os.remove(os.path.join(FILES_DIR, fid))
        except OSError:
            pass
        return jsonify({"deleted": True})
