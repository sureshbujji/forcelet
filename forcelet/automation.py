"""Automation engine: validation rules, flows, approvals, duplicate rules,
field history, record types, and webhook dispatch."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sys
import threading
import urllib.request

from .expressions import eval_expr, record_context, render_value
from . import crypto as _crypto
from . import history_tracking as _history_tracking
from . import queues as _queues
from .store import new_id, utcnow

MAX_FLOW_DEPTH = 3

#: Governor limits for flow execution (per top-level run_flows call, including
#: nested subflows/decisions/loops). A 1,000-row import fires flows per record,
#: so each invocation gets its own budget; when the budget is exhausted the run
#: stops and records a visible __flow_governor__ history entry instead of
#: silently hammering DML and external callouts.
FLOW_ACTION_BUDGET = 500
FLOW_CALLOUT_BUDGET = 10


def _new_flow_budget() -> dict:
    return {"actions": FLOW_ACTION_BUDGET, "callouts": FLOW_CALLOUT_BUDGET,
            "tripped": False}


def _governor_tripped(store, record: dict, user: dict, oname: str, reason: str,
                      budget: dict):
    """Record a visible history entry the first time the budget trips."""
    if budget.get("tripped"):
        return
    budget["tripped"] = True
    try:
        store._execute(
            "INSERT INTO mf_history (id, object_name, record_id, field_name, old_value,"
            " new_value, changed_by, changed_at) VALUES (?,?,?,?,?,?,?,?)",
            (new_id(), oname, record.get("id"), "__flow_governor__", None,
             f"Flow execution stopped: {reason}", user["id"], utcnow()),
        )
        store._commit()
    except Exception:
        pass


def _check_flow_budget(store, record: dict, user: dict, oname: str,
                       budget: dict, callout: bool = False) -> bool:
    """Consume one unit of budget. Returns False when exhausted (and records it)."""
    if budget is None:
        return True
    key = "callouts" if callout else "actions"
    if budget[key] <= 0:
        _governor_tripped(store, record, user, oname,
                          f"{key} budget exhausted "
                          f"({FLOW_CALLOUT_BUDGET if callout else FLOW_ACTION_BUDGET} max)",
                          budget)
        return False
    budget[key] -= 1
    return True


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
    tracked = _history_tracking.tracked_field_names(store, obj_name)
    if tracked is not None and not tracked:
        return  # field history tracking disabled for this object
    for key, new_val in new.items():
        if tracked is not None and key not in tracked:
            continue  # field not selected for tracking
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
              record: dict, old_record: dict | None, user: dict, depth: int = 0,
              _budget: dict | None = None):
    """Run matching flows. event in ('create', 'update'). Runs in system mode.

    ``_budget`` threads the governor through nested subflows/decisions/loops;
    a fresh budget is created per top-level call.
    """
    if depth >= MAX_FLOW_DEPTH:
        return
    budget = _budget if _budget is not None else _new_flow_budget()
    for flow in store.config_all("mf_flows"):
        if not flow.get("active", True) or flow.get("object") != obj_name:
            continue
        trigger = flow.get("trigger", "on_create_or_update")
        if trigger == "none":
            continue  # subflow-only: invoked via {type: subflow}, never by trigger
        if trigger == "on_create" and event != "create":
            continue
        if trigger == "on_update" and event != "update":
            continue
        try:
            cond = flow.get("condition")
            if cond and not eval_expr(cond, record_context(record),
                                      record_context(old_record)):
                continue
        except Exception:
            continue
        _run_action_list(store, registry, security, flow.get("actions") or [],
                         record, user, depth, obj_name, budget,
                         flow.get("id"), flow.get("name"))


class _FlowWait(Exception):
    """Raised by a wait action to pause the enclosing action list.

    Carries the resume time and a snapshot of the working record; the
    list runner persists the *remaining* actions and returns.
    """
    def __init__(self, resume_at, record_snapshot: dict):
        super().__init__("flow wait")
        self.resume_at = resume_at
        self.record_snapshot = record_snapshot


def _json_safe(value):
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return {}


def _wait_resume_at(action: dict):
    """Compute the resume datetime for a wait action, or None if invalid/past."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    until = action.get("until")
    if until:
        try:
            dt = datetime.fromisoformat(str(until))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt if dt > now else None
        except ValueError:
            return None
    minutes = (action.get("duration_minutes")
               or (action.get("duration_hours") or 0) * 60
               or (action.get("duration_days") or 0) * 1440)
    try:
        minutes = float(minutes)
    except (TypeError, ValueError):
        return None
    if minutes <= 0:
        return None
    return now + timedelta(minutes=minutes)


def _persist_flow_wait(store, resume_at, remaining: list, record: dict,
                       user: dict, obj_name: str | None,
                       flow_id: str | None, flow_name: str | None):
    """Persist a paused flow action list for later resume by the scheduler."""
    row = {
        "id": new_id(),
        "flow_id": flow_id,
        "flow_name": flow_name,
        "obj_name": obj_name,
        "record_id": record.get("id"),
        "actions": _json_safe(remaining),
        "record": _json_safe(record),
        "user_id": user.get("id"),
        "resume_at": resume_at.isoformat(),
        "created_at": utcnow(),
    }
    store.config_put("mf_flow_waits", row)
    return row["id"]


def _run_action_list(store, registry, security, actions: list, record: dict,
                     user: dict, depth: int, obj_name: str | None,
                     budget: dict, flow_id: str | None = None,
                     flow_name: str | None = None):
    """Execute an action list in order.

    A ``wait`` action raises _FlowWait; the remaining actions are persisted
    to mf_flow_waits and the list stops here (resume via process_due_flow_waits).
    """
    actions = actions or []
    i = 0
    while i < len(actions):
        try:
            _run_flow_action(store, registry, security, actions[i], record,
                             user, depth, obj_name, budget)
        except _FlowWait as w:
            _persist_flow_wait(store, w.resume_at, actions[i + 1:], w.record_snapshot,
                               user, obj_name, flow_id, flow_name)
            return
        i += 1


