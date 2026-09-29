"""Automation engine: validation rules, flows, approvals, duplicate rules,
field history, record types, and webhook dispatch."""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
import threading
import urllib.request

from .expressions import eval_expr, record_context, render_value
from . import crypto as _crypto
from .store import new_id, utcnow

MAX_FLOW_DEPTH = 3


# ------------------------------------------------------------ validation rules
def check_validation_rules(store, obj_name: str, record: dict, old_record: dict | None = None):
    """Return a list of violated rule messages for a (prospective) record."""
    messages = []
    for rule in store.config_all("mf_validation_rules"):
        if not rule.get("active", True) or rule.get("object") != obj_name:
            continue
        try:
            if eval_expr(rule.get("condition") or {}, record_context(record),
                         record_context(old_record)):
                messages.append(rule.get("message") or f"Validation rule '{rule.get('name')}' failed")
        except Exception as e:
            messages.append(f"Validation rule '{rule.get('name')}' error: {e}")
    return messages


# ------------------------------------------------------------ duplicate rules
def check_duplicates(store, obj_name: str, values: dict, exclude_id: str | None = None):
    """Return existing records that match an active matching rule."""
    dups = []
    for rule in store.config_all("mf_matching_rules"):
        if not rule.get("active", True) or rule.get("object") != obj_name:
            continue
        fields = rule.get("fields") or []
        keys = {f: values.get(f) for f in fields if values.get(f) not in (None, "")}
        if not keys:
            continue
        for rec in store.query(obj_name, owner_ids=None, limit=10000):
            if exclude_id and rec["id"] == exclude_id:
                continue
            if all(rec.get(f) == v for f, v in keys.items()):
                dups.append({"rule": rule.get("name"), "record_id": rec["id"],
                             "matched": keys})
    return dups


# ------------------------------------------------------------ field history
def log_history(store, obj_name: str, record_id: str, old: dict, new: dict, user: dict):
    for key, new_val in new.items():
        old_val = old.get(key)
        if old_val != new_val:
            store._execute(
                "INSERT INTO mf_history (id, object_name, record_id, field_name,"
                " old_value, new_value, changed_by, changed_at) VALUES (?,?,?,?,?,?,?,?)",
                (new_id(), obj_name, record_id, key, _s(old_val), _s(new_val),
                 user["id"], utcnow()),
            )
    store._commit()


def _s(v):
    return None if v is None else str(v)


