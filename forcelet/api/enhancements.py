"""Functionality enhancements: calendar data, quote PDFs, case queues + macros,
bulk data jobs, report subscriptions, flow versioning, TOTP two-factor auth,
knowledge suggestions for Web-to-Case.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import threading
import time
import uuid

from flask import Flask, jsonify, request, Response

from .. import automation
from .. import totp_util
from ..pdfgen import build_pdf
from ..expressions import eval_expr, record_context
from ._shared import (
    DEFAULT_MEMBER_STATUSES, _audit, _do_update, _visible_records,
    issue_session, require_admin, require_auth, serialize,
)

# in-memory TOTP login challenges: challenge_id -> {user_id, expires}
_TOTP_CHALLENGES: dict = {}
_TOTP_LOCK = threading.Lock()
# in-memory pending TOTP setup secrets: user_id -> {secret, expires}
_TOTP_SETUP: dict = {}


def _queue_table(store):
    return "mf_case_queues"


def register(app: Flask):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security

    # ------------------------------------------------------------ user names
    # Minimal id -> display-name map so the UI can show owner names instead
    # of raw user ids (e.g. in the record System card). Authenticated users
    # only; never exposes password hashes.
    @app.get("/api/users/names")
    @require_auth
    def user_names():
        return jsonify({u["id"]: (u.get("name") or u.get("username") or u["id"])
                        for u in security.list_users()})

    # --------------------------------------------------- calendar sources (A2)
    # Built-in calendar sources: which object + date field each feeds. Admins
    # may override the field/label/color per object, or enable calendar
    # rendering for any other object, via PUT /api/admin/objects/<obj>.
    _CALENDAR_DEFAULTS = {
        "Event": {"start": "StartDateTime", "color": "#0176d3",
                  "label_field": "Subject"},
        "Task": {"start": "DueDate", "color": "#2e9e4f",
                 "label_field": "Subject"},
        "ServiceAppointment": {"start": "ScheduledStart", "color": "#d98a1f",
                               "label_field": "Subject"},
    }

    @app.get("/api/calendar-sources")
    @require_auth
    def calendar_sources():
        user = request.mf_user

        def _source(obj, dflt):
            raw = obj.get("calendar_date_field")
            if raw == "":
                return None  # explicitly removed from the calendar
            start = raw or dflt["start"]
            fmap = {f["name"]: f for f in obj.get("fields", [])}
            f = fmap.get(start)
            if not f or f.get("type") not in ("Date", "DateTime"):
                return None
            return {"obj": obj["name"], "start": start,
                    "color": obj.get("calendar_color") or dflt["color"],
                    "label_field": (obj.get("calendar_label_field")
                                    or dflt["label_field"])}

        out, seen = [], set()
        for name, dflt in _CALENDAR_DEFAULTS.items():
            obj = registry.get_object(name)
            if not obj or not security.can(user, "read", name):
                continue
            src = _source(obj, dflt)
            if src:
                out.append(src)
                seen.add(name)
        for obj in registry.list_objects():
            name = obj["name"]
            if name in seen or not obj.get("calendar_date_field"):
                continue
            if not security.can(user, "read", name):
                continue
            src = _source(obj, {"start": None, "color": "#6b5ce7",
                                "label_field": "Name"})
            if src:
                out.append(src)
        return jsonify(out)

    # ------------------------------------------------------------ case queues
    @app.get("/api/case-queues")
    @require_auth
    def list_queues():
        return jsonify(store.config_all(_queue_table(store)))

    @app.post("/api/case-queues")
    @require_auth
    @require_admin
    def create_queue():
        body = request.json or {}
        if not body.get("name"):
            return jsonify({"error": "name is required"}), 422
        qobj = body.get("object") or "Case"
        if not registry.get_object(qobj):
            return jsonify({"error": f"Unknown object '{qobj}'"}), 422
        q = {"name": body["name"], "object": qobj,
             "filters": body.get("filters") or {},
             "active": body.get("active", True)}
        rid = store.config_put(_queue_table(store), q)
        _audit("create", "case-queue", q["name"])
        return jsonify(store.config_get(_queue_table(store), rid)), 201

    @app.put("/api/case-queues/<qid>")
    @require_auth
    @require_admin
    def update_queue(qid):
        q = store.config_get(_queue_table(store), qid)
        if not q:
            return jsonify({"error": "Not found"}), 404
        body = request.json or {}
        body.pop("id", None)
        if "object" in body:
            qobj = body.get("object") or "Case"
            if not registry.get_object(qobj):
                return jsonify({"error": f"Unknown object '{qobj}'"}), 422
            body["object"] = qobj
        store.config_put(_queue_table(store), {**q, **body, "id": qid})
        _audit("update", "case-queue", q.get("name") or qid)
        return jsonify(store.config_get(_queue_table(store), qid))

    @app.delete("/api/case-queues/<qid>")
    @require_auth
    @require_admin
    def delete_queue(qid):
        if not store.config_delete(_queue_table(store), qid):
            return jsonify({"error": "Not found"}), 404
        _audit("delete", "case-queue", qid)
        return jsonify({"deleted": True})

    @app.get("/api/case-queues/<qid>/cases")
    @require_auth
    def queue_cases(qid):
        user = request.mf_user
        q = store.config_get(_queue_table(store), qid)
        if not q:
            return jsonify({"error": "Not found"}), 404
        qobj = q.get("object") or "Case"
        obj = registry.get_object(qobj)
        if not obj:
            return jsonify({"error": f"Unknown object '{qobj}'"}), 422
        records, _ = _visible_records(user, qobj)
        filt = q.get("filters") or {}
        rows = []
        for r in records:
            try:
                if filt and not eval_expr(filt, record_context(r), user=user):
                    continue
            except Exception:
                continue
            rows.append(serialize(user, obj, r))
        return jsonify(rows[:200])

    # ------------------------------------------------------------ agent macros
    @app.get("/api/macros")
    @require_auth
    def list_macros():
        return jsonify(store.config_all("mf_macros"))

    @app.post("/api/macros")
    @require_auth
    @require_admin
    def create_macro():
        body = request.json or {}
        if not body.get("name") or not isinstance(body.get("actions"), list):
            return jsonify({"error": "name and actions[] are required"}), 422
        mobj = body.get("object") or "Case"
        if not registry.get_object(mobj):
            return jsonify(
                {"error": f"Unknown object '{mobj}'"}), 422
        m = {"name": body["name"], "object": mobj,
             "actions": body["actions"], "active": body.get("active", True)}
        rid = store.config_put("mf_macros", m)
        _audit("create", "macro", m["name"])
        return jsonify(store.config_get("mf_macros", rid)), 201

    @app.put("/api/macros/<mid>")
    @require_auth
    @require_admin
    def update_macro(mid):
        m = store.config_get("mf_macros", mid)
        if not m:
            return jsonify({"error": "Not found"}), 404
        body = request.json or {}
        body.pop("id", None)
        if "object" in body:
            mobj = body.get("object") or "Case"
            if not registry.get_object(mobj):
                return jsonify(
                    {"error": f"Unknown object '{mobj}'"}), 422
            body["object"] = mobj
        store.config_put("mf_macros", {**m, **body, "id": mid})
        _audit("update", "macro", m.get("name") or mid)
        return jsonify(store.config_get("mf_macros", mid))

    @app.delete("/api/macros/<mid>")
    @require_auth
    @require_admin
    def delete_macro(mid):
        if not store.config_delete("mf_macros", mid):
            return jsonify({"error": "Not found"}), 404
        _audit("delete", "macro", mid)
        return jsonify({"deleted": True})

    @app.post("/api/sobjects/<obj_name>/<rid>/apply-macro")
    @require_auth
    def apply_macro(obj_name, rid):
        """Run a macro's actions against one record.

        Actions: {"set_fields": {...}}, {"add_comment": "text"},
        {"reassign": "<user_id>"}.
        """
        user = request.mf_user
        body = request.json or {}
        m = store.config_get("mf_macros", body.get("macro_id") or "")
        if not m or not m.get("active", True):
            return jsonify({"error": "Unknown or inactive macro"}), 404
        if m.get("object") not in (None, obj_name):
            return jsonify({"error": "Macro does not apply to " + obj_name}), 422
        applied = []
        for act in m.get("actions") or []:
            if "set_fields" in act and isinstance(act["set_fields"], dict):
                status, payload = _do_update(user, obj_name, rid, act["set_fields"])
                if status >= 400:
                    return jsonify({"error": "set_fields failed",
                                    "details": payload}), status
                applied.append("set_fields")
            elif "add_comment" in act:
                rec = store.get(obj_name, rid)
                if not rec:
                    return jsonify({"error": "Not found"}), 404
                post, err = automation.post_to_feed(
                    store, security, user, obj_name, rid, str(act["add_comment"]))
                if err:
                    return jsonify({"error": err}), 422
                applied.append("add_comment")
            elif "reassign" in act:
                target = security.get_user(act["reassign"])
                if not target:
                    return jsonify({"error": "Unknown user"}), 422
                # owner_id is a system column, not an editable field
                store.update(obj_name, rid, {"owner_id": target["id"]})
                applied.append("reassign")
        _audit("apply-macro", obj_name, f"{m.get('name')} on {rid}")
        return jsonify({"applied": applied})

    # ------------------------------------------------------------ bulk data jobs
    def _run_bulk_job(job_id: str):
        """Background worker: process rows with its own DB connection."""
        from ..bootstrap import bootstrap
        bstore, bregistry, bsecurity = bootstrap(store.db_path)
        job = bstore.config_get("mf_bulk_jobs", job_id)
        if not job:
            return
        admin = bsecurity.get_user_by_username("admin")
        query, create, update = automation._trigger_dml_ops(
            bstore, bregistry, bsecurity, admin, 0, [])
        obj_name, op = job["object"], job["operation"]
        ext_field = job.get("external_id_field")
        ok, failed, errors = 0, 0, []
        job["status"] = "running"
        bstore.config_put("mf_bulk_jobs", job)
        for i, row in enumerate(job.get("rows") or []):
            try:
                if op == "insert":
                    create(obj_name, dict(row))
                elif op == "update":
                    rid = row.get("Id") or row.get("id")
                    if not rid:
                        raise ValueError("row is missing Id")
                    update(obj_name, rid, {k: v for k, v in row.items()
                                           if k not in ("Id", "id")})
                elif op == "upsert":
                    if not ext_field:
                        raise ValueError("external_id_field is required for upsert")
                    val = row.get(ext_field)
                    match = next((r for r in query(obj_name)
                                  if str(r.get(ext_field)) == str(val)), None)
                    if match:
                        update(obj_name, match["id"],
                               {k: v for k, v in row.items() if k != ext_field})
                    else:
                        create(obj_name, dict(row))
                else:
                    raise ValueError(f"unknown operation '{op}'")
                ok += 1
            except Exception as e:  # noqa: BLE001 - per-row errors are collected
                failed += 1
                if len(errors) < 25:
                    errors.append(f"row {i + 1}: {type(e).__name__}: {e}")
            if i % 50 == 0:
                job["processed"], job["succeeded"], job["failed"] = i + 1, ok, failed
                bstore.config_put("mf_bulk_jobs", job)
        job.update({"status": "completed", "processed": ok + failed,
                    "succeeded": ok, "failed": failed, "errors": errors})
        bstore.config_put("mf_bulk_jobs", job)

    @app.get("/api/bulk-jobs")
    @require_auth
    @require_admin
    def list_bulk_jobs():
        jobs = store.config_all("mf_bulk_jobs")
        return jsonify([{**j, "rows": f"{len(j.get('rows') or [])} rows"}
                        for j in jobs])

    @app.get("/api/bulk-jobs/<jid>")
    @require_auth
    @require_admin
    def get_bulk_job(jid):
        j = store.config_get("mf_bulk_jobs", jid)
        if not j:
            return jsonify({"error": "Not found"}), 404
        return jsonify(j)

    @app.post("/api/bulk-jobs")
    @require_auth
    @require_admin
    def create_bulk_job():
        body = request.json or {}
        obj_name = body.get("object")
        op = body.get("operation") or "insert"
        rows = body.get("rows")
        if not obj_name or not registry.get_object(obj_name):
            return jsonify({"error": "Unknown object"}), 422
        if op not in ("insert", "update", "upsert"):
            return jsonify({"error": "operation must be insert/update/upsert"}), 422
        if not isinstance(rows, list) or not rows:
            return jsonify({"error": "rows[] is required"}), 422
        if op == "upsert" and not body.get("external_id_field"):
            return jsonify({"error": "external_id_field is required for upsert"}), 422
        job = {"name": body.get("name") or f"Bulk {op} {obj_name}",
               "object": obj_name, "operation": op, "rows": rows,
               "external_id_field": body.get("external_id_field"),
               "status": "queued", "processed": 0, "succeeded": 0, "failed": 0,
               "errors": [], "created_by": request.mf_user["id"]}
        jid = store.config_put("mf_bulk_jobs", job)
        _audit("create", "bulk-job", job["name"])
        t = threading.Thread(target=_run_bulk_job, args=(jid,), daemon=True)
        t.start()
        return jsonify(store.config_get("mf_bulk_jobs", jid)), 201

    # ------------------------------------------------------------ report subscriptions
    @app.get("/api/report-subscriptions")
    @require_auth
    def list_subs():
        if request.mf_user["profile"] == "System Administrator":
            return jsonify(store.config_all("mf_report_subs"))
        mine = [s for s in store.config_all("mf_report_subs")
                if s.get("created_by") == request.mf_user["id"]]
        return jsonify(mine)

    def _sync_sub_job(sub: dict) -> dict:
        """(Re)create the scheduled job that fires this subscription."""
        if sub.get("job_id"):
            store.config_delete("mf_scheduled_jobs", sub["job_id"])
        freq = {"daily": 1440, "weekly": 10080}.get(sub.get("frequency"), 1440)
        job = {"name": f"Report digest: {sub.get('name')}",
               "interval_minutes": freq, "active": sub.get("active", True),
               "run_as": "admin",
               "code": f"automation.send_report_digest(store, security, '{sub['id']}')"}
        jid = store.config_put("mf_scheduled_jobs", job)
        sub["job_id"] = jid
        return sub

    @app.post("/api/report-subscriptions")
    @require_auth
    def create_sub():
        body = request.json or {}
        rep = store.config_get("mf_reports", body.get("report_id") or "")
        dash = store.config_get("mf_dashboards", body.get("dashboard_id") or "")
        if not rep and not dash:
            return jsonify({"error": "report_id or dashboard_id is required"}), 422
        recipients = [r for r in (body.get("recipients") or []) if r]
        if not recipients:
            return jsonify({"error": "at least one recipient email is required"}), 422
        sub = {"name": body.get("name") or (rep or dash).get("name"),
               "report_id": body.get("report_id"),
               "dashboard_id": body.get("dashboard_id"),
               "frequency": body.get("frequency") or "daily",
               "recipients": recipients, "active": body.get("active", True),
               "created_by": request.mf_user["id"]}
        sid = store.config_put("mf_report_subs", sub)
        sub = store.config_get("mf_report_subs", sid)
        sub = _sync_sub_job(sub)
        store.config_put("mf_report_subs", sub)
        _audit("create", "report-subscription", sub["name"])
        return jsonify(store.config_get("mf_report_subs", sid)), 201

    @app.put("/api/report-subscriptions/<sid>")
    @require_auth
    def update_sub(sid):
        sub = store.config_get("mf_report_subs", sid)
        if not sub:
            return jsonify({"error": "Not found"}), 404
        if request.mf_user["profile"] != "System Administrator" \
                and sub.get("created_by") != request.mf_user["id"]:
            return jsonify({"error": "Forbidden"}), 403
        body = request.json or {}
        body.pop("id", None)
        body.pop("job_id", None)
        sub = {**sub, **body}
        sub = _sync_sub_job(sub)
        store.config_put("mf_report_subs", sub)
        return jsonify(store.config_get("mf_report_subs", sid))

    @app.delete("/api/report-subscriptions/<sid>")
    @require_auth
    def delete_sub(sid):
        sub = store.config_get("mf_report_subs", sid)
        if not sub:
            return jsonify({"error": "Not found"}), 404
        if request.mf_user["profile"] != "System Administrator" \
                and sub.get("created_by") != request.mf_user["id"]:
            return jsonify({"error": "Forbidden"}), 403
        if sub.get("job_id"):
            store.config_delete("mf_scheduled_jobs", sub["job_id"])
        store.config_delete("mf_report_subs", sid)
        return jsonify({"deleted": True})

    # ------------------------------------------------------------ flow versioning
    @app.get("/api/admin/flows/<fid>/versions")
    @require_auth
    @require_admin
    def flow_versions(fid):
        if not store.config_get("mf_flows", fid):
            return jsonify({"error": "Not found"}), 404
        vers = [v for v in store.config_all("mf_flow_versions")
                if v.get("flow_id") == fid]
        vers.sort(key=lambda v: v.get("version", 0), reverse=True)
        return jsonify([{**v, "definition": f"{len(json_dumps(v.get('definition') or {}))} bytes"}
                        for v in vers])

    @app.get("/api/admin/flows/<fid>/versions/<vid>")
    @require_auth
    @require_admin
    def flow_version_get(fid, vid):
        v = store.config_get("mf_flow_versions", vid)
        if not v or v.get("flow_id") != fid:
            return jsonify({"error": "Not found"}), 404
        return jsonify(v)

    @app.post("/api/admin/flows/<fid>/rollback")
    @require_auth
    @require_admin
    def flow_rollback(fid):
        body = request.json or {}
        v = store.config_get("mf_flow_versions", body.get("version_id") or "")
        if not v or v.get("flow_id") != fid:
            return jsonify({"error": "Unknown version"}), 404
        cur = store.config_get("mf_flows", fid)
        if not cur:
            return jsonify({"error": "Flow not found"}), 404
        _snapshot_flow_version(store, fid, cur, request.mf_user,
                               note="pre-rollback snapshot")
        restored = dict(v["definition"])
        restored["id"] = fid
        store.config_put("mf_flows", restored)
        _audit("rollback", "flow", cur.get("name") or fid)
        return jsonify(store.config_get("mf_flows", fid))

    # ------------------------------------------------------------ TOTP two-factor
    @app.get("/api/me/totp/status")
    @require_auth
    def totp_status():
        return jsonify({"enabled": bool(request.mf_user.get("totp_secret"))})

    @app.post("/api/me/totp/setup")
    @require_auth
    def totp_setup():
        user = request.mf_user
        if user.get("totp_secret"):
            return jsonify({"error": "Two-factor is already enabled"}), 422
        secret = totp_util.new_secret()
        with _TOTP_LOCK:
            _TOTP_SETUP[user["id"]] = {"secret": secret,
                                       "expires": time.time() + 600}
        return jsonify({"secret": secret,
                        "otpauth_url": totp_util.otpauth_url(
                            secret, user.get("username") or user["id"])})

    @app.post("/api/me/totp/enable")
    @require_auth
    def totp_enable():
        user = request.mf_user
        with _TOTP_LOCK:
            pending = _TOTP_SETUP.get(user["id"])
        if not pending or pending["expires"] < time.time():
            return jsonify({"error": "No pending setup — start setup again"}), 422
        if not totp_util.verify(pending["secret"], (request.json or {}).get("code")):
            return jsonify({"error": "Invalid code"}), 422
        fresh = security.get_user(user["id"])
        fresh["totp_secret"] = pending["secret"]
        store.meta_put("mf_users", user["id"], fresh)
        with _TOTP_LOCK:
            _TOTP_SETUP.pop(user["id"], None)
        # 2FA enrollment completes onboarding: if this limited session was
        # gated only on pending TOTP enrollment, trust it fully now.
        sess = getattr(request, "mf_session", None)
        if sess and sess.get("limited") and not fresh.get("must_change_password"):
            store.unlimit_session(sess["token_hash"])
        _audit("enable", "totp", user.get("username") or user["id"])
        return jsonify({"enabled": True})

    @app.post("/api/me/totp/disable")
    @require_auth
    def totp_disable():
        user = request.mf_user
        if not security.check_password(user, (request.json or {}).get("password", "")):
            return jsonify({"error": "Password is incorrect"}), 403
        fresh = security.get_user(user["id"])
        fresh.pop("totp_secret", None)
        store.meta_put("mf_users", user["id"], fresh)
        _audit("disable", "totp", user.get("username") or user["id"])
        return jsonify({"enabled": False})

    # hook into the login flow: issue a challenge instead of a token
    _orig_login_view = app.view_functions.get("login")

    @app.post("/api/login/totp")
    def login_totp():
        body = request.json or {}
        with _TOTP_LOCK:
            chal = _TOTP_CHALLENGES.get(body.get("challenge") or "")
        if not chal or chal["expires"] < time.time():
            return jsonify({"error": "Challenge expired — log in again"}), 401
        user = security.get_user(chal["user_id"])
        if not user or not user.get("totp_secret"):
            return jsonify({"error": "Two-factor is not enabled"}), 401
        if not totp_util.verify(user["totp_secret"], body.get("code")):
            return jsonify({"error": "Invalid code"}), 401
        with _TOTP_LOCK:  # single-use: consume only after a valid code
            _TOTP_CHALLENGES.pop(body.get("challenge") or "", None)
        must_change = bool(user.get("must_change_password"))
        token = issue_session(store, user, limited=must_change)
        resp = jsonify({"token": token,
                        "must_change_password": must_change,
                        "user": {"id": user["id"], "username": user["username"],
                                 "name": user["name"], "profile": user["profile"],
                                 "role": user["role"]}})
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Pragma"] = "no-cache"
        return resp

    # wrap the original login view to add the TOTP step
    if _orig_login_view:
        app.view_functions["login"] = _login_with_totp(_orig_login_view,
                                                       security)

    # ------------------------------------------------------------ quote PDF
    @app.get("/api/quotes/<qid>/pdf")
    @require_auth
    def quote_pdf(qid):
        user = request.mf_user
        obj = registry.get_object("Quote")
        q = obj and store.get("Quote", qid)
        if not q or not security.can(user, "read", "Quote") \
                or not security.can_see_record(user, q, "Quote"):
            return jsonify({"error": "Not found"}), 404
        lines = [(f"QUOTE  {q.get('Name') or qid}", 20, True),
                 ("", 12, False)]
        for label, key in (("Status", "Status"), ("Expiration", "ExpirationDate"),
                           ("Opportunity", "OpportunityId"),
                           ("Price Book", "PriceBookId")):
            if q.get(key):
                lines.append((f"{label}: {q[key]}", 11, False))
        if q.get("Description"):
            lines.append(("", 11, False))
            lines.append((str(q["Description"])[:500], 11, False))
        lines += [("", 11, False), ("Line items", 14, True)]
        items = [r for r in store.query("QuoteLineItem", limit=10000)
                 if r.get("QuoteId") == qid]
        total = 0.0
        lines.append((f"{'Qty':>5}  {'Unit price':>12}  {'Disc.':>6}  {'Total':>12}", 10, True))
        for it in items:
            qty = it.get("Quantity") or 0
            up = it.get("UnitPrice") or 0
            disc = it.get("Discount") or 0
            lt = it.get("TotalPrice") or (qty * up * (1 - (disc or 0) / 100))
            total += lt or 0
            prod = ""
            pbe = store.get("PriceBookEntry", it.get("PriceBookEntryId") or "")
            if pbe:
                prod = str(pbe.get("ProductId") or pbe.get("Name") or "")[:28]
            lines.append((f"{prod:<28} {qty:>5}  {up:>12.2f}  {disc:>5}%  {lt:>12.2f}",
                          10, False))
        lines += [("", 11, False), (f"TOTAL: {total:,.2f}", 14, True)]
        pdf = build_pdf(f"Quote {q.get('Name') or qid}", lines)
        name = f"quote-{(q.get('Name') or qid)}.pdf".replace(" ", "_")
        return Response(pdf, mimetype="application/pdf",
                        headers={"Content-Disposition":
                                 f"attachment; filename={name}"})

    # ------------------------------------------------------------ knowledge suggestions
    @app.get("/api/public/knowledge-suggest")
    def knowledge_suggest():
        q = (request.args.get("q") or "").strip().lower()
        if len(q) < 3:
            return jsonify([])
        hits = []
        for a in store.query("KnowledgeArticle", limit=10000):
            if (a.get("Status") or "") not in ("Published", "Draft"):
                continue
            hay = f"{a.get('Title') or ''} {a.get('Summary') or ''} {a.get('Body') or ''}".lower()
            if q in hay:
                score = hay.count(q)
                hits.append({"id": a["id"], "title": a.get("Title"),
                             "summary": (a.get("Summary") or "")[:220],
                             "score": score})
        hits.sort(key=lambda h: -h["score"])
        return jsonify([{k: h[k] for k in ("id", "title", "summary")} for h in hits[:5]])

    # ------------------------------------------------------------ KB article versions
    def _kb_article_or_404(article_id, user):
        rec = store.get("KnowledgeArticle", article_id)
        if not rec or not security.can_see_record(user, rec, "KnowledgeArticle"):
            return None
        return rec

    @app.get("/api/kb/articles/<article_id>/versions")
    @require_auth
    def kb_versions(article_id):
        user = request.mf_user
        if not _kb_article_or_404(article_id, user):
            return jsonify({"error": "Not found"}), 404
        rows = sorted(
            (r for r in store.config_all("mf_kb_versions")
             if r.get("article_id") == article_id),
            key=lambda r: -(r.get("version") or 0))
        return jsonify([{
            "id": r["id"], "version": r.get("version"),
            "title": r.get("title"), "status": r.get("status"),
            "category": r.get("category"),
            "created_by": r.get("created_by"),
            "created_date": r.get("created_date")} for r in rows])

    @app.get("/api/kb/articles/<article_id>/versions/<int:version>")
    @require_auth
    def kb_version_detail(article_id, version):
        user = request.mf_user
        if not _kb_article_or_404(article_id, user):
            return jsonify({"error": "Not found"}), 404
        row = next((r for r in store.config_all("mf_kb_versions")
                    if r.get("article_id") == article_id
                    and r.get("version") == version), None)
        if not row:
            return jsonify({"error": "Not found"}), 404
        return jsonify(row)

    @app.post("/api/kb/articles/<article_id>/versions/<int:version>/restore")
    @require_auth
    def kb_version_restore(article_id, version):
        user = request.mf_user
        rec = _kb_article_or_404(article_id, user)
        if not rec:
            return jsonify({"error": "Not found"}), 404
        if not security.can(user, "edit", "KnowledgeArticle"):
            return jsonify({"error": "No edit access on KnowledgeArticle"}), 403
        row = next((r for r in store.config_all("mf_kb_versions")
                    if r.get("article_id") == article_id
                    and r.get("version") == version), None)
        if not row:
            return jsonify({"error": "Not found"}), 404
        snap = row.get("snapshot") or {}
        fields = {k: snap.get(k) for k in
                  ("Title", "Summary", "Body", "Category", "Status")
                  if snap.get(k) not in (None, "")}
        status, payload = _do_update(user, "KnowledgeArticle", article_id, fields)
        if status >= 400:
            return jsonify(payload), status
        _audit("restore", "kb-version", f"{article_id} v{version}")
        return jsonify({**payload, "restored_version": version})


    # ------------------------------------------------------------ campaign member statuses
    def _campaign_or_404(cid, user):
        rec = store.get("Campaign", cid)
        if not rec or not security.can_see_record(user, rec, "Campaign"):
            return None
        return rec

    def _member_status_rows(cid):
        return sorted(
            (r for r in store.config_all("mf_campaign_member_statuses")
             if r.get("campaign_id") == cid),
            key=lambda r: (r.get("sort_order") or 0, r.get("name") or ""))

    @app.get("/api/campaigns/<cid>/member-statuses")
    @require_auth
    def campaign_member_statuses_list(cid):
        user = request.mf_user
        if not _campaign_or_404(cid, user):
            return jsonify({"error": "Not found"}), 404
        return jsonify([{
            "id": r["id"], "name": r["name"],
            "sort_order": r.get("sort_order") or 0,
            "active": r.get("active", True)}
            for r in _member_status_rows(cid)] or
            [{"id": None, "name": n, "sort_order": i, "active": True,
              "builtin": True}
             for i, n in enumerate(DEFAULT_MEMBER_STATUSES)])

    @app.post("/api/campaigns/<cid>/member-statuses")
    @require_auth
    def campaign_member_status_create(cid):
        user = request.mf_user
        if not _campaign_or_404(cid, user):
            return jsonify({"error": "Not found"}), 404
        if not security.can(user, "edit", "Campaign"):
            return jsonify({"error": "No edit access on Campaign"}), 403
        body = request.get_json(force=True, silent=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name is required"}), 422
        rows = _member_status_rows(cid)
        if not rows:
            # first customization: materialize the built-in defaults as rows
            # so the campaign's list stays explicit and complete
            for i, n in enumerate(DEFAULT_MEMBER_STATUSES):
                store.config_put("mf_campaign_member_statuses", {
                    "campaign_id": cid, "name": n, "active": True,
                    "sort_order": i})
            rows = _member_status_rows(cid)
        if name.lower() in {r["name"].lower() for r in rows}:
            return jsonify({"error": f"Status '{name}' already exists"}), 422
        rid = store.config_put("mf_campaign_member_statuses", {
            "campaign_id": cid, "name": name, "active": True,
            "sort_order": max([r.get("sort_order") or 0 for r in rows] + [0]) + 1})
        _audit("create", "campaign-member-status", f"{cid}:{name}")
        return jsonify(store.config_get("mf_campaign_member_statuses", rid)), 201

    @app.patch("/api/campaigns/<cid>/member-statuses/<sid>")
    @require_auth
    def campaign_member_status_update(cid, sid):
        user = request.mf_user
        if not _campaign_or_404(cid, user):
            return jsonify({"error": "Not found"}), 404
        if not security.can(user, "edit", "Campaign"):
            return jsonify({"error": "No edit access on Campaign"}), 403
        row = next((r for r in _member_status_rows(cid)
                    if str(r["id"]) == str(sid)), None)
        if not row:
            return jsonify({"error": "Not found"}), 404
        body = request.get_json(force=True, silent=True) or {}
        upd = {}
        if "name" in body:
            name = (body.get("name") or "").strip()
            if not name:
                return jsonify({"error": "name cannot be blank"}), 422
            upd["name"] = name
        if "active" in body:
            upd["active"] = bool(body.get("active"))
        if "sort_order" in body:
            upd["sort_order"] = body.get("sort_order") or 0
        rid = store.config_put("mf_campaign_member_statuses", {**row, **upd})
        row = store.config_get("mf_campaign_member_statuses", rid)
        _audit("update", "campaign-member-status", f"{cid}:{row['name']}")
        return jsonify(row)

    @app.delete("/api/campaigns/<cid>/member-statuses/<sid>")
    @require_auth
    def campaign_member_status_delete(cid, sid):
        user = request.mf_user
        if not _campaign_or_404(cid, user):
            return jsonify({"error": "Not found"}), 404
        if not security.can(user, "edit", "Campaign"):
            return jsonify({"error": "No edit access on Campaign"}), 403
        row = next((r for r in _member_status_rows(cid)
                    if str(r["id"]) == str(sid)), None)
        if not row:
            return jsonify({"error": "Not found"}), 404
        in_use = any(m.get("CampaignId") == cid and m.get("Status") == row["name"]
                     for m in store.query("CampaignMember", limit=100000))
        if in_use:
            return jsonify({"error": f"Status '{row['name']}' is in use by members"}), 422
        store.config_delete("mf_campaign_member_statuses", row["id"])
        _audit("delete", "campaign-member-status", f"{cid}:{row['name']}")
        return jsonify({"ok": True})


def _login_with_totp(orig_view, security):
    """Wrap /api/login: users with a TOTP secret get a challenge, not a token."""
    from flask import jsonify as _jsonify, request as _req

    def login():
        rv = orig_view()
        # orig returns a Response, or a (Response, status) tuple
        if isinstance(rv, tuple):
            resp, status = rv[0], rv[1]
        else:
            resp, status = rv, rv.status_code if hasattr(rv, "status_code") else 200
        # only intercept successful logins
        if status != 200:
            return rv
        data = resp.get_json(silent=True) or {}
        user = security.get_user((data.get("user") or {}).get("id") or "")
        if user and user.get("totp_secret"):
            chal = uuid.uuid4().hex
            with _TOTP_LOCK:
                _TOTP_CHALLENGES[chal] = {"user_id": user["id"],
                                         "expires": time.time() + 300}
            return _jsonify({"totp_required": True, "challenge": chal}), 200
        return rv

    login.__name__ = "login"
    return login


def _snapshot_flow_version(store, fid, definition, user, note=""):
    """Save a version snapshot of a flow before it is changed."""
    from ..store import new_id  # noqa: F401  (kept explicit for clarity)
    existing = [v for v in store.config_all("mf_flow_versions")
                if v.get("flow_id") == fid]
    ver = max([v.get("version", 0) for v in existing] + [0]) + 1
    snap = {"flow_id": fid, "version": ver,
            "definition": dict(definition),
            "created_by": (user or {}).get("id"),
            "created_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).isoformat(timespec="seconds"),
            "note": note or f"v{ver} snapshot"}
    snap["definition"].pop("id", None)
    store.config_put("mf_flow_versions", snap)
    return snap


def json_dumps(d):
    import json as _json
    return _json.dumps(d)