def process_due_flow_waits(store, registry, security) -> list:
    """Resume flow action lists whose wait has elapsed. Returns resumed wait ids.

    Called from the scheduler tick. Each wait runs with a fresh governor
    budget; a missing/deactivated user or a corrupt row retires the wait.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    resumed = []
    for w in store.config_all("mf_flow_waits"):
        try:
            resume_at = datetime.fromisoformat(w.get("resume_at") or "")
            if resume_at.tzinfo is None:
                resume_at = resume_at.replace(tzinfo=timezone.utc)
        except ValueError:
            store.config_delete("mf_flow_waits", w.get("id"))
            continue
        if resume_at > now:
            continue
        user = security.get_user(w.get("user_id") or "")
        if not user or not user.get("is_active", True):
            store.config_delete("mf_flow_waits", w.get("id"))
            continue
        try:
            record = dict(w.get("record") or {})
            _run_action_list(store, registry, security, w.get("actions") or [],
                             record, user, 0, w.get("obj_name"),
                             _new_flow_budget(), w.get("flow_id"), w.get("flow_name"))
        except Exception:
            pass
        store.config_delete("mf_flow_waits", w.get("id"))
        resumed.append(w.get("id"))
    return resumed


#: Every action type the flow engine executes. Unknown types are rejected at
#: save time by validate_flow_actions (they used to be silently ignored).
FLOW_ACTION_TYPES = (
    "set_fields", "create_record", "log", "http_callout",
    "send_notification", "decision", "subflow", "invocable",
    "loop", "get_records", "assignment", "delete_record", "wait",
)


def validate_flow_actions(actions) -> list:
    """Return error strings for unknown flow action types (recursive).

    Walks nested decision outcomes and loop bodies; subflow bodies are
    validated when the referenced flow itself is saved.
    """
    errors = []

    def walk(acts, path):
        for i, a in enumerate(acts or []):
            a = a or {}
            atype = a.get("type")
            where = f"{path}[{i}]"
            if atype not in FLOW_ACTION_TYPES:
                errors.append(f"{where}: unknown action type {atype!r}")
                continue
            if atype == "decision":
                for oi, o in enumerate(a.get("outcomes") or []):
                    walk((o or {}).get("actions"),
                         f"{where}.outcomes[{oi}].actions")
                walk(a.get("default_actions"), f"{where}.default_actions")
            elif atype == "loop":
                walk(a.get("actions"), f"{where}.actions")

    walk(actions, "actions")
    return errors


def _run_flow_action(store, registry, security, action: dict, record: dict, user: dict,
                     depth: int, obj_name: str | None = None,
                     _budget: dict | None = None):
    oname = obj_name or record.get("_object", "?")
    if not _check_flow_budget(store, record, user, oname, _budget):
        return
    atype = action.get("type")
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
        if not _check_flow_budget(store, record, user, oname, _budget, callout=True):
            return
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
    elif atype == "send_notification":
        send_custom_notification(store, security, {
            "notification_type": action.get("notification_type"),
            "title": render_value(action.get("title") or "", record_context(record), user),
            "body": render_value(action.get("body") or "", record_context(record), user),
            "recipients": render_value(action.get("recipients") or {}, record_context(record), user),
            "object_name": oname,
            "record_id": record.get("id"),
        }, record, user)
    elif atype == "decision":
        # Branching: first outcome whose condition is true runs its actions.
        # {type: decision, outcomes: [{label, condition, actions}], default_actions: []}
        ctx = record_context(record)
        matched = False
        for outcome in action.get("outcomes") or []:
            try:
                if eval_expr(outcome.get("condition") or {}, ctx, {}):
                    matched = True
                    _run_action_list(store, registry, security, outcome.get("actions"),
                                     record, user, depth, obj_name, _budget)
                    break
            except Exception:
                continue
        if not matched:
            _run_action_list(store, registry, security, action.get("default_actions"),
                             record, user, depth, obj_name, _budget)
    elif atype == "subflow":
        # Invoke another flow's actions with mapped inputs.
        # {type: subflow, flow: <id|name>, inputs: {var: template}}
        sub = _find_flow(store, action.get("flow"))
        if sub and sub.get("active", True) and depth + 1 < MAX_FLOW_DEPTH:
            inputs = render_value(action.get("inputs") or {},
                                  record_context(record), user)
            sub_record = {**record, **inputs}  # inputs as {{Trigger.<var>}}
            _run_action_list(store, registry, security, sub.get("actions"),
                             sub_record, user, depth + 1, obj_name, _budget,
                             sub.get("id"), sub.get("name"))
    elif atype == "invocable":
        # Call a code-registered invocable action.
        # {type: invocable, name: <action name>, inputs: {...}}
        fn = INVOCABLE_ACTIONS.get(action.get("name") or "")
        if fn:
            inputs = render_value(action.get("inputs") or {},
                                  record_context(record), user)
            try:
                outputs = fn({"record": record, "inputs": inputs,
                              "user": user, "store": store,
                              "registry": registry, "security": security}) or {}
            except Exception:
                outputs = {}
            if isinstance(outputs, dict):
                record.update(outputs)  # outputs feed subsequent actions
    elif atype == "loop":
        # Iterate over a collection, running sub-actions per item.
        # {type: loop, collection: "{{Trigger.items}}" | [...], item_var: "item",
        #  actions: [...]}. Governor budget caps runaway iterations.
        raw = action.get("collection")
        if isinstance(raw, str):
            rendered = render_value(raw, record_context(record), user)
            if isinstance(rendered, list):
                items = rendered
            else:
                try:
                    items = json.loads(rendered) if isinstance(rendered, str) else []
                except Exception:
                    items = []
        elif isinstance(raw, list):
            items = raw
        else:
            items = []
        item_var = action.get("item_var") or "item"
        for item in items:
            loop_record = {**record, item_var: item}
            _run_action_list(store, registry, security, action.get("actions"),
                             loop_record, user, depth, obj_name, _budget)
    elif atype == "get_records":
        # Query records into a flow variable.
        # {type: get_records, object: "Contact", filters: {AccountId: "{{Trigger.Id}}"},
        #  limit: 50, variable: "contacts"}
        obj = registry.get_object(action.get("object") or "")
        if obj:
            filters = render_value(action.get("filters") or {},
                                   record_context(record), user)
            try:
                limit = max(1, min(int(action.get("limit") or 100), 500))
            except (TypeError, ValueError):
                limit = 100
            rows = []
            for r in store.query(obj["name"], limit=10000):
                if all(str(r.get(k)) == str(v) for k, v in filters.items()):
                    rows.append(r)
                    if len(rows) >= limit:
                        break
            record[action.get("variable") or "records"] = rows
    elif atype == "assignment":
        # Set a flow variable on the working record.
        # {type: assignment, variable: "total", value: "{{Trigger.Amount}}"}
        var = action.get("variable")
        if var:
            record[var] = render_value(action.get("value"),
                                       record_context(record), user)
    elif atype == "delete_record":
        # Delete a record by id (runs delete triggers + emits change).
        # {type: delete_record, object: "Task", record_id: "{{Trigger.task_id}}"}
        obj = registry.get_object(action.get("object") or "")
        target_id = render_value(action.get("record_id") or "",
                                 record_context(record), user)
        if obj and target_id:
            rec = store.get(obj["name"], target_id)
            if rec:
                errs = run_triggers(store, registry, security, obj["name"],
                                    "before_delete", rec, None, user, depth + 1)
                if not errs:
                    store.delete(obj["name"], target_id)
                    run_triggers(store, registry, security, obj["name"],
                                 "after_delete", rec, None, user, depth + 1)
                    store.emit_change(obj["name"], target_id, "delete", user)
    elif atype == "wait":
        # Pause the flow; remaining actions resume via the scheduler.
        # {type: wait, duration_minutes: 60} | {duration_hours} | {duration_days}
        # | {until: "<iso datetime>"}. A past/invalid wait is a no-op.
        resume_at = _wait_resume_at(action)
        if resume_at is not None:
            raise _FlowWait(resume_at, dict(record))


def _find_flow(store, ref: str):
    """Find a flow by id or name."""
    if not ref:
        return None
    flow = store.config_get("mf_flows", ref)
    if flow:
        return flow
    for cand in store.config_all("mf_flows"):
        if cand.get("name") == ref:
            return cand
    return None


# ------------------------------------------------------------ invocable actions
# Code-registered actions callable from flows via {type: invocable, name, inputs}.
# Handlers receive {"record", "inputs", "user", "store", "registry", "security"}
# and return a dict of outputs merged into the flow's working record.
INVOCABLE_ACTIONS: dict = {}


def register_invocable_action(name: str, fn):
    INVOCABLE_ACTIONS[name] = fn
    return fn


def list_invocable_actions() -> list:
    return sorted(INVOCABLE_ACTIONS)


def _invocable_convert_currency(ctx):
    from . import currency as _cur
    store, inputs = ctx["store"], ctx["inputs"]
    try:
        return {"converted_amount": _cur.convert(
            store, inputs.get("amount"), inputs.get("from"),
            inputs.get("to"), inputs.get("date"))}
    except (ValueError, TypeError):
        return {"converted_amount": None}


register_invocable_action("Convert Currency", _invocable_convert_currency)


# ------------------------------------------------------------ approvals
def find_approval_process(store, obj_name: str, record: dict):
    for proc in store.config_all("mf_approval_processes"):
        if not proc.get("active", True) or proc.get("object") != obj_name:
            continue
        try:
            conds = proc.get("entry_conditions")
            if not conds or eval_expr(conds, record_context(record)):
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
    # Auto-skip steps whose skip_if condition is already true.
    idx = 0
    while idx < len(steps) and _step_skipped(store, steps[idx], record):
        req["history"].append({"by": "system", "at": utcnow(), "step": idx,
                               "step_name": steps[idx].get("name"),
                               "decision": "Skipped"})
        idx += 1
    req["current_step"] = idx
    if idx >= len(steps):
        req["status"] = "Approved"  # every step skipped: auto-approved
    rid = store.config_put("mf_approval_requests", req)
    saved = store.config_get("mf_approval_requests", rid)
    if saved.get("status") == "Pending":
        _notify_approvers(store, security, saved, obj_name, record, user)
    return saved, None


def _notify_approvers(store, security, req: dict, obj_name: str,
                      record: dict, user: dict):
    for uid in _approver_ids(store, security, req):
        if uid != user["id"]:
            store.notify(uid, "approval",
                         f"Approval requested: {obj_name}",
                         f"{user.get('name')} submitted a {obj_name} record "
                         f"({req.get('process_name')}) for approval.",
                         obj_name, record["id"])


def _approver_ids(store, security, req: dict):
    """User ids allowed to act on the current step.

    Approver specs:
      {"type": "user", "id": <user id>}
      {"type": "role", "role": <role name>}        — role + subordinates
      {"type": "queue", "id"|"name": ...}          — queue members
      {"type": "field", "field": <field api name>} — user id from a lookup
                                                     field on the record
      "manager"                                     — record owner's manager
    """
    step = (req.get("steps") or [])[req.get("current_step", 0)] or {}
    approver = step.get("approver")
    record = None
    try:
        row = store._execute(
            f"SELECT * FROM {store._table(req['object'])} WHERE id=?", (req["record_id"],)
        ).fetchone()
        record = dict(row) if row else None
    except Exception:
        record = None
    owner_id = record.get("owner_id") if record else None
    ids = set()
    if isinstance(approver, dict):
        atype = approver.get("type")
        if atype == "user":
            if security.get_user(approver.get("id")):
                ids.add(approver.get("id"))
        elif atype == "role":
            try:
                subtree = security._role_subtree(approver.get("role"))
            except Exception:
                subtree = set()
            ids.update(u["id"] for u in security.list_users() if u.get("role") in subtree)
        elif atype == "queue":
            qref = approver.get("id") or approver.get("name")
            ids.update(_queues.queue_member_ids(store, security, qref))
        elif atype == "field" and record:
            uid = record.get(approver.get("field"))
            if uid and security.get_user(uid):
                ids.add(uid)
    elif approver == "manager" and owner_id:
        owner = security.get_user(owner_id)
        if owner and owner.get("role"):
            role = security.get_role(owner["role"])
            parent = role.get("parent") if role else None
            if parent:
                ids.update(u["id"] for u in security.list_users() if u.get("role") == parent)
    return ids


def _step_skipped(store, step: dict, record: dict) -> bool:
    """A step with a skip_if condition is skipped when it evaluates true."""
    cond = step.get("skip_if")
    if not cond:
        return False
    try:
        return bool(eval_expr(cond, record_context(record)))
    except Exception:
        return False


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
    steps = req.get("steps") or []
    cur = req.get("current_step", 0)
    step_name = (steps[cur] or {}).get("name") if cur < len(steps) else None
    req["history"].append({"by": user["id"], "at": utcnow(), "step": cur,
                           "step_name": step_name,
                           "decision": "Approved" if approve else "Rejected",
                           "comment": comment})
    if not approve:
        req["status"] = "Rejected"
    else:
        # Advance through any remaining steps (honoring skip_if); the request
        # is Approved only after the final step approves.
        nxt = cur + 1
        record = None
        try:
            record = store.get(req["object"], req["record_id"])
        except Exception:
            record = None
        while nxt < len(steps) and record is not None and _step_skipped(store, steps[nxt], record):
            req["history"].append({"by": "system", "at": utcnow(), "step": nxt,
                                   "step_name": steps[nxt].get("name"),
                                   "decision": "Skipped"})
            nxt += 1
        if nxt >= len(steps):
            req["status"] = "Approved"
        else:
            req["current_step"] = nxt
            store.config_put("mf_approval_requests", req)
            saved = store.config_get("mf_approval_requests", request_id)
            try:
                rec = record or {}
                _notify_approvers(store, security, saved, req["object"], rec, user)
            except Exception:
                pass
            return saved, None
    store.config_put("mf_approval_requests", req)
    return req, None


# ------------------------------------------------------------ custom notifications
NOTIFICATION_TYPE_TABLE = "mf_notification_types"


def get_notification_type(store, ref: str):
    """Fetch a notification type by id or by name."""
    if not ref:
        return None
    nt = store.config_get(NOTIFICATION_TYPE_TABLE, ref)
    if nt:
        return nt
    for cand in store.config_all(NOTIFICATION_TYPE_TABLE):
        if cand.get("name") == ref:
            return cand
    return None


def validate_notification_type(defn: dict) -> str | None:
    if not (defn.get("name") or "").strip():
        return "Notification type name is required"
    return None


def resolve_notification_recipients(store, security, recipients: dict,
                                    record: dict | None, user: dict) -> set:
    """Resolve a recipient spec to user ids.

    recipients: {"users": [ids], "roles": [role names], "queues": [ids/names],
                 "owner": bool, "submitter": bool}
    """
    recipients = recipients or {}
    ids = set()
    for uid in recipients.get("users") or []:
        if security.get_user(uid):
            ids.add(uid)
    for role_name in recipients.get("roles") or []:
        try:
            subtree = security._role_subtree(role_name)
        except Exception:
            subtree = set()
        ids.update(u["id"] for u in security.list_users()
                   if u.get("role") in subtree)
    for qref in recipients.get("queues") or []:
        ids.update(_queues.queue_member_ids(store, security, qref))
    if recipients.get("owner") and record:
        owner_id = record.get("owner_id")
        if owner_id and security.get_user(owner_id):
            ids.add(owner_id)
    if recipients.get("submitter") and user:
        ids.add(user["id"])
    return ids


def send_custom_notification(store, security, spec: dict,
                             record: dict | None, user: dict) -> dict:
    """Send a custom notification to resolved recipients.

    spec: {"notification_type": <id|name>, "title": <override?>,
           "body": <override?>, "recipients": {...},
           "object_name": <override?>, "record_id": <override?>}
    Returns {"sent": n, "recipients": [ids]}.
    """
    nt = get_notification_type(store, spec.get("notification_type") or "")
    if nt and not nt.get("active", True):
        return {"sent": 0, "recipients": [], "skipped": "inactive type"}
    ctx = record_context(record) if record else {}
    title_tpl = spec.get("title") or (nt.get("title_template") if nt else "") or "Notification"
    body_tpl = spec.get("body") or (nt.get("body_template") if nt else "") or ""
    title = render_value(title_tpl, ctx, user)
    body = render_value(body_tpl, ctx, user)
    recipient_ids = resolve_notification_recipients(
        store, security, spec.get("recipients") or {}, record, user)
    ntype = f"custom:{nt.get('name')}" if nt else "custom"
    sent = []
    for uid in sorted(recipient_ids):
        store.notify(uid, ntype, title, body,
                     spec.get("object_name") or (record.get("_object") if record else None),
                     spec.get("record_id") or (record.get("id") if record else None))
        sent.append(uid)
    return {"sent": len(sent), "recipients": sent}


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


# ------------------------------------------------------------ unified save pipeline
# The API save path (_do_create/_do_update in api/_shared.py) and the system
# DML used by bulk ingest and trigger code (_trigger_dml_ops below) must run
# the same save pipeline. The helpers here implement the steps the system
# path used to skip: duplicate-rule blocking, assignment rules, AutoNumber
# assignment, divisions, case milestones, escalation rules, auto-responses,
# webhooks, approval auto-submit, change events, email alerts and roll-ups.
# Heavy API-only modules are imported lazily to avoid import cycles.


def _fls_payload(security, user: dict, obj: dict, rec: dict) -> dict:
    """Record payload filtered to fields the user may read (for webhooks)."""
    try:
        readable = set(security.readable_fields(user, obj))
    except Exception:
        readable = set()
    return {k: v for k, v in rec.items()
            if (k in readable or k in ("id", "owner_id", "created_by"))
            and not _crypto.is_encrypted(v)}


def _assert_not_locked(store, security, obj_name: str, rid: str, user: dict):
    if pending_request_for(store, obj_name, rid) \
            and not security.is_admin(user):
        raise TriggerAbort("Record is locked: an approval request is pending")


def _assert_no_duplicate_block(store, obj_name: str, record: dict,
                               event: str, exclude_id: str | None = None):
    from . import duplicate_rules as _duprules
    dup_action, dup_message = _duprules.evaluate_duplicate_rules(
        store, obj_name, event, record, exclude_id=exclude_id)
    if dup_action == "block":
        raise TriggerAbort(dup_message)


def apply_system_create_defaults(store, registry, security, obj_name: str,
                                 clean: dict, user: dict):
    """Pre-insert steps for system DML: duplicate blocking, assignment, AutoNumber."""
    from . import datamodel as _datamodel
    obj = registry.get_object(obj_name)
    _assert_no_duplicate_block(store, obj_name, clean, "create")
    clean["owner_id"] = (apply_assignment_rules(
        store, registry, security, obj_name, clean, user) or user["id"])
    clean["created_by"] = user["id"]
    clean.setdefault("record_type", default_record_type(store, obj_name))
    for f in obj.get("fields", []):
        if f.get("type") == "AutoNumber" and f.get("active") is not False:
            clean[f["name"]] = _datamodel.next_auto_number(store, obj_name, f)


def _post_insert_record_admin(store, registry, security, obj_name: str,
                              rid: str, user: dict) -> dict:
    """Division stamping, case milestones and owner notification (pre-after-triggers)."""
    try:
        from . import divisions as _divisions
        user_div = _divisions.user_division_id(store, security, user)
        if user_div:
            _divisions.set_record_division(store, obj_name, rid, user_div)
    except Exception:
        pass  # division stamping must never break the save pipeline
    rec = store.get(obj_name, rid)
    try:
        start_case_milestones(store, obj_name, rec)
    except Exception:
        pass
    if rec.get("owner_id") and rec["owner_id"] != user["id"]:
        owner = security.get_user(rec["owner_id"])
        if owner:
            label = rec.get("Name") or rec.get("Subject") or rid
            try:
                store.notify(owner["id"], "assignment",
                             f"{obj_name} assigned to you",
                             f"{label} was assigned to you by {user.get('name')}.",
                             obj_name, rid)
            except Exception:
                pass
    return rec


def run_create_automation(store, registry, security, obj_name: str,
                          rec: dict, user: dict):
    """Post-insert automation shared by the API and system DML paths.

    Escalation, auto-responses, webhooks, approval auto-submit, change event,
    email alerts and roll-ups. Every step is guarded so a downstream failure
    never breaks the save.
    """
    obj = registry.get_object(obj_name)
    try:
        apply_escalation_rules(store, registry, security, obj_name, rec, None, user)
    except Exception:
        pass
    try:
        run_auto_responses(store, registry, security, obj_name, rec, user)
    except Exception:
        pass
    try:
        dispatch_webhooks(store, obj_name, "create",
                          _fls_payload(security, user, obj, rec), user)
    except Exception:
        pass
    try:
        submit_for_approval(store, security, obj_name, rec, user)
    except Exception:
        pass
    try:
        store.emit_change(obj_name, rec["id"], "create", user,
                          snapshot={k: v for k, v in rec.items()
                                    if not _crypto.is_encrypted(v)})
    except Exception:
        pass
    try:
        from . import email_alerts as _emailalerts
        _emailalerts.fire_email_alerts(store, obj_name, "Create", rec,
                                       user=user, security=security)
    except Exception:
        pass
    try:
        from .api import _shared as _api_shared  # lazy: _shared imports automation
        _api_shared.recompute_stored_rollups(user, obj_name, rec)
    except Exception:
        pass


def run_update_post_automation(store, registry, security, obj_name: str,
                               new_rec: dict, old_rec: dict, user: dict,
                               changed_fields: list):
    """Post-update automation shared by the API and system DML paths.

    Runs after after_update triggers and flows: webhooks, change event,
    email alerts and roll-ups. Every step is guarded.
    """
    obj = registry.get_object(obj_name)
    try:
        dispatch_webhooks(store, obj_name, "update",
                          _fls_payload(security, user, obj, new_rec), user)
    except Exception:
        pass
    try:
        store.emit_change(obj_name, new_rec["id"], "update", user,
                          changed_fields=changed_fields,
                          snapshot={k: v for k, v in new_rec.items()
                                    if not _crypto.is_encrypted(v)})
    except Exception:
        pass
    try:
        from . import email_alerts as _emailalerts
        _emailalerts.fire_email_alerts(store, obj_name, "Update", new_rec,
                                       user=user, security=security)
    except Exception:
        pass
    try:
        from .api import _shared as _api_shared  # lazy: _shared imports automation
        _api_shared.recompute_stored_rollups(user, obj_name, new_rec,
                                             old_rec=old_rec)
    except Exception:
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
        # Unified pipeline: duplicate blocking, assignment, AutoNumber —
        # the same pre-insert steps the API _do_create path runs.
        apply_system_create_defaults(store, registry, security, obj_name,
                                     clean, user)
        errs = run_triggers(store, registry, security, obj_name,
                            "before_insert", clean, None, user, depth + 1)
        if errs:
            raise TriggerAbort("; ".join(errs))
        vr = check_validation_rules(store, obj_name, clean)
        if vr:
            raise TriggerAbort("; ".join(vr))
        rid = store.insert(obj_name, clean)
        rec = _post_insert_record_admin(store, registry, security,
                                        obj_name, rid, user)
        errs = run_triggers(store, registry, security, obj_name,
                            "after_insert", rec, None, user, depth + 1)
        if errs:
            store.delete(obj_name, rid)
            raise TriggerAbort("; ".join(errs))
        run_flows(store, registry, security, obj_name, "create", rec, None, user, depth + 1)
        run_create_automation(store, registry, security, obj_name, rec, user)
        return rec

    def update(obj_name, rid, fields):
        obj = registry.get_object(obj_name)
        rec = obj and store.get(obj_name, rid)
        if not rec:
            raise TriggerAbort("Record not found")
        if not security.can(user, "edit", obj_name) \
                or not security.can_see_record(user, rec, obj_name):
            raise TriggerAbort(f"No edit access on {obj_name}")
        _assert_not_locked(store, security, obj_name, rid, user)
        clean, verrs = registry.validate_record(obj, dict(fields), partial=True)
        if verrs:
            raise TriggerAbort("; ".join(verrs))
        merged = {**rec, **clean}
        errs = run_triggers(store, registry, security, obj_name,
                            "before_update", merged, rec, user, depth + 1)
        if errs:
            raise TriggerAbort("; ".join(errs))
        # Unified pipeline: duplicate blocking then validation rules — the
        # same order the API _do_update path uses.
        _assert_no_duplicate_block(store, obj_name, merged, "update",
                                   exclude_id=rid)
        vr = check_validation_rules(store, obj_name, merged, rec)
        if vr:
            raise TriggerAbort("; ".join(vr))
        log_history(store, obj_name, rid, rec, merged, user)
        store.update(obj_name, rid, {k: merged[k] for k in clean})
        new_rec = store.get(obj_name, rid)
        try:
            complete_case_milestones(store, obj_name, new_rec, rec)
        except Exception:
            pass
        try:
            apply_escalation_rules(store, registry, security, obj_name,
                                   new_rec, rec, user)
        except Exception:
            pass
        errs = run_triggers(store, registry, security, obj_name,
                            "after_update", new_rec, rec, user, depth + 1)
        if errs:
            store.update(obj_name, rid, {k: rec[k] for k in clean if k in rec})
            raise TriggerAbort("; ".join(errs))
        run_flows(store, registry, security, obj_name, "update", new_rec, rec, user, depth + 1)
        run_update_post_automation(store, registry, security, obj_name,
                                   new_rec, rec, user, list(clean.keys()))
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
        cron_expr = (job.get("cron") or "").strip()
        last = job.get("last_run")
        if cron_expr:
            # cron-scheduled job: due when the schedule has an unconsumed occurrence
            from . import cron as _cron
            try:
                due = _cron.is_due(cron_expr, last, now)
            except ValueError as e:
                store.log_scheduled_run(job["id"], "error",
                                        f"bad cron expression {cron_expr!r}: {e}"[:2000])
                continue
        else:
            interval = int(job.get("interval_minutes") or 1440)
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


# ------------------------------------------------------------ scheduled flows
SCHEDULED_FLOW_BATCH_CAP = 200
_SCHED_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def validate_schedule(schedule) -> str | None:
    """Return an error message for a bad schedule dict, else None."""
    if not isinstance(schedule, dict):
        return "schedule must be an object"
    freq = schedule.get("frequency")
    if freq not in ("daily", "weekly"):
        return "schedule.frequency must be 'daily' or 'weekly'"
    if not _SCHED_TIME_RE.match(str(schedule.get("time") or "")):
        return "schedule.time must be HH:MM (24-hour)"
    if "day_of_week" in schedule:
        dow = schedule.get("day_of_week")
        if isinstance(dow, bool) or not isinstance(dow, int) or not 0 <= dow <= 6:
            return "schedule.day_of_week must be an integer 0-6 (Monday=0)"
    return None


def validate_scheduled_flow(flow: dict, registry) -> str | None:
    """Return an error message for a bad scheduled-flow definition, else None."""
    if flow.get("flow_type") == "screen":
        return "screen flows cannot use the scheduled trigger"
    if not registry.get_object(flow.get("object") or ""):
        return f"Unknown object '{flow.get('object')}'"
    err = validate_schedule(flow.get("schedule"))
    if err:
        return err
    cond = flow.get("condition")
    if cond is not None and not isinstance(cond, dict):
        return "condition must be an object"
    if not isinstance(flow.get("actions") or [], list):
        return "actions must be a list"
    return None


def compute_next_run(schedule: dict, from_dt=None) -> str:
    """Next scheduled run as an ISO-8601 UTC string.

    Schedule times are interpreted in UTC (consistent with cron-scheduled
    jobs). For weekly, day_of_week is 0-6 with Monday=0 (matches
    datetime.weekday()); a missing day_of_week defaults to Monday.
    """
    from datetime import datetime, timedelta, timezone
    now = from_dt or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    m = _SCHED_TIME_RE.match(str(schedule.get("time") or "00:00"))
    hh, mm = (int(m.group(1)), int(m.group(2))) if m else (0, 0)

    def _at(day):
        return day.replace(hour=hh, minute=mm, second=0, microsecond=0)

    if schedule.get("frequency") == "weekly":
        dow = schedule.get("day_of_week", 1)
        cand = _at(now + timedelta(days=(dow - now.weekday()) % 7))
        if cand <= now:
            cand += timedelta(days=7)
        return cand.isoformat(timespec="seconds")
    cand = _at(now)
    if cand <= now:
        cand += timedelta(days=1)
    return cand.isoformat(timespec="seconds")


def run_due_scheduled_flows(store, registry, security) -> list:
    """Run active scheduled flows whose next_run has passed.

    Queries up to SCHEDULED_FLOW_BATCH_CAP records per flow, evaluates the
    flow's condition per record, and executes its actions (reusing the
    record-triggered flow action executor) as the admin user. next_run is
    claimed (advanced) BEFORE the batch executes so a second scheduler
    runner cannot double-execute; combined with the scheduler's
    single-runner lock this makes runs idempotent.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    users = {u["username"]: u for u in store.meta_all("mf_users")}
    run_as = users.get("admin")
    if not run_as:
        return []
    results = []
    for flow in store.config_all("mf_flows"):
        if not flow.get("active", True) or flow.get("trigger") != "scheduled":
            continue
        if flow.get("flow_type") == "screen":
            continue
        obj_name = flow.get("object") or ""
        if not registry.get_object(obj_name):
            continue
        nxt = flow.get("next_run")
        if not nxt:
            # First sighting: initialize the schedule without backfilling.
            flow["next_run"] = compute_next_run(flow.get("schedule") or {}, now)
            store.config_put("mf_flows", flow)
            continue
        try:
            due_at = datetime.fromisoformat(nxt)
        except Exception:
            due_at = now
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=timezone.utc)
        if due_at > now:
            continue
        # Claim the next run before executing (idempotency).
        flow["next_run"] = compute_next_run(flow.get("schedule") or {}, now)
        store.config_put("mf_flows", flow)
        matched = executed = errors = 0
        try:
            condition = flow.get("condition") or {}
            for rec in store.query(obj_name, limit=SCHEDULED_FLOW_BATCH_CAP):
                try:
                    if not eval_expr(condition, record_context(rec)):
                        continue
                except Exception:
                    continue
                matched += 1
                try:
                    for action in flow.get("actions") or []:
                        _run_flow_action(store, registry, security, action,
                                         rec, run_as, 0, obj_name)
                    executed += 1
                except Exception:
                    errors += 1
            store.log_scheduled_run(
                flow["id"], "ok",
                f"matched={matched} executed={executed} errors={errors}")
        except Exception as e:
            errors += 1
            store.log_scheduled_run(flow["id"], "error", str(e)[:2000])
        results.append({"flow": flow.get("name"), "flow_id": flow["id"],
                        "matched": matched, "executed": executed,
                        "errors": errors, "next_run": flow["next_run"]})
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
    "rollup_rules": ("mf_rollup_rules",
                     ("child_object", "parent_object", "parent_field")),
    "lead_field_mappings": ("mf_lead_field_mappings",
                            ("target_object", "target_field")),
    "web_to_forms": ("mf_web_to_forms", ("key",)),
    "named_credentials": ("mf_named_credentials", ("name",)),
    "forecast_quotas": ("mf_forecast_quotas", ("user_id", "period")),
    "forecast_types": ("mf_forecast_types", ("name",)),
    "apps": ("mf_apps", ("name",)),
    "custom_settings": ("mf_custom_settings", ("name",)),
    "external_objects": ("mf_external_objects", ("api_name",)),
}


