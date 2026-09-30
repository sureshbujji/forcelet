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


#: Built-in web-to-* form definitions. These seed mf_web_to_forms on
#: bootstrap; admins can edit the field lists per object in Setup afterwards.
#: Kept as a fallback so the public endpoints work even if the config rows
#: were deleted.
DEFAULT_WEB_TO_FORMS = {
    "web-to-lead": {
        "key": "web-to-lead", "name": "Web-to-Lead", "object": "Lead",
        "title": "Contact us",
        "fields": ["FirstName", "LastName", "Company", "Email", "Phone", "Rating"],
        "required": ["LastName", "Company"],
        "defaults": {"LeadSource": "Web"}, "active": True,
    },
    "web-to-case": {
        "key": "web-to-case", "name": "Web-to-Case", "object": "Case",
        "title": "Open a support case",
        "fields": ["FirstName", "LastName", "Email", "Subject", "Priority",
                   "Description"],
        "required": ["LastName", "Subject"],
        "defaults": {"Origin": "Web", "Priority": "Medium"}, "active": True,
    },
}


def get_web_to_form(store, key):
    """Return the active web-to-* form config for ``key``.

    Falls back to the built-in default when no config row exists, so the
    public endpoints keep working on databases seeded before B2.
    """
    try:
        rows = store.config_all("mf_web_to_forms")
    except Exception:
        rows = []
    for r in rows:
        if r.get("key") == key and r.get("active", True):
            return r
    return dict(DEFAULT_WEB_TO_FORMS.get(key) or {})


def register(app: Flask):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security

    def _form_html(key, form):
        fields = "".join(
            f'<label>{f}</label>'
            f'<input name="{f}"{" required" if f in (form.get("required") or []) else ""}>'
            for f in form.get("fields") or [])
        ksug = ""
        if key == "web-to-case" and "Subject" in (form.get("fields") or []) \
                and "Description" in (form.get("fields") or []):
            ksug = (
                "<div class='ksug' id='ksug'><b>These help articles might answer your "
                "question:</b><div id='ksug_list'></div></div>"
                "<script>var t=null;function esc(s){return String(s||'').replace(/[&<>\"']/g,"
                "function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]});}"
                "function sug(){var se=document.getElementsByName('Subject')[0],de=document.getElementsByName('Description')[0];"
                "if(!se||!de)return;var q=(se.value+' '+de.value).trim();"
                "if(q.length<4){document.getElementById('ksug').style.display='none';return;}"
                "fetch('/api/public/knowledge-suggest?q='+encodeURIComponent(q)).then(function(r){return r.json()}).then(function(j){"
                "var arr=j.data||j,box=document.getElementById('ksug'),list=document.getElementById('ksug_list');"
                "if(!arr||!arr.length){box.style.display='none';return;}"
                "list.innerHTML=arr.map(function(a){return '<b>'+esc(a.title)+'</b><p>'+esc(a.summary)+'</p>'}).join('');"
                "box.style.display='block';});}"
                "['Subject','Description'].forEach(function(n){var el=document.getElementsByName(n)[0];"
                "if(el)el.addEventListener('input',function(){clearTimeout(t);t=setTimeout(sug,600);});});"
                "</script>")
        return Response(
            '<!doctype html><html><head><meta charset="utf-8"><title>' + form.get("title", key) + '</title>'
            "<style>body{font-family:sans-serif;max-width:480px;margin:40px auto;padding:0 16px}"
            "label{display:block;margin:12px 0 4px;font-weight:600}"
            "input{width:100%;padding:8px;box-sizing:border-box}"
            "button{margin-top:16px;padding:10px 24px}"
            ".ksug{background:#f5f9ff;border:1px solid #cfe0f7;border-radius:8px;padding:10px 12px;margin:12px 0;display:none}"
            ".ksug a{display:block;font-weight:600;color:#0176d3;margin:6px 0 2px}"
            ".ksug p{margin:2px 0 8px;color:#444;font-size:14px}</style></head><body>"
            f"<h2>{form.get('title', key)}</h2><form method='post' action='/api/public/{key}'>"
            f"{fields}<button type='submit'>Submit</button></form>"
            f"{ksug}</body></html>",
            mimetype="text/html")

    def _form_submit(key, form):
        data = request.get_json(silent=True) or request.form.to_dict() or {}
        fields = form.get("fields") or []
        values = {f: data.get(f) for f in fields if data.get(f) not in (None, "")}
        for dk, dv in (form.get("defaults") or {}).items():
            values.setdefault(dk, dv)
        # Public submissions flow through the normal pipeline as the admin
        # user: validation, duplicate bypass, assignment rules, flows,
        # auto-response rules, and approval entry all apply.
        admin = security.get_user_by_username("admin")
        status, payload = _do_create(admin, form.get("object") or "Lead", values,
                                     allow_duplicates=True)
        if status == 201:
            if request.form:
                return Response(f"<p>Thanks — your {form.get('name', 'submission')} "
                                "was received. We'll be in touch shortly.</p>",
                                mimetype="text/html")
            return jsonify({"created": True, "id": payload["Id"]}), 201
        return jsonify(payload), status

    # ------------------------------------------------------------ web-to-lead (public)
    @app.get("/api/public/web-to-lead")
    def web_to_lead_form():
        return _form_html("web-to-lead", get_web_to_form(store, "web-to-lead"))

    @app.post("/api/public/web-to-lead")
    def web_to_lead_submit():
        return _form_submit("web-to-lead", get_web_to_form(store, "web-to-lead"))

    # ------------------------------------------------------------ web-to-case (public)
    @app.get("/api/public/web-to-case")
    def web_to_case_form():
        return _form_html("web-to-case", get_web_to_form(store, "web-to-case"))

    @app.post("/api/public/web-to-case")
    def web_to_case_submit():
        return _form_submit("web-to-case", get_web_to_form(store, "web-to-case"))

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
