"""AI lead scoring. — Forcelet REST API domain module.

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
    # ------------------------------------------------------------ lead scoring (ML)
    @app.post("/api/admin/ml/train-lead-scoring")
    @require_auth
    @require_admin
    def train_lead_scoring():
        from datetime import datetime, timezone
        from .. import ml as _ml
        leads = store.query("Lead", owner_ids=None, limit=10000)
        try:
            model = _ml.train_lead_scoring(leads)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        model["trained_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        model["trained_by"] = request.mf_user["id"]
        store.config_put("mf_ml_models", {"id": "lead_scoring", **model})
        _audit("train", "ml_model", "lead_scoring",
               f"{model['samples']} samples, accuracy {model['accuracy']}")
        return jsonify({k: v for k, v in model.items() if k != "weights"})

    @app.get("/api/sobjects/Lead/<rid>/score")
    @require_auth
    def lead_score(rid):
        from .. import ml as _ml
        user = request.mf_user
        rec = store.get("Lead", rid)
        if not rec or not security.can_see_record(user, rec, "Lead"):
            return jsonify({"error": "Not found"}), 404
        model = store.config_get("mf_ml_models", "lead_scoring")
        if not model:
            return jsonify({"error": "Scoring model has not been trained yet"}), 404
        out = _ml.score_lead(model, rec)
        out["trained_at"] = model.get("trained_at")
        out["model_accuracy"] = model.get("accuracy")
        return jsonify(out)