def build_package(store, registry, namespace=None, version=None,
                  managed: bool = False) -> dict:
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
           "namespace": (namespace or "").strip() or None,
           "version": (version or "").strip() or None,
           "managed": bool(managed),
           "custom_objects": custom_objects,
           "standard_object_fields": extra_fields,
           "layouts": store.layouts_all(),
           "config": {kind: store.config_all(table)
                      for kind, (table, _keys) in PACKAGE_TABLES.items()}}
    from . import devops as _devops  # lazy: devops imports automation
    pkg["custom_metadata"] = _devops.export_custom_metadata_package(store)
    return pkg


def import_package(store, registry, pkg: dict, user: dict) -> dict:
    """Install a package: upserts objects, fields, layouts and config by natural key."""
    if not isinstance(pkg, dict) or pkg.get("package_version") != 1:
        raise ValueError("Not a forcelet package (package_version must be 1)")
    from . import devops as _devops  # lazy: devops imports automation
    install = _devops.check_package_install(store, pkg)  # raises on downgrade
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
    if pkg.get("custom_metadata"):
        summary["custom_metadata"] = _devops.import_custom_metadata_package(
            store, user, pkg["custom_metadata"])
    if install:
        summary["installed_package"] = _devops.record_package_install(
            store, user, pkg, {k: v for k, v in summary.items()})
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
                 record_id: str | None, body: str, record_mentions: bool = True):
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
    if record_mentions:
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


