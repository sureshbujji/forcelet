"""Web-to-Lead and Web-to-Case (public). — Forcelet REST API domain module.

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
    # ------------------------------------------------------------ web-to-lead (public)
    W2L_FIELDS = ["FirstName", "LastName", "Company", "Email", "Phone", "Rating"]

    @app.get("/api/public/web-to-lead")
    def web_to_lead_form():
        fields = "".join(
            f'<label>{f}</label>'
            f'<input name="{f}"{" required" if f in ("LastName", "Company") else ""}>'
            for f in W2L_FIELDS)
        return Response(
            '<!doctype html><html><head><meta charset="utf-8"><title>Contact us</title>'
            "<style>body{font-family:sans-serif;max-width:480px;margin:40px auto;padding:0 16px}"
            "label{display:block;margin:12px 0 4px;font-weight:600}"
            "input{width:100%;padding:8px;box-sizing:border-box}"
            "button{margin-top:16px;padding:10px 24px}</style></head><body>"
            f"<h2>Contact us</h2><form method='post' action='/api/public/web-to-lead'>"
            f"{fields}<button type='submit'>Submit</button></form></body></html>",
            mimetype="text/html")

    @app.post("/api/public/web-to-lead")
    def web_to_lead_submit():
        data = request.get_json(silent=True) or request.form.to_dict() or {}
        values = {f: data.get(f) for f in W2L_FIELDS if data.get(f) not in (None, "")}
        values["LeadSource"] = "Web"
        # Public submissions flow through the normal pipeline as the admin
        # user: validation, duplicate bypass, assignment rules, flows,
        # auto-response rules, and approval entry all apply.
        admin = security.get_user_by_username("admin")
        status, payload = _do_create(admin, "Lead", values, allow_duplicates=True)
        if status == 201:
            if request.form:
                return Response("<p>Thanks — we got your details and will be in touch.</p>",
                                mimetype="text/html")
            return jsonify({"created": True, "id": payload["Id"]}), 201
        return jsonify(payload), status

    # ------------------------------------------------------------ web-to-case (public)
    W2C_FIELDS = ["FirstName", "LastName", "Email", "Subject", "Priority",
                  "Description"]

    @app.get("/api/public/web-to-case")
    def web_to_case_form():
        fields = "".join(
            f'<label>{f}</label>'
            f'<input name="{f}"{" required" if f in ("LastName", "Subject") else ""}>'
            for f in W2C_FIELDS)
        return Response(
            '<!doctype html><html><head><meta charset="utf-8"><title>Open a support case</title>'
            "<style>body{font-family:sans-serif;max-width:480px;margin:40px auto;padding:0 16px}"
            "label{display:block;margin:12px 0 4px;font-weight:600}"
            "input{width:100%;padding:8px;box-sizing:border-box}"
            "button{margin-top:16px;padding:10px 24px}</style></head><body>"
            f"<h2>Open a support case</h2><form method='post' action='/api/public/web-to-case'>"
            f"{fields}<button type='submit'>Submit</button></form></body></html>",
            mimetype="text/html")

    @app.post("/api/public/web-to-case")
    def web_to_case_submit():
        data = request.get_json(silent=True) or request.form.to_dict() or {}
        values = {f: data.get(f) for f in W2C_FIELDS if data.get(f) not in (None, "")}
        values["Origin"] = "Web"
        values.setdefault("Priority", "Medium")
        # public submissions run through the normal pipeline as the admin user
        admin = security.get_user_by_username("admin")
        status, payload = _do_create(admin, "Case", values, allow_duplicates=True)
        if status == 201:
            if request.form:
                return Response("<p>Thanks — your support case was created. "
                                "We'll be in touch shortly.</p>",
                                mimetype="text/html")
            return jsonify({"created": True, "id": payload["Id"]}), 201
        return jsonify(payload), status

    @app.post("/api/public/email-to-case")
    def email_to_case():
        """Inbound-email webhook: point an email service's inbound-parse
        webhook here. JSON body: {from_name, from_email, subject, body}."""
        data = request.get_json(silent=True) or {}
        subject = (data.get("subject") or "(no subject)")[:120]
        sender = f"{data.get('from_name') or ''} <{data.get('from_email') or ''}>".strip()
        desc = (f"From: {sender}\n\n" if sender.strip("<> ") else "") + (data.get("body") or "")
        values = {"Subject": subject, "Description": desc,
                  "Origin": "Email", "Priority": "Medium"}
        admin = security.get_user_by_username("admin")
        status, payload = _do_create(admin, "Case", values, allow_duplicates=True)
        if status == 201:
            return jsonify({"created": True, "id": payload["Id"]}), 201
        return jsonify(payload), status
