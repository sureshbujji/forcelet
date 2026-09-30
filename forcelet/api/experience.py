"""Customer community portal (Experience Cloud). — Forcelet REST API domain module.

A separate, customer-facing surface: community users (backed by the
``CommunityUser`` standard object, linked to a Contact/Account) sign in with
portal-only session tokens (``mf_portal_…``) that are stored in their own
``mf_portal_sessions`` table.  The internal ``require_auth`` machinery only
resolves ``mf_sess_``/``mf_live_`` tokens from ``mf_sessions``, so a portal
token can never authenticate an internal ``/api/*`` endpoint.

Scoping rule: every authed portal endpoint is confined to the sign-in user's
own Account (via Contact → Account).  Cross-account access is impossible by
construction — account ids always come from the session, never the request.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, jsonify, request

from ..security import (
    SESSION_MAX_SECONDS, SESSION_TTL_SECONDS, hash_token,
    login_locked_out, record_failed_login, reset_login_attempts,
    verify_password,
)
from ._shared import _client_ip, ctx, rate_limit

log = logging.getLogger("forcelet.portal")

PORTAL_PREFIX = "mf_portal_"


def _new_portal_token() -> str:
    return PORTAL_PREFIX + secrets.token_urlsafe(32)


def _community_user_by_username(store, username: str):
    """Case-insensitive lookup, mirroring Security.get_user_by_username."""
    uname = (username or "").strip().lower()
    if not uname:
        return None
    for rec in store.query("CommunityUser", owner_ids=None, limit=10000):
        if (rec.get("Username") or "").lower() == uname:
            return rec
    return None


def _portal_session_context():
    """Resolve the Bearer portal token to the caller's community context.

    Returns {"community_user", "contact", "account_id"} or None.
    """
    store, registry, security = ctx()
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not token.startswith(PORTAL_PREFIX):
        return None
    thash = hash_token(token)
    sess = store.get_portal_session(thash)
    if not sess:
        return None
    now = datetime.now(timezone.utc)
    try:
        expires = datetime.fromisoformat(sess["expires_at"])
    except Exception:
        store.delete_portal_session(thash)
        return None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= now:
        store.delete_portal_session(thash)
        return None
    # Sliding expiry, mirroring the internal session model.
    try:
        created = datetime.fromisoformat(sess["created_at"])
    except Exception:
        created = now
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    new_exp = min(created + timedelta(seconds=SESSION_MAX_SECONDS),
                  now + timedelta(seconds=SESSION_TTL_SECONDS))
    store.touch_portal_session(thash, new_exp.isoformat(timespec="seconds"))

    cu = store.get("CommunityUser", sess["community_user_id"])
    if not cu or not cu.get("IsActive"):
        store.delete_portal_session(thash)
        return None
    contact = store.get("Contact", cu.get("ContactId"))
    if not contact:
        return None
    return {"community_user": cu, "contact": contact,
            "account_id": contact.get("AccountId")}


def require_portal_auth(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        pctx = _portal_session_context()
        if not pctx:
            return jsonify({"error": "Portal authentication required"}), 401
        request.mf_portal = pctx
        return fn(*a, **kw)
    return wrapper


def _case_out(rec: dict) -> dict:
    return {"Id": rec["id"], "Subject": rec.get("Subject"),
            "Description": rec.get("Description"),
            "Status": rec.get("Status"), "Priority": rec.get("Priority"),
            "Origin": rec.get("Origin"),
            "CreatedDate": rec.get("created_date")}


def _appt_out(rec: dict, wo_subjects: dict) -> dict:
    return {"Id": rec["id"], "Name": rec.get("Name"),
            "Status": rec.get("Status"),
            "ScheduledStart": rec.get("ScheduledStart"),
            "ScheduledEnd": rec.get("ScheduledEnd"),
            "Technician": rec.get("Technician"),
            "Address": rec.get("Address"),
            "ArrivalWindowStart": rec.get("ArrivalWindowStart"),
            "ArrivalWindowEnd": rec.get("ArrivalWindowEnd"),
            "WorkOrderId": rec.get("WorkOrderId"),
            "WorkOrderSubject": wo_subjects.get(rec.get("WorkOrderId"))}


def _account_work_orders(store, account_id: str) -> list:
    if not account_id:
        return []
    return [r for r in store.query("WorkOrder", owner_ids=None, limit=10000)
            if r.get("AccountId") == account_id]


def register(app: Flask):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security

    # ------------------------------------------------------------------ login
    @app.post("/api/portal/login")
    @rate_limit(max_requests=10, window_seconds=300,
                key_fn=lambda: "portal-login:" + _client_ip())
    def portal_login():
        body = request.json or {}
        username = (body.get("username") or "").strip()
        ip = _client_ip()
        lock_key = "portal:" + username.lower()
        if login_locked_out(ip, lock_key):
            log.warning("portal login locked out ip=%s username=%s", ip, username)
            return jsonify({"error": "Too many failed attempts. "
                                     "Try again in 15 minutes."}), 429
        cu = _community_user_by_username(store, username)
        if (not cu or not cu.get("IsActive")
                or not verify_password(body.get("password", ""),
                                       cu.get("PasswordHash") or "")):
            record_failed_login(ip, lock_key)
            log.warning("portal login failed ip=%s username=%s", ip, username)
            return jsonify({"error": "Invalid username or password"}), 401
        reset_login_attempts(ip, lock_key)
        token = _new_portal_token()
        now = datetime.now(timezone.utc)
        expires = min(now + timedelta(seconds=SESSION_MAX_SECONDS),
                      now + timedelta(seconds=SESSION_TTL_SECONDS))
        store.create_portal_session(
            hash_token(token), cu["id"], expires.isoformat(timespec="seconds"),
            ip=ip, user_agent=request.headers.get("User-Agent", ""))
        contact = store.get("Contact", cu.get("ContactId")) or {}
        cname = " ".join(p for p in (contact.get("FirstName"),
                                     contact.get("LastName")) if p) or username
        resp = jsonify({"token": token, "contact_name": cname,
                        "username": cu.get("Username")})
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Pragma"] = "no-cache"
        return resp

    @app.post("/api/portal/logout")
    @require_portal_auth
    def portal_logout():
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if token.startswith(PORTAL_PREFIX):
            store.delete_portal_session(hash_token(token))
        return jsonify({"ok": True})

    # ------------------------------------------------------- knowledge base
    @app.get("/api/portal/kb")
    def portal_kb_list():
        arts = [r for r in store.query("KnowledgeArticle", owner_ids=None,
                                       limit=500)
                if r.get("Status") == "Published"]
        arts.sort(key=lambda r: r.get("Title") or "")
        return jsonify([{"Id": r["id"], "Title": r.get("Title"),
                         "ArticleNumber": r.get("ArticleNumber"),
                         "Summary": r.get("Summary"),
                         "Category": r.get("Category")} for r in arts])

    @app.get("/api/portal/kb/<article_id>")
    def portal_kb_detail(article_id):
        rec = store.get("KnowledgeArticle", article_id)
        if not rec or rec.get("Status") != "Published":
            return jsonify({"error": "Article not found"}), 404
        try:
            store.update("KnowledgeArticle", article_id,
                         {"ViewCount": (rec.get("ViewCount") or 0) + 1})
        except Exception:
            pass
        return jsonify({"Id": rec["id"], "Title": rec.get("Title"),
                        "ArticleNumber": rec.get("ArticleNumber"),
                        "Summary": rec.get("Summary"), "Body": rec.get("Body"),
                        "Category": rec.get("Category"),
                        "ViewCount": (rec.get("ViewCount") or 0) + 1})

    # ----------------------------------------------------------------- cases
    @app.get("/api/portal/cases")
    @require_portal_auth
    def portal_cases():
        account_id = request.mf_portal["account_id"]
        if not account_id:
            return jsonify([])
        cases = [r for r in store.query("Case", owner_ids=None, limit=1000)
                 if r.get("AccountId") == account_id]
        cases.sort(key=lambda r: r.get("created_date") or "", reverse=True)
        return jsonify([_case_out(r) for r in cases])

    @app.post("/api/portal/cases")
    @require_portal_auth
    def portal_create_case():
        pctx = request.mf_portal
        if not pctx["account_id"]:
            return jsonify({"error": "No account linked to this portal user"}), 422
        body = request.json or {}
        subject = (body.get("subject") or "").strip()
        if not subject:
            return jsonify({"error": "subject is required"}), 422
        priority = (body.get("priority") or "Medium").strip()
        if priority not in ("Low", "Medium", "High", "Critical"):
            priority = "Medium"
        # AccountId/ContactId come from the session — any values in the
        # request body are ignored, so a caller cannot file against (or
        # spoof) another account.
        cid = store.insert("Case", {
            "Subject": subject,
            "Description": (body.get("description") or "").strip(),
            "Status": "New",
            "Priority": priority,
            "Origin": "Portal",
            "AccountId": pctx["account_id"],
            "ContactId": pctx["contact"]["id"],
        })
        return jsonify({"ok": True, "Id": cid}), 201

    # ---------------------------------------------------------- appointments
    @app.get("/api/portal/appointments")
    @require_portal_auth
    def portal_appointments():
        account_id = request.mf_portal["account_id"]
        wos = _account_work_orders(store, account_id)
        wo_ids = {w["id"] for w in wos}
        wo_subjects = {w["id"]: w.get("Subject") for w in wos}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        appts = [r for r in store.query("ServiceAppointment", owner_ids=None,
                                        limit=1000)
                 if r.get("WorkOrderId") in wo_ids
                 and (r.get("ScheduledStart") or "") >= now]
        appts.sort(key=lambda r: r.get("ScheduledStart") or "")
        return jsonify([_appt_out(r, wo_subjects) for r in appts])

    # --------------------------------------------------------------- reports
    @app.get("/api/portal/reports")
    @require_portal_auth
    def portal_reports():
        account_id = request.mf_portal["account_id"]
        wos = _account_work_orders(store, account_id)
        wo_ids = {w["id"] for w in wos}
        appts = {r["id"]: r for r in
                 store.query("ServiceAppointment", owner_ids=None, limit=2000)
                 if r.get("WorkOrderId") in wo_ids}
        reps = [r for r in store.query("ServiceReport", owner_ids=None,
                                       limit=2000)
                if r.get("ServiceAppointmentId") in appts]
        reps.sort(key=lambda r: r.get("SignedAt") or
                  r.get("created_date") or "", reverse=True)
        out = []
        for r in reps:
            appt = appts.get(r.get("ServiceAppointmentId")) or {}
            out.append({"Id": r["id"],
                        "ServiceAppointmentId": r.get("ServiceAppointmentId"),
                        "AppointmentName": appt.get("Name"),
                        "ScheduledStart": appt.get("ScheduledStart"),
                        "Summary": r.get("Summary"),
                        "SignatureName": r.get("SignatureName"),
                        "SignatureData": r.get("SignatureData"),
                        "SignedAt": r.get("SignedAt")})
        return jsonify(out)

    # ---------------------------------------------------------------- profile
    @app.get("/api/portal/profile")
    @require_portal_auth
    def portal_profile():
        c = request.mf_portal["contact"]
        acct = store.get("Account", c.get("AccountId")) or {}
        return jsonify({"Id": c["id"],
                        "FirstName": c.get("FirstName"),
                        "LastName": c.get("LastName"),
                        "Email": c.get("Email"), "Phone": c.get("Phone"),
                        "Title": c.get("Title"),
                        "AccountName": acct.get("Name")})

    @app.patch("/api/portal/profile")
    @require_portal_auth
    def portal_profile_update():
        c = request.mf_portal["contact"]
        body = request.json or {}
        fields = {}
        if "phone" in body:
            fields["Phone"] = (body.get("phone") or "").strip()
        if "email" in body:
            email = (body.get("email") or "").strip()
            if email and "@" not in email:
                return jsonify({"error": "email is not valid"}), 422
            fields["Email"] = email
        # Only phone/email are customer-editable; everything else in the
        # body (names, account, ids) is ignored.
        if fields:
            store.update("Contact", c["id"], fields)
        return jsonify({"ok": True})