def lead_field_mappings(store):
    """Active declarative lead-conversion mappings.

    Returns {target_object: {target_field: lead_field}} for rows in
    mf_lead_field_mappings. Custom mappings override the built-in defaults
    in convert_lead (explicit API ``options`` overrides win over both).
    """
    out = {}
    try:
        rows = store.config_all("mf_lead_field_mappings")
    except Exception:
        return out
    for m in rows:
        if not m.get("active", True):
            continue
        if m.get("target_object") and m.get("target_field") and m.get("lead_field"):
            out.setdefault(m["target_object"], {})[m["target_field"]] = m["lead_field"]
    return out


def _apply_lead_mappings(lead, target_fields, mappings, skip=()):
    """Overlay custom lead->target mappings onto ``target_fields``.

    Only non-empty lead values are copied; ``skip`` fields (system-managed
    links like AccountId) are never remapped.
    """
    for target_field, lead_field in mappings.items():
        if target_field in skip:
            continue
        val = lead.get(lead_field)
        if val not in (None, ""):
            target_fields[target_field] = val
    return target_fields


def convert_lead(store, registry, security, lead_id: str, user: dict,
                 options: dict | None = None):
    """Convert a Lead into Account + Contact (+ Opportunity).

    options: {"account_name", "contact": {...overrides},
              "opportunity_name", "create_opportunity": bool,
              "opportunity": {...overrides}}
    Field values come from built-in defaults, overridden by active
    mf_lead_field_mappings rows, overridden by explicit ``options``.
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
    mappings = lead_field_mappings(store)
    acct_map = mappings.get("Account", {})
    account_name = (options.get("account_name")
                    or lead.get(acct_map.get("Name", "Company"))
                    or f"{lead.get('FirstName', '')} {lead.get('LastName', '')}".strip()
                    or "Converted Account")
    # Transactional conversion: track every created record so a later failure
    # compensates (deletes) the earlier ones instead of orphaning them.
    created: list[tuple[str, str]] = []
    account = contact = opportunity = None
    try:
        account_fields = _apply_lead_mappings(
            lead, {"Name": account_name}, acct_map, skip=("Name",))
        account = _convert_insert(store, registry, security, "Account",
                                  account_fields, lead.get("owner_id")
                                  or user["id"], user)
        created.append(("Account", account["id"]))
        contact_fields = {"FirstName": lead.get("FirstName"),
                          "LastName": lead.get("LastName"),
                          "Email": lead.get("Email"), "Phone": lead.get("Phone"),
                          "AccountId": account["id"]}
        contact_fields = _apply_lead_mappings(
            lead, contact_fields, mappings.get("Contact", {}),
            skip=("AccountId",))
        if not contact_fields.get("LastName"):
            contact_fields["LastName"] = "Unknown"
        contact_fields.update(options.get("contact") or {})
        contact = _convert_insert(store, registry, security, "Contact",
                                  {k: v for k, v in contact_fields.items()
                                   if v not in (None, "")},
                                  account["owner_id"], user)
        created.append(("Contact", contact["id"]))
        opportunity = None
        if options.get("create_opportunity", True):
            opp_obj = registry.get_object("Opportunity")
            stage_field = next((f for f in opp_obj["fields"] if f["name"] == "Stage"), {})
            opp_fields = {"Name": options.get("opportunity_name")
                          or f"{account_name} Opportunity",
                          "AccountId": account["id"],
                          "Stage": (stage_field.get("picklist_values") or ["Prospecting"])[0],
                          "CloseDate": str(_date.today() + _timedelta(days=30))}
            opp_fields = _apply_lead_mappings(
                lead, opp_fields, mappings.get("Opportunity", {}),
                skip=("AccountId", "Name"))
            opp_fields.update(options.get("opportunity") or {})
            opportunity = _convert_insert(store, registry, security, "Opportunity",
                                          opp_fields, account["owner_id"], user)
            created.append(("Opportunity", opportunity["id"]))
    except ValueError as e:
        # Compensate: delete everything already created, in reverse order,
        # so a failed conversion leaves no orphaned Account/Contact behind.
        for obj_name, rid in reversed(created):
            try:
                store.delete(obj_name, rid)
            except Exception:
                pass
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
    """Mark open milestones complete when a record hits its completion rule.

    The completion rule is ``{field: value}`` on the matching SLA policy
    (``completion`` key); it fires only when every field transitioned INTO
    its target value on this update. Policies created before the
    ``completion`` field existed complete on ``{"Status": "Closed"}``, which
    also preserves the entitlement-process behavior.
    """
    policy = sla_policy_for(store, obj_name, rec)
    completion = (policy or {}).get("completion") or {"Status": "Closed"}
    for field, want in completion.items():
        if rec.get(field) != want or (old or {}).get(field) == want:
            return 0
    n = 0
    for m in store.config_all("mf_case_milestones"):
        if m.get("record_id") == rec["id"] and not m.get("completed_at"):
            m["completed_at"] = utcnow()
            store.config_put("mf_case_milestones", m)
            n += 1
    return n


def case_milestones(store, obj_name: str, record_id: str):
    return sorted(
        (m for m in store.config_all("mf_case_milestones")
         if m.get("record_id") == record_id
         and m.get("object", "Case") == obj_name),
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
#: Built-in forecast type: Opportunity revenue (the historical default).
BUILTIN_FORECAST_TYPE = {
    "id": "builtin", "name": "Opportunity Revenue", "object": "Opportunity",
    "amount_field": "Amount", "date_field": "CloseDate",
    "category_field": "Stage", "won_values": ["Closed Won"],
    "lost_values": ["Closed Lost"], "probability_field": "Probability",
}


def get_forecast_type(store, type_id):
    """Stored forecast type by id, or the built-in Opportunity type."""
    if not type_id or type_id == "builtin":
        return dict(BUILTIN_FORECAST_TYPE)
    return store.config_get("mf_forecast_types", type_id)


def list_forecast_types(store):
    """Stored forecast types, with the built-in Opportunity type first."""
    rows = sorted(store.config_all("mf_forecast_types"),
                  key=lambda r: r.get("name") or "")
    return [dict(BUILTIN_FORECAST_TYPE)] + rows


def validate_forecast_type(store, registry, body, existing_id=None):
    """Validate a forecast-type definition. Returns an error string or None."""
    name = (body.get("name") or "").strip()
    if not name:
        return "name is required"
    dup = next((r for r in store.config_all("mf_forecast_types")
                if r.get("name") == name and r.get("id") != existing_id), None)
    if dup or name == BUILTIN_FORECAST_TYPE["name"]:
        return f"A forecast type named '{name}' already exists"
    obj_name = body.get("object")
    obj = registry.get_object(obj_name) if obj_name else None
    if not obj:
        return f"Unknown object '{obj_name}'"
    fmap = {f["name"]: f for f in obj.get("fields", [])}
    for key, kinds in (("amount_field", ("Currency", "Number")),
                       ("date_field", ("Date", "DateTime"))):
        fname = body.get(key)
        f = fmap.get(fname or "")
        if not f or f.get("type") not in kinds:
            return f"{key} must be a { '/'.join(kinds)} field on {obj_name}"
        if f.get("formula") or f.get("rollup") or f.get("type") == "Formula":
            return f"{key} cannot be a computed field"
    cf = fmap.get(body.get("category_field") or "")
    if not cf or cf.get("type") not in ("Picklist", "Text"):
        return "category_field must be a Picklist or Text field on " + obj_name
    for key in ("won_values", "lost_values"):
        vals = body.get(key)
        if not isinstance(vals, list) or not vals:
            return f"{key} must be a non-empty list of category values"
    pf = body.get("probability_field")
    if pf:
        f = fmap.get(pf)
        if not f or f.get("type") not in ("Currency", "Number", "Percent"):
            return "probability_field must be a numeric field on " + obj_name
    return None


def forecast_for_period(store, security, period: str, viewer: dict,
                        forecast_type: dict | None = None):
    """Forecast for one YYYY-MM period, over any configured object.

    Rows cover the viewer, their role subtree, or everyone for admins.
    Records whose category is in won_values count at full amount; lost_values
    are excluded; open records count at amount x probability (or full amount
    when the type has no probability field).
    """
    import re
    if not re.fullmatch(r"\d{4}-\d{2}", period or ""):
        raise ValueError("period must be YYYY-MM")
    ft = forecast_type or BUILTIN_FORECAST_TYPE
    type_id = ft.get("id")
    obj_name = ft["object"]
    amt_f, date_f = ft["amount_field"], ft["date_field"]
    cat_f, won, lost = ft["category_field"], set(ft["won_values"] or []), \
        set(ft["lost_values"] or [])
    prob_f = ft.get("probability_field")
    if security.is_admin(viewer):
        user_ids = [u["id"] for u in security.list_users()]
    else:
        user_ids = security.visible_owner_ids(viewer)
    quotas = {q["user_id"]: q["quota"] for q in store.config_all("mf_forecast_quotas")
              if q.get("period") == period
              and (q.get("forecast_type_id") or "builtin") == (type_id or "builtin")}
    names = {u["id"]: u.get("name") or u.get("username") for u in security.list_users()}
    rows = []
    for uid in user_ids:
        closed = 0.0
        weighted = 0.0
        pipeline = []
        for rec in store.query(obj_name, owner_ids=[uid], limit=10000):
            cd = (rec.get(date_f) or "")[:7]
            if cd != period:
                continue
            amt = float(rec.get(amt_f) or 0)
            cat = rec.get(cat_f) or ""
            if cat in won:
                closed += amt
            elif cat in lost:
                continue
            else:
                w = amt * float(rec.get(prob_f) or 0) / 100.0 if prob_f else amt
                weighted += w
                pipeline.append({"stage": cat, "amount": amt,
                                 "probability": rec.get(prob_f) if prob_f else None})
        total = round(closed + weighted, 2)
        quota = quotas.get(uid)
        rows.append({"user_id": uid, "user_name": names.get(uid, uid),
                     "quota": quota, "closed_amount": round(closed, 2),
                     "weighted_pipeline": round(weighted, 2),
                     "forecast": total,
                     "attainment": round(total / quota, 4) if quota else None,
                     "pipeline": pipeline})
    rows.sort(key=lambda r: r["user_name"] or "")
    return {"period": period, "forecast_type": {"id": type_id, "name": ft.get("name"),
                                               "object": obj_name},
            "rows": rows}


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


def _registry_shim(store):
    """Minimal registry for scheduler context (no Flask app available)."""
    class _Shim:
        def get_object(self, name):
            return store.meta_get("mf_objects", name)
    return _Shim()


def _resolve_sub_recipients(security, recipients):
    """Recipient entries -> deduped email list.

    Entries: {"type": "email"|"user"|"role", "value": ...}; legacy plain
    strings are treated as raw email addresses.
    """
    emails = []
    try:
        users = {u.get("id"): u for u in security.list_users()}
    except Exception:
        users = {}
    for r in recipients or []:
        if isinstance(r, str):
            if r.strip():
                emails.append(r.strip())
            continue
        if not isinstance(r, dict):
            continue
        t, v = r.get("type") or "email", (r.get("value") or "").strip()
        if not v:
            continue
        if t == "email":
            emails.append(v)
        elif t == "user":
            u = users.get(v)
            if u and u.get("email"):
                emails.append(u["email"])
        elif t == "role":
            for u in users.values():
                if u.get("role") == v and u.get("email") \
                        and u.get("is_active") is not False:
                    emails.append(u["email"])
    seen, out = set(), []
    for e in emails:
        if e.lower() not in seen:
            seen.add(e.lower())
            out.append(e)
    return out


def _report_run_for_digest(store, security, user, rep):
    """Run a report outside a request; returns the run_report_data dict."""
    from .api.reports import run_report_data
    return run_report_data(store, _registry_shim(store), security, user, rep,
                           page=1, page_size=10000)


def _dashboard_snapshot_html(store, security, user, dash):
    """D9: server-rendered HTML snapshot of a dashboard for email."""
    from .api.reports import run_report_data
    registry = _registry_shim(store)
    parts = [f"<h2>{(dash.get('name') or 'Dashboard')}</h2>"]
    for w in (dash.get("widgets") or [])[:20]:
        rep = store.config_get("mf_reports", w.get("report_id") or "")
        if not rep:
            continue
        data = run_report_data(store, registry, security, user, rep,
                               extra_filters=dash.get("filters") or [],
                               page=1, page_size=50)
        if data.get("error"):
            continue
        parts.append(f"<h3>{rep.get('name')} "
                     f"({data.get('row_count', 0)} rows)</h3>")
        cols = data.get("columns") or []
        if cols and data.get("rows"):
            cells = "".join(f"<th>{c}</th>" for c in cols)
            body = "".join(
                "<tr>" + "".join(f"<td>{(r.get(c) if r.get(c) is not None else '')}</td>"
                                 for c in cols) + "</tr>"
                for r in data["rows"][:25])
            parts.append(f"<table border='1' cellpadding='4'><tr>{cells}</tr>"
                         f"{body}</table>")
        for g in (data.get("groups") or [])[:12]:
            parts.append(f"<p>{g.get('key')}: {g.get('count')}</p>")
    return ("<html><body style='font-family:sans-serif'>"
            + "".join(parts) + "</body></html>")


def send_report_digest(store, security, sub_id: str) -> dict:
    """Scheduled-job entry point: email a report/dashboard digest.

    Called from generated job code as
    ``automation.send_report_digest(store, security, '<sub_id>')``.
    Delivery follows the platform demo convention: the email is logged
    (see email_log) and actually sent only when FORCELET_SMTP is set.

    R13 upgrades: CSV/Excel/HTML attachments, monthly cadence (see
    _sync_sub_job), "only when conditions met" (condition.min_rows), and
    user/role recipient resolution. D5: dashboards honor their run_as
    setting instead of always running as admin.
    """
    import base64
    sub = store.config_get("mf_report_subs", sub_id)
    if not sub or not sub.get("active", True):
        return {"ok": False, "detail": "subscription missing or inactive"}
    admin = security.get_user_by_username("admin")
    creator = None
    try:
        creator = security.get_user(sub.get("created_by") or "")
    except Exception:
        creator = None
    # D5: dashboards run as their configured run_as user (viewer default).
    dash = store.config_get("mf_dashboards", sub.get("dashboard_id") or "")
    run_user = admin or {}
    if dash:
        run_as = dash.get("run_as") or "viewer"
        if run_as == "viewer":
            run_user = creator or admin or {}
        else:
            try:
                run_user = security.get_user(run_as) or (creator or admin or {})
            except Exception:
                run_user = creator or admin or {}
    rep = store.config_get("mf_reports", sub.get("report_id") or "")
    if rep and (rep.get("run_as_user")):
        try:
            run_user = security.get_user(rep["run_as_user"]) or run_user
        except Exception:
            pass
    recipients = _resolve_sub_recipients(security, sub.get("recipients"))
    if not recipients:
        store.log_scheduled_run(sub.get("job_id") or sub_id, "ok",
                                "digest skipped: no resolvable recipients")
        return {"ok": False, "detail": "no resolvable recipients"}
    lines = [f"Report digest: {sub.get('name')}", ""]
    attachments = []
    attach_kind = sub.get("attachment") or "none"
    row_count = 0
    if rep:
        data = _report_run_for_digest(store, security, run_user, rep)
        if data.get("error"):
            return {"ok": False, "detail": data["error"]}
        row_count = data.get("row_count", 0)
        lines += [f"Report: {rep.get('name')} — {row_count} record(s)"]
        for g in (data.get("groups") or [])[:15]:
            agg = g.get("aggregate")
            lines.append(f"  {g.get('key')}: {g.get('count')}"
                         + (f" (Σ {agg})" if agg is not None else ""))
        if attach_kind in ("csv", "xlsx"):
            from .api.reports import _export_columns, _csv_bytes, _xlsx_bytes
            cols = _export_columns(rep, data.get("rows") or [])
            payload = _xlsx_bytes(cols, data.get("rows") or []) \
                if attach_kind == "xlsx" else _csv_bytes(cols, data.get("rows") or [])
            attachments.append({
                "filename": f"{(rep.get('name') or 'report')}.{attach_kind}",
                "content_type": "application/vnd.openxmlformats-officedocument."
                                "spreadsheetml.sheet" if attach_kind == "xlsx"
                                else "text/csv",
                "data": base64.b64encode(payload).decode("ascii")})
        elif attach_kind == "html":
            html = _dashboard_snapshot_html(
                store, security, run_user,
                {"name": rep.get("name"), "widgets": [
                    {"report_id": rep.get("id"), "type": "table"}]})
            attachments.append({"filename": f"{rep.get('name') or 'report'}.html",
                                "content_type": "text/html",
                                "data": base64.b64encode(
                                    html.encode("utf-8")).decode("ascii")})
    if dash:
        for w in dash.get("widgets") or []:
            wrep = store.config_get("mf_reports", w.get("report_id") or "")
            if not wrep:
                continue
            wdata = _report_run_for_digest(store, security, run_user, wrep)
            n = wdata.get("row_count", 0)
            row_count = max(row_count, n)
            lines += [f"--- {wrep.get('name')} ({w.get('type', 'bar')}) ---",
                      f"{n} record(s)"]
        if attach_kind == "html":
            html = _dashboard_snapshot_html(store, security, run_user, dash)
            attachments.append({"filename": f"{dash.get('name') or 'dashboard'}.html",
                                "content_type": "text/html",
                                "data": base64.b64encode(
                                    html.encode("utf-8")).decode("ascii")})
    # R13: only-when-conditions-met.
    min_rows = (sub.get("condition") or {}).get("min_rows")
    if isinstance(min_rows, int) and min_rows > 0 and row_count < min_rows:
        store.log_scheduled_run(sub.get("job_id") or sub_id, "ok",
                                f"digest skipped: {row_count} rows < min_rows {min_rows}")
        return {"ok": True, "detail": "skipped: condition not met"}
    body = "\n".join(lines)[:8000]
    subject = f"[Forcelet digest] {sub.get('name')}"
    for rcpt in recipients:
        store.log_email("Report", sub.get("report_id") or sub.get("dashboard_id") or "",
                        rcpt, subject, body, "report-digest", run_user or {},
                        attachments=attachments)
    store.log_scheduled_run(sub.get("job_id") or sub_id, "ok",
                            f"digest sent to {len(recipients)}")
    return {"ok": True, "detail": f"sent to {len(recipients)}"}
