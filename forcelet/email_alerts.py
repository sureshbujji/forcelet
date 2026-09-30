"""Workflow email alerts — Forcelet platform module.

``EmailAlert`` records fire templated emails on record create/update when an
optional field-equals criteria matches. Templates are the existing
``mf_email_templates`` configs (see ``forcelet/bootstrap.py``); delivery
reuses the platform's email pipeline — ``store.log_email`` plus the
activity timeline entry — exactly like
``automation.send_templated_email`` does. (Actual SMTP relay only happens
when the app is pointed at one; in tests the send is observable through
``store.email_log()``.)

Templates support ``{{Field}}`` and ``{{Record.Field}}`` placeholders
resolved against the triggering record.

WIRE-UP (for the main agent — this file must NOT import or touch
``forcelet/api/flows.py`` or ``forcelet/automation.py``):

    After a record create/update (and after flow actions) completes, add::

        from forcelet.email_alerts import fire_email_alerts
        fire_email_alerts(store, obj_name, "Create", rec, user=user,
                          security=security)   # or "Update"

    ``store``/``security`` are available in the API modules
    (``app.mf_store`` / ``app.mf_security``); ``user`` is the acting user
    dict. The call is fire-and-forget safe: unknown objects, missing
    templates, and unresolvable recipients are skipped, never raised.
"""
from __future__ import annotations

import json
import re


def render_template(text: str, record: dict) -> str:
    """Merge ``{{Field}}`` / ``{{Record.Field}}`` placeholders."""
    if not text:
        return ""

    def repl(m):
        parts = m.group(1).strip().split(".")
        if parts and parts[0] == "Record":
            parts = parts[1:]
        val: object = record
        for part in parts:
            val = val.get(part) if isinstance(val, dict) else None
            if val is None:
                return ""
        return str(val)

    return re.sub(r"\{\{\s*([^}]+?)\s*\}\}", repl, text)


def criteria_matches(criteria, record: dict) -> bool:
    """Simple field-equals criteria: every entry must equal the record value.

    Accepts a dict or a JSON string of one. Blank criteria matches all.
    Malformed criteria never match (fail closed).
    """
    if not criteria:
        return True
    if isinstance(criteria, str):
        try:
            criteria = json.loads(criteria)
        except (ValueError, TypeError):
            return False
    if not isinstance(criteria, dict):
        return False
    return all(str(record.get(k)) == str(v) for k, v in criteria.items())


def _resolve_recipients(alert: dict, security) -> list:
    """Comma-separated user ids and/or raw email addresses -> addresses."""
    out = []
    for entry in str(alert.get("Recipients") or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "@" in entry:
            out.append(entry)
            continue
        if security is not None:
            try:
                user = security.get_user(entry)
            except Exception:
                user = None
            if user and user.get("email"):
                out.append(user["email"])
    return out


def fire_email_alerts(store, object_name: str, event: str, record: dict,
                      user: dict | None = None, security=None) -> list:
    """Fire active EmailAlerts for ``object_name``/``event``.

    ``event`` is ``"Create"`` or ``"Update"``. Returns a list of
    ``{"alert", "to", "subject"}`` for every email logged.
    """
    fired = []
    sender = user or {"id": "system", "name": "System"}
    try:
        alerts = store.query("EmailAlert", owner_ids=None, limit=10000)
    except Exception:
        return fired  # object not provisioned yet (fragment not merged)
    for alert in alerts:
        try:
            if not alert.get("IsActive", True):
                continue
            if (alert.get("ObjectName") or "") != object_name:
                continue
            if (alert.get("TriggerEvent") or "") != event:
                continue
            if not criteria_matches(alert.get("Criteria"), record):
                continue
            tpl = store.config_get("mf_email_templates",
                                   alert.get("EmailTemplateId") or "")
            if not tpl:
                continue
            subject = render_template(tpl.get("subject") or "", record)
            body = render_template(tpl.get("body") or "", record)
            for addr in _resolve_recipients(alert, security):
                store.log_email(object_name, record.get("id"), addr,
                                subject, body, tpl.get("name", ""), sender)
                store.add_activity(object_name, record.get("id"), "email",
                                   f"Email alert: {subject}", body, sender)
                fired.append({"alert": alert.get("Name"), "to": addr,
                              "subject": subject})
        except Exception:
            continue  # one bad alert must not break the save pipeline
    return fired