def get_history(store, obj_name: str, record_id: str, limit: int = 100):
    rows = store._execute(
        "SELECT * FROM mf_history WHERE object_name=? AND record_id=? ORDER BY changed_at DESC LIMIT ?",
        (obj_name, record_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------ record types
def get_record_types(store, obj_name: str):
    return [r for r in store.config_all("mf_record_types") if r.get("object") == obj_name]


def default_record_type(store, obj_name: str) -> str:
    rts = get_record_types(store, obj_name)
    for rt in rts:
        if rt.get("is_default"):
            return rt["name"]
    return "Default"


def picklist_values_for(store, obj_name: str, record_type: str, field: dict):
    for rt in get_record_types(store, obj_name):
        if rt["name"] == record_type:
            override = (rt.get("picklist_overrides") or {}).get(field["name"])
            if override:
                return override
    return field.get("picklist_values")


# ------------------------------------------------------------ flows
def run_flows(store, registry, security, obj_name: str, event: str,
              record: dict, old_record: dict | None, user: dict, depth: int = 0):
    """Run matching flows. event in ('create', 'update'). Runs in system mode."""
    if depth >= MAX_FLOW_DEPTH:
        return
    for flow in store.config_all("mf_flows"):
        if not flow.get("active", True) or flow.get("object") != obj_name:
            continue
        trigger = flow.get("trigger", "on_create_or_update")
        if trigger == "on_create" and event != "create":
            continue
        if trigger == "on_update" and event != "update":
            continue
        try:
            if not eval_expr(flow.get("condition") or {}, record_context(record),
                             record_context(old_record)):
                continue
        except Exception:
            continue
        for action in flow.get("actions") or []:
            _run_flow_action(store, registry, security, action, record, user, depth,
                             obj_name)


def _run_flow_action(store, registry, security, action: dict, record: dict, user: dict,
                     depth: int, obj_name: str | None = None):
    atype = action.get("type")
    oname = obj_name or record.get("_object", "?")
    if atype == "set_fields":
        obj = registry.get_object(action.get("object") or "")
        target_id = render_value(action.get("record_id") or "{{Trigger.Id}}",
                                    record_context(record), user)
        fields = render_value(action.get("fields") or {}, record_context(record), user)
        if obj and target_id:
            clean, errors = registry.validate_record(obj, fields, partial=True)
            if not errors:
                store.update(obj["name"], target_id, clean)
    elif atype == "create_record":
        obj = registry.get_object(action.get("object") or "")
        if not obj:
            return
        fields = render_value(action.get("fields") or {}, record_context(record), user)
        clean, errors = registry.validate_record(obj, fields)
        if errors:
            return
        clean["owner_id"] = record.get("owner_id") or user["id"]
        clean["created_by"] = user["id"]
        clean.setdefault("record_type", default_record_type(store, obj["name"]))
        errs = run_triggers(store, registry, security, obj["name"],
                            "before_insert", clean, None, user, depth + 1)
        if errs:
            return
        new_id_ = store.insert(obj["name"], clean)
        new_rec = store.get(obj["name"], new_id_)
        run_triggers(store, registry, security, obj["name"],
                     "after_insert", new_rec, None, user, depth + 1)
        run_flows(store, registry, security, obj["name"], "create", new_rec, None, user, depth + 1)
    elif atype == "log":
        store._execute(
            "INSERT INTO mf_history (id, object_name, record_id, field_name, old_value,"
            " new_value, changed_by, changed_at) VALUES (?,?,?,?,?,?,?,?)",
            (new_id(), oname, record.get("id"), "__flow__", None,
             render_value(action.get("message", ""), record_context(record), user), user["id"], utcnow()),
        )
        store._commit()
    elif atype == "http_callout":
        ctx = record_context(record)
        res = invoke_callout(
            store, action.get("credential") or "",
            method=action.get("method") or "POST",
            path=render_value(action.get("path") or "", ctx, user),
            headers=render_value(action.get("headers") or {}, ctx, user),
            body=render_value(action.get("body"), ctx, user))
        store._execute(
            "INSERT INTO mf_history (id, object_name, record_id, field_name,"
            " old_value, new_value, changed_by, changed_at) VALUES (?,?,?,?,?,?,?,?)",
            (new_id(), oname, record.get("id"), "__callout__",
             action.get("credential"),
             f"status={res['status']} ok={res['ok']} "
             f"{(res.get('body') or res.get('error') or '')[:500]}",
             user["id"], utcnow()),
        )
        store._commit()


# ------------------------------------------------------------ approvals
def find_approval_process(store, obj_name: str, record: dict):
    for proc in store.config_all("mf_approval_processes"):
        if not proc.get("active", True) or proc.get("object") != obj_name:
            continue
        try:
            if eval_expr(proc.get("entry_conditions") or {}, record_context(record)):
                return proc
        except Exception:
            continue
    return None


def pending_request_for(store, obj_name: str, record_id: str):
    for req in store.config_all("mf_approval_requests"):
        if (req.get("object") == obj_name and req.get("record_id") == record_id
                and req.get("status") == "Pending"):
            return req
    return None


def submit_for_approval(store, security, obj_name: str, record: dict, user: dict):
    proc = find_approval_process(store, obj_name, record)
    if not proc:
        return None, "No approval process matches this record"
    if pending_request_for(store, obj_name, record["id"]):
        return None, "An approval request is already pending"
    steps = proc.get("steps") or [{"name": "Step 1", "approver": "manager"}]
    req = {"object": obj_name, "record_id": record["id"], "process_id": proc["id"],
           "process_name": proc.get("name"), "status": "Pending",
           "current_step": 0, "steps": steps, "submitted_by": user["id"],
           "submitted_at": utcnow(), "history": []}
    rid = store.config_put("mf_approval_requests", req)
    saved = store.config_get("mf_approval_requests", rid)
    for uid in _approver_ids(store, security, saved):
        if uid != user["id"]:
            store.notify(uid, "approval",
                         f"Approval requested: {obj_name}",
                         f"{user.get('name')} submitted a {obj_name} record "
                         f"({saved.get('process_name')}) for approval.",
                         obj_name, record["id"])
    return saved, None


def _approver_ids(store, security, req: dict):
    """User ids allowed to act on the current step."""
    step = (req.get("steps") or [])[req.get("current_step", 0)] or {}
    approver = step.get("approver")
    owner_id = None
    row = store._execute(
        f"SELECT owner_id FROM {store._table(req['object'])} WHERE id=?", (req["record_id"],)
    ).fetchone()
    if row:
        owner_id = row["owner_id"]
    ids = set()
    if isinstance(approver, dict):
        if approver.get("type") == "user":
            ids.add(approver.get("id"))
        elif approver.get("type") == "role":
            subtree = security._role_subtree(approver.get("role"))
            ids.update(u["id"] for u in security.list_users() if u.get("role") in subtree)
    elif approver == "manager" and owner_id:
        owner = security.get_user(owner_id)
        if owner and owner.get("role"):
            role = security.get_role(owner["role"])
            parent = role.get("parent") if role else None
            if parent:
                ids.update(u["id"] for u in security.list_users() if u.get("role") == parent)
    return ids


def pending_for_user(store, security, user: dict):
    out = []
    for req in store.config_all("mf_approval_requests"):
        if req.get("status") != "Pending":
            continue
        if user["id"] in _approver_ids(store, security, req) or security.is_admin(user):
            out.append(req)
    return out


def decide_request(store, security, request_id: str, user: dict, approve: bool, comment: str = ""):
    req = store.config_get("mf_approval_requests", request_id)
    if not req or req.get("status") != "Pending":
        return None, "Request not found or not pending"
    if user["id"] not in _approver_ids(store, security, req) and not security.is_admin(user):
        return None, "You are not an approver for this request"
    req["status"] = "Approved" if approve else "Rejected"
    req["history"].append({"by": user["id"], "at": utcnow(),
                           "decision": req["status"], "comment": comment})
    store.config_put("mf_approval_requests", req)
    return req, None


# ------------------------------------------------------------ roll-up summaries
def compute_rollup(store, security, user: dict, spec: dict, parent_id: str):
    """Aggregate child records into a parent summary value (computed on read).

    spec: {"object": "Opportunity", "via": "AccountId", "field": "Amount",
           "func": "sum"|"avg"|"min"|"max"|"count", "filter": {...optional...}}
    """
    children = store.query(spec["object"], owner_ids=None, limit=10000)
    vals = []
    for c in children:
        if c.get(spec["via"]) != parent_id:
            continue
        if not security.can_see_record(user, c, spec["object"]):
            continue
        if spec.get("filter"):
            try:
                if not eval_expr(spec["filter"], record_context(c), user=user):
                    continue
            except Exception:
                continue
        vals.append(c.get(spec["field"]))
    func = spec.get("func", "count")
    if func == "count":
        return len(vals)
    nums = [v for v in vals if isinstance(v, (int, float))]
    if not nums:
        return None
    if func == "sum":
        return sum(nums)
    if func == "avg":
        return sum(nums) / len(nums)
    if func == "min":
        return min(nums)
    if func == "max":
        return max(nums)
    return None


# ------------------------------------------------------------ code triggers
TRIGGER_EVENTS = ("before_insert", "after_insert", "before_update",
                  "after_update", "before_delete", "after_delete")
MAX_TRIGGER_DEPTH = 3

TRIGGER_BUILTINS = {
    "len": len, "str": str, "int": int, "float": float, "bool": bool,
    "list": list, "dict": dict, "set": set, "tuple": tuple,
    "min": min, "max": max, "sum": sum, "abs": abs, "round": round,
    "sorted": sorted, "enumerate": enumerate, "range": range,
    "any": any, "all": all, "isinstance": isinstance,
}


class TriggerAbort(Exception):
    pass


def _trigger_dml_ops(store, registry, security, user: dict, depth: int, errors: list):
    """System-mode create/update/query helpers exposed to trigger code."""
    def query(obj_name, **filters):
        obj = registry.get_object(obj_name)
        if not obj:
            raise TriggerAbort(f"Unknown object '{obj_name}'")
        out = []
        for r in store.query(obj_name, owner_ids=None, limit=10000):
            if all(r.get(k) == v for k, v in filters.items()) \
                    and security.can_see_record(user, r, obj_name):
                out.append(r)
        return out

    def create(obj_name, fields):
        obj = registry.get_object(obj_name)
        if not obj:
            raise TriggerAbort(f"Unknown object '{obj_name}'")
        if not security.can(user, "create", obj_name):
            raise TriggerAbort(f"No create access on {obj_name}")
        clean, verrs = registry.validate_record(obj, dict(fields))
        if verrs:
            raise TriggerAbort("; ".join(verrs))
        vr = check_validation_rules(store, obj_name, clean)
        if vr:
            raise TriggerAbort("; ".join(vr))
        clean["owner_id"] = user["id"]
        clean["created_by"] = user["id"]
        clean.setdefault("record_type", default_record_type(store, obj_name))
        errs = run_triggers(store, registry, security, obj_name,
                            "before_insert", clean, None, user, depth + 1)
        if errs:
            raise TriggerAbort("; ".join(errs))
        rid = store.insert(obj_name, clean)
        rec = store.get(obj_name, rid)
        errs = run_triggers(store, registry, security, obj_name,
                            "after_insert", rec, None, user, depth + 1)
        if errs:
            store.delete(obj_name, rid)
            raise TriggerAbort("; ".join(errs))
        run_flows(store, registry, security, obj_name, "create", rec, None, user, depth + 1)
        return rec

    def update(obj_name, rid, fields):
        obj = registry.get_object(obj_name)
        rec = obj and store.get(obj_name, rid)
        if not rec:
            raise TriggerAbort("Record not found")
        if not security.can(user, "edit", obj_name) \
                or not security.can_see_record(user, rec, obj_name):
            raise TriggerAbort(f"No edit access on {obj_name}")
        clean, verrs = registry.validate_record(obj, dict(fields), partial=True)
        if verrs:
            raise TriggerAbort("; ".join(verrs))
        merged = {**rec, **clean}
        vr = check_validation_rules(store, obj_name, merged, rec)
        if vr:
            raise TriggerAbort("; ".join(vr))
        errs = run_triggers(store, registry, security, obj_name,
                            "before_update", merged, rec, user, depth + 1)
        if errs:
            raise TriggerAbort("; ".join(errs))
        store.update(obj_name, rid, {k: merged[k] for k in clean})
        new_rec = store.get(obj_name, rid)
        errs = run_triggers(store, registry, security, obj_name,
                            "after_update", new_rec, rec, user, depth + 1)
        if errs:
            store.update(obj_name, rid, {k: rec[k] for k in clean if k in rec})
            raise TriggerAbort("; ".join(errs))
        run_flows(store, registry, security, obj_name, "update", new_rec, rec, user, depth + 1)
        return new_rec

    return query, create, update


def run_triggers(store, registry, security, obj_name: str, event: str,
                 record: dict, old_record: dict | None, user: dict, depth: int = 0):
    """Execute active code triggers for an event. Returns a list of error strings.

    In before_* events the trigger may mutate `record` and append to `errors`
    to block the save (like Apex's addError). No imports, files, or network
    are available to trigger code.
    """
    if depth >= MAX_TRIGGER_DEPTH:
        return []
    errors: list = []
    triggers = [t for t in store.config_all("mf_triggers")
                if t.get("active", True) and t.get("object") == obj_name
                and event in (t.get("events") or [])]
    triggers.sort(key=lambda t: t.get("order", 0))
    query, create, update = _trigger_dml_ops(store, registry, security, user, depth, errors)
    for trig in triggers:
        ctx = {"__builtins__": TRIGGER_BUILTINS,
               "record": record, "old": old_record, "user": user, "event": event,
               "errors": errors, "query": query, "create": create, "update": update}
        try:
            exec(compile(trig.get("code") or "", f"<trigger {trig.get('name')}>", "exec"), ctx)
        except TriggerAbort as e:
            errors.append(str(e))
        except Exception as e:
            errors.append(f"Trigger '{trig.get('name')}' failed: {type(e).__name__}: {e}")
    return errors


# ------------------------------------------------------------ assignment rules
def apply_assignment_rules(store, registry, security, obj_name: str, clean: dict,
                           creator: dict) -> str | None:
    """Evaluate active assignment rules in order; the first matching rule wins.

    Returns an owner user id, or None to keep the creator as owner.
    Assignee forms: {"type": "user", "username": "leo"} or
    {"type": "round_robin", "usernames": ["leo", "maya"]} (counter persisted
    on the rule itself).
    """
    obj = registry.get_object(obj_name)
    if not obj:
        return None
    users = {u["username"]: u for u in store.meta_all("mf_users")}
    rules = [r for r in store.config_all("mf_assignment_rules")
             if r.get("active", True) and r.get("object") == obj_name]
    rules.sort(key=lambda r: r.get("order", 0))
    for rule in rules:
        try:
            if rule.get("criteria") and not eval_expr(
                    rule["criteria"], record_context(clean), user=creator):
                continue
        except Exception:
            continue
        assignee = rule.get("assignee") or {}
        if assignee.get("type") == "user":
            u = users.get(assignee.get("username"))
            return u["id"] if u else None
        if assignee.get("type") == "round_robin":
            names = [n for n in (assignee.get("usernames") or []) if n in users]
            if not names:
                return None
            idx = int(rule.get("counter", 0)) % len(names)
            rule["counter"] = int(rule.get("counter", 0)) + 1
            store.config_put("mf_assignment_rules", rule)
            return users[names[idx]]["id"]
    return None


# ------------------------------------------------------------ scheduled jobs
from datetime import date as _date, timedelta as _timedelta


def _today():
    """Python-level wrapper: calling C classmethods like date.today() directly
    inside the sandbox trips a CPython builtins-dict quirk (KeyError:
    '__import__'), so scheduled code uses today() instead."""
    return _date.today()


SCHEDULED_BUILTINS = {**TRIGGER_BUILTINS, "today": _today,
                      "timedelta": _timedelta, "date": _date}


def run_scheduled_job(store, registry, security, job: dict, run_as: dict) -> dict:
    """Execute one scheduled job's code. Returns {"ok": bool, "detail": str}."""
    errors: list = []
    query, create, update = _trigger_dml_ops(store, registry, security, run_as, 0, errors)
    # scheduled jobs may assign ownership explicitly via OwnerId in create()
    _orig_create = create

    def create_owned(obj_name, fields):
        fields = dict(fields)
        owner = fields.pop("OwnerId", None)
        rec = _orig_create(obj_name, fields)
        if owner:
            store.update(obj_name, rec["id"], {"owner_id": owner})
            rec = store.get(obj_name, rec["id"])
        return rec

    ctx = {"__builtins__": SCHEDULED_BUILTINS, "user": run_as, "errors": errors,
           "query": query, "create": create_owned, "update": update, "job": job,
           # full platform access for admin-authored jobs (e.g. the SLA
           # monitor calls automation.check_sla_breaches(store, security))
           "store": store, "registry": registry, "security": security,
           "automation": sys.modules[__name__]}
    try:
        exec(compile(job.get("code") or "", f"<scheduled {job.get('name')}>", "exec"), ctx)
    except TriggerAbort as e:
        errors.append(str(e))
    except Exception as e:
        errors.append(f"{type(e).__name__}: {e}")
    ok = not errors
    detail = "; ".join(errors) if errors else "ok"
    store.log_scheduled_run(job["id"], "ok" if ok else "error", detail[:2000])
    return {"ok": ok, "detail": detail}


def run_due_scheduled_jobs(store, registry, security) -> list:
    """Run every active job whose interval has elapsed. Returns per-job results."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    users = {u["username"]: u for u in store.meta_all("mf_users")}
    admin = users.get("admin")
    results = []
    for job in store.config_all("mf_scheduled_jobs"):
        if not job.get("active", True):
            continue
        interval = int(job.get("interval_minutes") or 1440)
        last = job.get("last_run")
        due = True
        if last:
            try:
                elapsed = (now - datetime.fromisoformat(last)).total_seconds() / 60
                due = elapsed >= interval
            except Exception:
                due = True
        if not due:
            continue
        run_as = users.get(job.get("run_as") or "admin") or admin
        res = run_scheduled_job(store, registry, security, job, run_as)
        job["last_run"] = now.isoformat(timespec="seconds")
        store.config_put("mf_scheduled_jobs", job)
        results.append({"job": job.get("name"), **res})
    return results


# ------------------------------------------------------------ email merge
def merge_template(text: str, record: dict, user: dict) -> str:
    """Merge {{Record.Field}} / {{User.Name}} style templates."""
    if not text:
        return ""
    import re
    ctx = {"Record": record_context(record), "User": user or {}}

    def repl(m):
        path = m.group(1).strip().split(".")
        val = ctx
        for part in path:
            val = val.get(part) if isinstance(val, dict) else None
            if val is None:
                return ""
        return str(val)

    return re.sub(r"\{\{\s*([^}]+?)\s*\}\}", repl, text)


# ------------------------------------------------------------ packaging
PACKAGE_TABLES = {
    "validation_rules": ("mf_validation_rules", ("object", "name")),
    "flows": ("mf_flows", ("object", "name")),
    "triggers": ("mf_triggers", ("object", "name")),
    "approval_processes": ("mf_approval_processes", ("object", "name")),
    "record_types": ("mf_record_types", ("object", "name")),
    "sharing_rules": ("mf_sharing_rules", ("object", "name")),
    "matching_rules": ("mf_matching_rules", ("object", "name")),
    "permission_sets": ("mf_permission_sets", ("name",)),
    "webhooks": ("mf_webhooks", ("name",)),
    "list_views": ("mf_list_views", ("object", "name")),
    "scheduled_jobs": ("mf_scheduled_jobs", ("name",)),
    "assignment_rules": ("mf_assignment_rules", ("object", "name")),
    "email_templates": ("mf_email_templates", ("name",)),
    "auto_responses": ("mf_auto_responses", ("name",)),
    "paths": ("mf_paths", ("object",)),
    "ml_models": ("mf_ml_models", ("id",)),
    "reports": ("mf_reports", ("name",)),
    "sla_policies": ("mf_sla_policies", ("object", "name")),
    "escalation_rules": ("mf_escalation_rules", ("object", "name")),
    "named_credentials": ("mf_named_credentials", ("name",)),
    "forecast_quotas": ("mf_forecast_quotas", ("user_id", "period")),
}


def build_package(store, registry) -> dict:
    """Export customizations as a versioned package installable on another org."""
    import os
    std_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "metadata", "standard_objects.json")
    std_fields = {}
    if os.path.exists(std_path):
        std_fields = {o["name"]: {f["name"] for f in o.get("fields", [])}
                      for o in json.load(open(std_path))}
    custom_objects, extra_fields = [], {}
    for obj in registry.list_objects():
        name = obj["name"]
        if obj.get("is_custom"):
            custom_objects.append(obj)
        else:
            extra = [f for f in obj.get("fields", [])
                     if f["name"] not in std_fields.get(name, set())]
            if extra:
                extra_fields[name] = extra
    pkg = {"package_version": 1, "name": "forcelet-package",
           "exported_at": utcnow(),
           "custom_objects": custom_objects,
           "standard_object_fields": extra_fields,
           "layouts": store.layouts_all(),
           "config": {kind: store.config_all(table)
                      for kind, (table, _keys) in PACKAGE_TABLES.items()}}
    return pkg


def import_package(store, registry, pkg: dict, user: dict) -> dict:
    """Install a package: upserts objects, fields, layouts and config by natural key."""
    if not isinstance(pkg, dict) or pkg.get("package_version") != 1:
        raise ValueError("Not a forcelet package (package_version must be 1)")
    summary = {"objects": 0, "fields": 0, "layouts": 0, "config": {}}

    def add_missing_fields(obj_name, fields):
        existing = {f["name"] for f in registry.get_object(obj_name)["fields"]}
        n = 0
        for f in fields:
            if f["name"] in existing:
                continue
            f = {k: v for k, v in f.items() if k not in ("id",)}
            registry.add_field(obj_name, f)
            n += 1
        return n

    for obj in pkg.get("custom_objects", []):
        name = obj.get("name")
        from .field_types import is_valid_api_name
        if not name or not is_valid_api_name(name):
            continue
        if not registry.get_object(name):
            registry.create_object(name, obj.get("label", name), obj.get("plural", name))
            summary["objects"] += 1
        summary["fields"] += add_missing_fields(name, obj.get("fields", []))
    for obj_name, fields in (pkg.get("standard_object_fields") or {}).items():
        if registry.get_object(obj_name):
            summary["fields"] += add_missing_fields(obj_name, fields)

    for lay in pkg.get("layouts", []):
        if not registry.get_object(lay.get("object") or ""):
            continue
        store.layout_put(lay["object"], lay.get("profile", "Default"),
                         {"sections": lay.get("sections", []),
                          "related_lists": lay.get("related_lists", [])},
                         lay.get("record_type", "Default"))
        summary["layouts"] += 1

    for kind, defs in (pkg.get("config") or {}).items():
        if kind not in PACKAGE_TABLES:
            continue
        table, keys = PACKAGE_TABLES[kind]
        n = 0
        for d in defs:
            d = dict(d)
            if kind == "list_views":
                d["owner_id"] = user["id"]  # personal views get re-owned by the importer
            match = None
            for existing in store.config_all(table):
                if all(existing.get(k) == d.get(k) for k in keys):
                    match = existing
                    break
            d["id"] = match["id"] if match else d.get("id")
            store.config_put(table, d)
            n += 1
        summary["config"][kind] = n
    return summary


# ------------------------------------------------------------ webhooks
def dispatch_webhooks(store, obj_name: str, event: str, record: dict, user: dict):
    hooks = [h for h in store.config_all("mf_webhooks")
             if h.get("active", True) and h.get("object") == obj_name
             and event in (h.get("events") or [])]
    if not hooks:
        return

    def fire():
        for hook in hooks:
            payload = json.dumps({"event": f"{obj_name}.{event}",
                                  "object": obj_name, "record": record,
                                  "actor": user["id"], "at": utcnow()})
            status, detail = "delivered", ""
            try:
                req = urllib.request.Request(
                    hook["url"], data=payload.encode(),
                    headers={"Content-Type": "application/json",
                             "X-Forcelet-Event": f"{obj_name}.{event}"})
                secret = hook.get("secret")
                if secret:
                    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
                    req.add_header("X-Forcelet-Signature", f"sha256={sig}")
                with urllib.request.urlopen(req, timeout=8) as resp:
                    detail = f"HTTP {resp.status}"
            except Exception as e:
                status, detail = "failed", f"{type(e).__name__}: {e}"
            store._execute(
                "INSERT INTO mf_webhook_deliveries (id, webhook_id, event, payload,"
                " status, detail, attempted_at) VALUES (?,?,?,?,?,?,?)",
                (new_id(), hook["id"], f"{obj_name}.{event}", payload,
                 status, detail[:500], utcnow()),
            )
            store._commit()

    threading.Thread(target=fire, daemon=True).start()


# ------------------------------------------------------------ chatter feed
import re as _re

MENTION_RE = _re.compile(r"@([A-Za-z][A-Za-z0-9_]*)")


def find_mentioned_users(store, body: str):
    """Users whose @username appears in the post body."""
    names = {m.lower() for m in MENTION_RE.findall(body or "")}
    if not names:
        return []
    return [u for u in store.meta_all("mf_users")
            if str(u.get("username", "")).lower() in names]


def post_to_feed(store, security, user: dict, object_name: str | None,
                 record_id: str | None, body: str):
    """Create a feed post, recording @mentions. Returns (post, error)."""
    if not (body or "").strip():
        return None, "Post body is required"
    if object_name:
        if not security.can(user, "read", object_name):
            return None, "Unknown object or no access"
    if record_id:
        rec = store.get(object_name, record_id)
        if not rec or not security.can_see_record(user, rec, object_name):
            return None, "Record not found"
    pid = store.feed_post(user["id"], body.strip(), object_name, record_id)
    for u in find_mentioned_users(store, body):
        if u["id"] != user["id"]:
            store.feed_mention(pid, u["id"])
    post = store.feed_get_post(pid)
    post["user_name"] = user.get("name")
    post["like_count"] = 0
    post["comment_count"] = 0
    return post, None


# ------------------------------------------------------------ lead conversion
def _convert_insert(store, registry, security, obj_name: str, fields: dict,
                    owner_id: str, user: dict):
    """Insert one converted record through validation + triggers + flows."""
    obj = registry.get_object(obj_name)
    clean, errors = registry.validate_record(obj, fields)
    if errors:
        raise ValueError("; ".join(errors))
    clean["owner_id"] = owner_id
    clean["created_by"] = user["id"]
    clean.setdefault("record_type", default_record_type(store, obj_name))
    terr = run_triggers(store, registry, security, obj_name,
                        "before_insert", clean, None, user)
    if terr:
        raise ValueError("; ".join(terr))
    vr = check_validation_rules(store, obj_name, clean)
    if vr:
        raise ValueError("; ".join(vr))
    rid = store.insert(obj_name, clean)
    rec = store.get(obj_name, rid)
    terr = run_triggers(store, registry, security, obj_name,
                        "after_insert", rec, None, user)
    if terr:
        store.delete(obj_name, rid)
        raise ValueError("; ".join(terr))
    run_flows(store, registry, security, obj_name, "create", rec, None, user)
    return rec


def convert_lead(store, registry, security, lead_id: str, user: dict,
                 options: dict | None = None):
    """Convert a Lead into Account + Contact (+ Opportunity).

    options: {"account_name", "contact": {...overrides},
              "opportunity_name", "create_opportunity": bool,
              "opportunity": {...overrides}}
    Returns (result, error).
    """
    options = options or {}
    lead = store.get("Lead", lead_id)
    if not lead or not security.can_see_record(user, lead, "Lead"):
        return None, "Lead not found"
    if not security.can(user, "edit", "Lead"):
        return None, "No edit access on Lead"
    if lead.get("Status") == "Converted":
        return None, "Lead is already converted"
    for obj_name in ("Account", "Contact", "Opportunity"):
        if not security.can(user, "create", obj_name):
            return None, f"No create access on {obj_name}"
    account_name = options.get("account_name") or lead.get("Company") \
        or f"{lead.get('FirstName', '')} {lead.get('LastName', '')}".strip() \
        or "Converted Account"
    try:
        account = _convert_insert(store, registry, security, "Account",
                                  {"Name": account_name}, lead.get("owner_id")
                                  or user["id"], user)
        contact_fields = {"FirstName": lead.get("FirstName"),
                          "LastName": lead.get("LastName") or "Unknown",
                          "Email": lead.get("Email"), "Phone": lead.get("Phone"),
                          "AccountId": account["id"]}
        contact_fields.update(options.get("contact") or {})
        contact = _convert_insert(store, registry, security, "Contact",
                                  {k: v for k, v in contact_fields.items()
                                   if v not in (None, "")},
                                  account["owner_id"], user)
        opportunity = None
        if options.get("create_opportunity", True):
            opp_obj = registry.get_object("Opportunity")
            stage_field = next((f for f in opp_obj["fields"] if f["name"] == "Stage"), {})
            opp_fields = {"Name": options.get("opportunity_name")
                          or f"{account_name} Opportunity",
                          "AccountId": account["id"],
                          "Stage": (stage_field.get("picklist_values") or ["Prospecting"])[0],
                          "CloseDate": str(_date.today() + _timedelta(days=30))}
            opp_fields.update(options.get("opportunity") or {})
            opportunity = _convert_insert(store, registry, security, "Opportunity",
                                          opp_fields, account["owner_id"], user)
    except ValueError as e:
        return None, str(e)
    # mark the lead converted
    store.update("Lead", lead_id, {"Status": "Converted"})
    new_lead = store.get("Lead", lead_id)
    log_history(store, "Lead", lead_id, lead, new_lead, user)
    store.emit_change("Lead", lead_id, "update", user, changed_fields=["Status"],
                      snapshot={k: v for k, v in new_lead.items()
                                if not _crypto.is_encrypted(v)})
    store.log_lead_conversion(lead_id, account["id"], contact["id"],
                              opportunity["id"] if opportunity else None, user)
    return {"lead_id": lead_id, "account_id": account["id"],
            "contact_id": contact["id"],
            "opportunity_id": opportunity["id"] if opportunity else None}, None


# ------------------------------------------------------------ auto-response rules
def send_templated_email(store, obj_name: str, record: dict, template: dict,
                         user: dict, to_addr: str = ""):
    """Merge + log + timeline an email from a template. Returns (to, subject)."""
    subject = merge_template(template.get("subject") or "", record, user)
    body = merge_template(template.get("body") or "", record, user)
    recipient = to_addr or record_context(record).get("Email") or ""
    store.log_email(obj_name, record.get("id"), recipient, subject, body,
                    template.get("name", ""), user)
    store.add_activity(obj_name, record.get("id"), "email",
                       f"Auto-response: {subject}", body, user)
    return recipient, subject


def run_auto_responses(store, registry, security, obj_name: str,
                       record: dict, user: dict):
    """Send the first matching active auto-response rule's template (on create).

    Returns the rule name that fired, or None.
    """
    rules = [r for r in store.config_all("mf_auto_responses")
             if r.get("active", True) and r.get("object") == obj_name]
    rules.sort(key=lambda r: r.get("order", 0))
    for rule in rules:
        try:
            if rule.get("criteria") and not eval_expr(
                    rule["criteria"], record_context(record), user=user):
                continue
        except Exception:
            continue
        tpl = store.config_get("mf_email_templates", rule.get("template_id") or "")
        if not tpl:
            continue
        send_templated_email(store, obj_name, record, tpl, user)
        return rule.get("name")
    return None


# ------------------------------------------------------------ sales path
def get_path(store, obj_name: str):
    """Active Path configuration for an object, or None."""
    for p in store.config_all("mf_paths"):
        if p.get("object") == obj_name and p.get("active", True):
            return p
    return None


# ------------------------------------------------------------ case SLA milestones
def sla_policy_for(store, obj_name: str, record: dict):
    """First active SLA policy matching the record's priority (or '*')."""
    for p in store.config_all("mf_sla_policies"):
        if not p.get("active", True) or p.get("object") != obj_name:
            continue
        want = p.get("priority") or "*"
        if want == "*" or want == (record.get("Priority") or ""):
            return p
    return None


def start_case_milestones(store, obj_name: str, rec: dict):
    """Stamp due-time milestones on a newly created record. Returns the policy."""
    from datetime import datetime, timezone, timedelta
    policy = sla_policy_for(store, obj_name, rec)
    if not policy:
        return None
    now = datetime.now(timezone.utc)
    for ms in policy.get("milestones") or []:
        mins = int(ms.get("target_minutes") or 0)
        due = now + timedelta(minutes=mins)
        store.config_put("mf_case_milestones", {
            "object": obj_name, "record_id": rec["id"],
            "policy": policy.get("name"), "name": ms.get("name"),
            "due_at": due.isoformat(timespec="seconds"),
            "completed_at": None, "breached": False})
    return policy


def complete_case_milestones(store, obj_name: str, rec: dict, old: dict | None):
    """Mark open milestones complete when a case is closed."""
    if rec.get("Status") != "Closed" or (old or {}).get("Status") == "Closed":
        return 0
    n = 0
    for m in store.config_all("mf_case_milestones"):
        if m.get("record_id") == rec["id"] and not m.get("completed_at"):
            m["completed_at"] = utcnow()
            store.config_put("mf_case_milestones", m)
            n += 1
    return n


def case_milestones(store, record_id: str):
    return sorted(
        (m for m in store.config_all("mf_case_milestones")
         if m.get("record_id") == record_id),
        key=lambda m: m.get("due_at") or "")


def manager_of(security, user: dict):
    """The user holding the parent role, or None."""
    role = security.get_role(user.get("role") or "")
    parent = role and role.get("parent")
    if not parent:
        return None
    for u in security.list_users():
        if u.get("role") == parent and u["id"] != user["id"]:
            return u
    return None


def check_sla_breaches(store, security):
    """Mark overdue milestones breached and notify owners + their managers.

    Returns the list of newly breached milestones. Intended for a scheduled job.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    breached = []
    for m in store.config_all("mf_case_milestones"):
        if m.get("completed_at") or m.get("breached"):
            continue
        try:
            due = datetime.fromisoformat(m["due_at"])
        except Exception:
            continue
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        if due < now:
            m["breached"] = True
            store.config_put("mf_case_milestones", m)
            breached.append(m)
    for m in breached:
        rec = store.get(m.get("object") or "Case", m["record_id"])
        if not rec or not rec.get("owner_id"):
            continue
        owner = security.get_user(rec["owner_id"])
        if not owner:
            continue
        label = rec.get("Subject") or rec.get("Name") or m["record_id"]
        for person in (owner, manager_of(security, owner)):
            if person:
                store.notify(person["id"], "sla",
                             f"SLA breached: {m['name']}",
                             f"The '{m['name']}' milestone for {label} was due "
                             f"{m['due_at']}.", m.get("object"), m["record_id"])
    return breached


# ------------------------------------------------------------ escalation rules
def apply_escalation_rules(store, registry, security, obj_name: str, record: dict,
                           old_record: dict | None, user: dict, trigger_on: str = "save"):
    """Evaluate criteria-based escalation rules. Runs in system mode; actions
    update the record directly (no recursive re-evaluation)."""
    fired = []
    for rule in store.config_all("mf_escalation_rules"):
        if not rule.get("active", True) or rule.get("object") != obj_name:
            continue
        if (rule.get("trigger_on") or "save") != trigger_on:
            continue
        try:
            crit = rule.get("criteria")
            if crit and not eval_expr(crit, record_context(record),
                                      record_context(old_record)):
                continue
        except Exception:
            continue
        action = rule.get("action") or {}
        atype = action.get("type")
        if atype == "set_fields":
            fields = {k: v for k, v in (action.get("fields") or {}).items()}
            if fields:
                store.update(obj_name, record["id"], fields)
                record.update(fields)
        elif atype == "reassign":
            new_owner = action.get("user_id")
            if new_owner and security.get_user(new_owner):
                store.update(obj_name, record["id"], {"owner_id": new_owner})
                record["owner_id"] = new_owner
                store.notify(new_owner, "assignment",
                             f"Escalated {obj_name} assigned to you",
                             f"{rule.get('name')}: "
                             f"{record.get('Subject') or record.get('Name') or record['id']}",
                             obj_name, record["id"])
        elif atype == "notify":
            target = action.get("to") or "owner"
            recipients = []
            if target == "owner" and record.get("owner_id"):
                recipients = [security.get_user(record["owner_id"])]
            elif target == "manager" and record.get("owner_id"):
                owner = security.get_user(record["owner_id"])
                recipients = [owner and manager_of(security, owner)]
            elif target not in ("owner", "manager"):
                recipients = [security.get_user(target)]
            for person in recipients:
                if person:
                    store.notify(person["id"], "escalation",
                                 f"Escalation: {rule.get('name')}",
                                 action.get("message") or
                                 f"{obj_name} {record.get('Subject') or record.get('Name') or record['id']} "
                                 f"was escalated by rule '{rule.get('name')}'.",
                                 obj_name, record["id"])
        fired.append(rule.get("name"))
    return fired


# ------------------------------------------------------------ named credentials + HTTP callouts
def get_named_credential(store, ref: str):
    for c in store.config_all("mf_named_credentials"):
        if c.get("id") == ref or c.get("name") == ref:
            return c
    return None


def invoke_callout(store, cred_ref: str, method: str = "GET", path: str = "",
                   headers: dict | None = None, body=None, timeout: int = 10):
    """Execute an HTTP callout through a named credential. Returns
    {"ok", "status", "body", "error"}. Secrets stay server-side."""
    import base64
    cred = get_named_credential(store, cred_ref)
    if not cred:
        return {"ok": False, "status": None, "body": None,
                "error": f"Unknown credential '{cred_ref}'"}
    if not cred.get("active", True):
        return {"ok": False, "status": None, "body": None,
                "error": f"Credential '{cred.get('name')}' is inactive"}
    base = (cred.get("url") or "").rstrip("/")
    url = base + (path or "")
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "status": None, "body": None,
                "error": "Credential URL must start with http(s)://"}
    data = None
    req_headers = dict(headers or {})
    if body is not None:
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        req_headers.setdefault("Content-Type", "application/json")
    auth = cred.get("auth_type") or "none"
    secret = _crypto.decrypt(cred.get("secret_enc") or "")
    if auth == "basic":
        token = base64.b64encode(
            f"{cred.get('username') or ''}:{secret}".encode()).decode()
        req_headers["Authorization"] = f"Basic {token}"
    elif auth == "bearer":
        req_headers["Authorization"] = f"Bearer {secret}"
    elif auth == "api_key":
        req_headers[cred.get("api_key_header") or "X-API-Key"] = secret
    try:
        req = urllib.request.Request(url, data=data, headers=req_headers,
                                     method=(method or "GET").upper())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return {"ok": 200 <= resp.status < 300, "status": resp.status,
                    "body": raw[:20000], "error": None}
    except Exception as e:
        return {"ok": False, "status": getattr(e, "code", None),
                "body": None, "error": f"{type(e).__name__}: {e}"}


# ------------------------------------------------------------ forecasts
def forecast_for_period(store, security, period: str, viewer: dict):
    """Sales forecast for one YYYY-MM period.

    Rows cover the viewer, their role subtree, or everyone for admins.
    Closed Won counts at full amount; open stages count at Amount*Probability.
    """
    import re
    if not re.fullmatch(r"\d{4}-\d{2}", period or ""):
        raise ValueError("period must be YYYY-MM")
    if security.is_admin(viewer):
        user_ids = [u["id"] for u in security.list_users()]
    else:
        user_ids = security.visible_owner_ids(viewer)
    quotas = {q["user_id"]: q["quota"] for q in store.config_all("mf_forecast_quotas")
              if q.get("period") == period}
    names = {u["id"]: u.get("name") or u.get("username") for u in security.list_users()}
    rows = []
    for uid in user_ids:
        closed = 0.0
        weighted = 0.0
        pipeline = []
        for opp in store.query("Opportunity", owner_ids=[uid], limit=10000):
            cd = (opp.get("CloseDate") or "")[:7]
            if cd != period:
                continue
            amt = float(opp.get("Amount") or 0)
            stage = opp.get("Stage") or ""
            if stage == "Closed Won":
                closed += amt
            elif stage == "Closed Lost":
                continue
            else:
                w = amt * float(opp.get("Probability") or 0) / 100.0
                weighted += w
                pipeline.append({"stage": stage, "amount": amt,
                                 "probability": opp.get("Probability")})
        total = round(closed + weighted, 2)
        quota = quotas.get(uid)
        rows.append({"user_id": uid, "user_name": names.get(uid, uid),
                     "quota": quota, "closed_amount": round(closed, 2),
                     "weighted_pipeline": round(weighted, 2),
                     "forecast": total,
                     "attainment": round(total / quota, 4) if quota else None,
                     "pipeline": pipeline})
    rows.sort(key=lambda r: r["user_name"] or "")
    return {"period": period, "rows": rows}


# ------------------------------------------------------------ screen flows
SCREEN_FIELD_TYPES = ("text", "textarea", "number", "email", "phone",
                      "date", "picklist", "checkbox")


def validate_screen_input(screen: dict, inputs: dict):
    """Return (clean, errors) for one screen's submitted values."""
    clean, errors = {}, []
    for f in screen.get("fields") or []:
        name = f.get("name")
        ftype = f.get("type") or "text"
        raw = (inputs or {}).get(name)
        if raw in (None, ""):
            if f.get("required"):
                errors.append(f"{f.get('label') or name} is required")
            clean[name] = None
            continue
        if ftype == "number":
            try:
                clean[name] = float(raw)
            except (TypeError, ValueError):
                errors.append(f"{f.get('label') or name} must be a number")
        elif ftype == "email":
            if "@" not in str(raw):
                errors.append(f"{f.get('label') or name} must be a valid email")
            else:
                clean[name] = str(raw)
        elif ftype == "picklist":
            vals = f.get("picklist_values") or []
            if vals and raw not in vals:
                errors.append(f"{f.get('label') or name} must be one of {vals}")
            else:
                clean[name] = raw
        elif ftype == "checkbox":
            clean[name] = raw in (True, "true", "on", "1", 1)
        elif ftype == "date":
            import re
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(raw)):
                errors.append(f"{f.get('label') or name} must be YYYY-MM-DD")
            else:
                clean[name] = str(raw)
        else:
            clean[name] = str(raw)
    return clean, errors


def _public_screen(screen: dict):
    return {"id": screen.get("id"), "title": screen.get("title"),
            "fields": [{"name": f.get("name"), "label": f.get("label"),
                        "type": f.get("type") or "text",
                        "required": bool(f.get("required")),
                        "picklist_values": f.get("picklist_values") or []}
                       for f in screen.get("fields") or []]}


def start_screen_flow(store, flow: dict, user: dict):
    screens = flow.get("screens") or []
    if not screens:
        raise ValueError("Screen flow has no screens")
    rid = store.config_put("mf_flow_runs", {
        "flow_id": flow["id"], "flow_name": flow.get("name"),
        "user_id": user["id"], "status": "in_progress",
        "current": 0, "values": {}, "created_at": utcnow()})
    return store.config_get("mf_flow_runs", rid)


def advance_screen_flow(store, registry, security, flow: dict, run: dict,
                        inputs: dict, user: dict):
    """Submit one screen. Returns (run, next_screen|None, result)."""
    screens = flow.get("screens") or []
    idx = int(run.get("current") or 0)
    if idx >= len(screens):
        raise ValueError("Flow run is already complete")
    clean, errors = validate_screen_input(screens[idx], inputs)
    if errors:
        return run, _public_screen(screens[idx]), {"ok": False, "errors": errors}
    values = {**(run.get("values") or {}), **clean}
    run["values"] = values
    if idx + 1 < len(screens):
        run["current"] = idx + 1
        store.config_put("mf_flow_runs", run)
        return store.config_get("mf_flow_runs", run["id"]), \
            _public_screen(screens[idx + 1]), {"ok": True}
    # finish: run finish actions with the collected values as the trigger record
    pseudo = {"id": run["id"], "_object": "FlowRun", **values}
    created = []
    for action in flow.get("finish_actions") or []:
        if action.get("type") == "create_record":
            obj = registry.get_object(action.get("object") or "")
            fields = render_value(action.get("fields") or {},
                                  record_context(pseudo), user)
            fclean, ferrs = registry.validate_record(obj, fields) if obj else ({}, ["?"])
            if obj and not ferrs:
                fclean["owner_id"] = user["id"]
                fclean["created_by"] = user["id"]
                fclean.setdefault("record_type",
                                  default_record_type(store, obj["name"]))
                nid = store.insert(obj["name"], fclean)
                created.append({"object": obj["name"], "id": nid})
                new_rec = store.get(obj["name"], nid)
                run_flows(store, registry, security, obj["name"], "create",
                          new_rec, None, user, 1)
        else:
            _run_flow_action(store, registry, security, action, pseudo, user, 1)
    run["status"] = "completed"
    run["completed_at"] = utcnow()
    store.config_put("mf_flow_runs", run)
    return store.config_get("mf_flow_runs", run["id"]), None, \
        {"ok": True, "created": created}
