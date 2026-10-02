"""Shared API helpers: auth decorators, serialization, record DML.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import current_app, jsonify, request

from .. import automation
from .. import crypto as _crypto
from ..store import utcnow
from .. import datamodel as _datamodel
from .. import dynamic_forms as _dynforms
from .. import duplicate_rules as _duprules
from .. import email_alerts as _emailalerts
from ..expressions import eval_expr, record_context, resolve_dotted_field
from ..field_types import FIELD_TYPES, mask_secret, validate_value
from ..security import (hash_token, session_timeouts)


def ctx():
    """Return (store, registry, security) for the current app."""
    return current_app.mf_store, current_app.mf_registry, current_app.mf_security


# ---------------------------------------------------------------------------
# Declarative stored roll-up rules (A6)
# ---------------------------------------------------------------------------
#: Aggregation functions a roll-up rule may use.
ROLLUP_FUNCS = ("sum", "count", "min", "max", "avg")

_rollup_local = threading.local()


def _rollup_rules_for(child_obj_name):
    store = ctx()[0]
    try:
        rows = store.config_all("mf_rollup_rules")
    except Exception:
        return []
    return [r for r in rows
            if r.get("active", True)
            and r.get("child_object") == child_obj_name]


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _apply_rollup_rule(user, rule, child_rec, seen):
    """Recompute one rule's parent field. Never raises (caller guards)."""
    store, registry, security = ctx()
    parent_obj = rule.get("parent_object")
    link_field = rule.get("link_field")
    parent_field = rule.get("parent_field")
    child_field = rule.get("child_field")
    func = rule.get("func") or "sum"
    parent_id = (child_rec or {}).get(link_field) if link_field else None
    if not parent_obj or not parent_id or (parent_obj, parent_id) in seen:
        return
    pdef = registry.get_object(parent_obj)
    cdef = registry.get_object(rule.get("child_object"))
    if not pdef or not cdef:
        return
    pfields = {f["name"]: f for f in pdef.get("fields", [])}
    cfields = {f["name"]: f for f in cdef.get("fields", [])}
    pf = pfields.get(parent_field)
    if not pf or pf.get("formula") or pf.get("rollup") or pf.get("type") in ("Formula", "AutoNumber"):
        return  # never write into computed fields
    if func != "count" and not cfields.get(child_field):
        return
    if not cfields.get(link_field):
        return
    seen.add((parent_obj, parent_id))
    recs, _ = _visible_records(user, rule["child_object"])
    kids = [r for r in recs if r.get(link_field) == parent_id]
    if func == "count":
        total = len(kids)
    else:
        vals = [_num(r.get(child_field)) for r in kids]
        if func == "min":
            total = min(vals) if vals else None
        elif func == "max":
            total = max(vals) if vals else None
        elif func == "avg":
            total = sum(vals) / len(vals) if vals else None
        else:  # sum over an empty set is 0, matching the legacy roll-ups
            total = sum(vals)
    if total is not None and pf.get("type") == "Currency":
        total = round(total, 2)
    # Write through the normal update pipeline so validation, triggers,
    # flows and webhooks fire exactly as they did for the hardcoded roll-up.
    status, _payload = _do_update(user, parent_obj, parent_id,
                                  {parent_field: total})
    if status >= 400:
        logging.getLogger("forcelet").warning(
            "Roll-up rule '%s' could not update %s %s (status %s)",
            rule.get("name"), parent_obj, parent_id, status)


def recompute_stored_rollups(user, child_obj_name, child_rec, old_rec=None):
    """Recompute parent fields for all active roll-up rules on a child write.

    Called after create/update/delete of a child record. When the link field
    changed (reparenting), ``old_rec`` refreshes the previous parent too.
    Never raises: a stale parent value is better than a 500 on a saved
    record. A per-thread ``seen`` set stops cyclic rule chains from looping.
    """
    rules = _rollup_rules_for(child_obj_name)
    if not rules:
        return
    seen = getattr(_rollup_local, "seen", None)
    top = seen is None
    if top:
        seen = set()
        _rollup_local.seen = seen
    try:
        for rule in rules:
            try:
                _apply_rollup_rule(user, rule, child_rec, seen)
            except Exception:
                logging.getLogger("forcelet").warning(
                    "Stored roll-up rule '%s' failed", rule.get("name"),
                    exc_info=True)
            link = rule.get("link_field")
            old_pid = (old_rec or {}).get(link) if link else None
            new_pid = (child_rec or {}).get(link) if link else None
            if old_pid and old_pid != new_pid:
                try:
                    _apply_rollup_rule(
                        user, rule, {**(child_rec or {}), link: old_pid},
                        seen)
                except Exception:
                    logging.getLogger("forcelet").warning(
                        "Stored roll-up rule '%s' failed (old parent)",
                        rule.get("name"), exc_info=True)
    finally:
        if top:
            _rollup_local.seen = None


#: KnowledgeArticle fields whose change snapshots a new article version.
KB_CONTENT_FIELDS = ("Title", "Summary", "Body", "Category", "Status")


def snapshot_kb_version(store, old_rec, user):
    """Store the pre-update state of a KnowledgeArticle as a new version.

    Versions are immutable history; the live article always holds the latest
    content. Never raises.
    """
    try:
        article_id = old_rec.get("id")
        rows = [r for r in store.config_all("mf_kb_versions")
                if r.get("article_id") == article_id]
        version = max([r.get("version") or 0 for r in rows] + [0]) + 1
        store.config_put("mf_kb_versions", {
            "article_id": article_id, "version": version,
            "title": old_rec.get("Title"), "summary": old_rec.get("Summary"),
            "body": old_rec.get("Body"), "status": old_rec.get("Status"),
            "category": old_rec.get("Category"),
            "snapshot": {k: v for k, v in old_rec.items()},
            "created_by": (user or {}).get("id"),
            "created_date": utcnow(),
        })
    except Exception:
        logging.getLogger("forcelet").warning(
            "KB version snapshot failed for %s", old_rec.get("id"),
            exc_info=True)


# ---------------------------------------------------------------------------
# Rate limiting (in-process sliding window; bypassed when TESTING)
# ---------------------------------------------------------------------------
_RATE_BUCKETS: dict[str, deque] = {}
_RATE_LOCK = threading.Lock()


def rate_limit(max_requests: int, window_seconds: int, key_fn=None):
    """Decorator: at most max_requests per window_seconds per key."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if current_app.config.get("TESTING"):
                return fn(*a, **kw)
            key = (key_fn() if key_fn else "global") + ":" + fn.__name__
            now = time.time()
            with _RATE_LOCK:
                bucket = _RATE_BUCKETS.setdefault(key, deque())
                while bucket and bucket[0] <= now - window_seconds:
                    bucket.popleft()
                if len(bucket) >= max_requests:
                    return jsonify({"error": "Too many requests, slow down"}), 429
                bucket.append(now)
            return fn(*a, **kw)
        return wrapper
    return deco


def _client_ip() -> str:
    # When behind a trusted reverse proxy (FORCELET_BEHIND_PROXY=1), honor
    # X-Forwarded-For; otherwise use the direct peer address so a client
    # cannot spoof its IP.
    import os
    if os.environ.get("FORCELET_BEHIND_PROXY") == "1":
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
# Query-param tokens exist only for browser EventSource streams, which cannot
# set an Authorization header. They are accepted solely on those paths so
# tokens do not leak into logs via arbitrary URLs.
QUERY_TOKEN_PATHS = frozenset({"/api/streaming"})


def issue_session(store, user, limited=False):
    """Mint a session token for user; store only its hash. Returns the token."""
    from ..security import new_session_token, session_timeouts
    token = new_session_token()
    ttl, max_age = session_timeouts(store)
    now = datetime.now(timezone.utc)
    expires = min(now + timedelta(seconds=max_age),
                  now + timedelta(seconds=ttl))
    store.create_session(hash_token(token), user["id"],
                         expires.isoformat(timespec="seconds"),
                         ip=_client_ip(),
                         user_agent=request.headers.get("User-Agent", ""),
                         limited=limited)
    return token


def current_user():
    store, registry, security = ctx()
    auth = request.headers.get("Authorization", "")
    token = None
    if auth.startswith("Bearer "):
        token = auth[7:]
    elif request.path in QUERY_TOKEN_PATHS and request.args.get("access_token"):
        token = request.args["access_token"]
    if not token:
        return None
    if token.startswith("mf_live_"):
        rec = store.get_api_key(hashlib.sha256(token.encode()).hexdigest())
        if rec:
            store.touch_api_key(rec["key_hash"])
            return security.get_user(rec["user_id"])
        return None
    from ..security import SESSION_PREFIX
    if token.startswith(SESSION_PREFIX):
        sess = store.get_session(hash_token(token))
        if not sess:
            return None
        now = datetime.now(timezone.utc)
        try:
            expires = datetime.fromisoformat(sess["expires_at"])
        except Exception:
            return None
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            store.delete_session(hash_token(token))
            return None
        # Sliding expiry: idle sessions live session_timeout_minutes, capped at
        # session_max_hours from creation (both admin-configurable).
        try:
            created = datetime.fromisoformat(sess["created_at"])
        except Exception:
            created = now
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        ttl, max_age = session_timeouts(store)
        new_exp = min(created + timedelta(seconds=max_age),
                      now + timedelta(seconds=ttl))
        store.touch_session(hash_token(token), new_exp.isoformat(timespec="seconds"))
        user = security.get_user(sess["user_id"])
        if user and not user.get("is_active", True):
            # Deactivated mid-session: kill the session, deny the request.
            store.delete_session(hash_token(token))
            return None
        if user:
            request.mf_session = sess
        return user
    return None


# Paths a limited session may call: forced password change, plus the
# self-service TOTP enrollment flow (needed when 2FA is required but the
# user has not enrolled yet).
_LIMITED_SESSION_PATHS = frozenset({
    "/api/change-password",
    "/api/me/totp/status",
    "/api/me/totp/setup",
    "/api/me/totp/enable",
})


def require_auth(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        user = current_user()
        if not user:
            return jsonify({"error": "Authentication required"}), 401
        sess = getattr(request, "mf_session", None)
        if sess and sess.get("limited") and request.path not in _LIMITED_SESSION_PATHS:
            # Seeded/default credentials or pending 2FA enrollment: the user
            # must finish onboarding before doing anything else.
            return jsonify({"error": "Account setup incomplete — finish the "
                                     "required password change / 2FA enrollment",
                            "must_change_password": bool(user.get("must_change_password"))}), 403
        request.mf_user = user
        return fn(*a, **kw)
    return wrapper


def require_admin(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        store, registry, security = ctx()
        if not security.is_admin(request.mf_user):
            return jsonify({"error": "System Administrator profile required"}), 403
        return fn(*a, **kw)
    return wrapper


def require_admin_scope(*scopes):
    """Allow full admins, or delegated admins holding any of the scopes.

    Scopes come from delegated administration groups
    (see forcelet/delegated.py): "users", "passwords", "profiles", "roles".
    Endpoints that target a specific user must additionally call
    ``delegated.target_in_scope`` when the grant is role-restricted.
    """
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            store, registry, security = ctx()
            user = request.mf_user
            if security.is_admin(user):
                return fn(*a, **kw)
            from .. import delegated as _delegated
            if any(_delegated.has_scope(store, user, s) for s in scopes):
                return fn(*a, **kw)
            return jsonify({"error": "System Administrator profile or a "
                                     "delegated administration grant required"}), 403
        return wrapper
    return deco


def _audit(action, entity_type, name, details=""):
    store, registry, security = ctx()
    try:
        store.audit(request.mf_user, action, entity_type, name, details)
    except Exception:
        pass


def serialize(user, obj_def, record):
    store, registry, security = ctx()
    readable = set(security.readable_fields(user, obj_def))
    data = {"Id": record["id"], "OwnerId": record.get("owner_id"),
            "RecordType": record.get("record_type") or "Default",
            "CreatedDate": record.get("created_date"),
            "LastModifiedDate": record.get("last_modified_date")}
    for f in obj_def.get("fields", []):
        if f["name"] not in readable:
            continue
        if f.get("formula") or f.get("type") == "Formula":
            try:
                data[f["name"]] = eval_expr(
                    f["formula"], record_context(record),
                    rel_resolver=lambda path: resolve_dotted_field(
                        store, registry, obj_def["name"], record, path))
            except Exception:
                data[f["name"]] = None
        elif f.get("rollup"):
            try:
                data[f["name"]] = automation.compute_rollup(
                    store, security, user, f["rollup"], record["id"])
            except Exception:
                data[f["name"]] = None
        elif f.get("type") == "EncryptedText":
            # Decrypted server-side, then masked for every consumer of the API
            # (detail view, list views, CSV export): "••••••1234".
            v = record.get(f["name"])
            v = _crypto.decrypt(v) if _crypto.is_encrypted(v) else v
            data[f["name"]] = mask_secret(v, f.get("mask_chars", 4))
        else:
            v = record.get(f["name"])
            data[f["name"]] = _crypto.decrypt(v) if f.get("encrypted") else v
    if obj_def["name"] == "Account":
        person_name = _datamodel.person_display_name(record)
        if person_name:
            data["Name"] = person_name
    try:
        from .. import divisions as _divisions
        data["Division"] = _divisions.record_division_name(
            store, obj_def["name"], record.get("id") or "")
    except Exception:
        data["Division"] = ""
    return data


def _visible_records(user, obj_name):
    store, registry, security = ctx()
    obj = registry.get_object(obj_name)
    return [r for r in store.query(obj_name, owner_ids=None, limit=10000)
            if security.can_see_record(user, r, obj_name)], obj


#: Snapshot keys that are record-system metadata, not field data; they are
#: always safe to include in a scrubbed change event (mirrors serialize()).
SNAPSHOT_SYSTEM_KEYS = frozenset(
    {"id", "owner_id", "created_date", "last_modified_date", "record_type"})


def filter_change_event(user, obj_name, record_id, snapshot, changed_fields):
    """Apply record sharing + field-level security to a change event.

    Used by the SSE streaming broker consumer and the CDC REST endpoint so a
    subscriber only receives events for records they may see, with snapshots
    masked to fields they may read.

    Returns ``(scrubbed_snapshot, scrubbed_changed_fields)`` or ``None`` when
    the user must not see the event at all. Never mutates its inputs.
    """
    store, registry, security = ctx()
    if not security.can(user, "read", obj_name):
        return None
    obj_def = store.meta_get("mf_objects", obj_name)
    if not obj_def:
        return None
    record = store.get(obj_name, record_id) if record_id else None
    # For deletes the record is gone; evaluate sharing against the snapshot.
    seen = record if record is not None else dict(snapshot or {})
    if not security.can_see_record(user, seen, obj_name):
        return None
    readable = set(security.readable_fields(user, obj_def))
    scrubbed = {k: v for k, v in (snapshot or {}).items()
                if k in readable or k in SNAPSHOT_SYSTEM_KEYS}
    fields = [f for f in (changed_fields or []) if f in readable]
    return scrubbed, fields


#: Task fields copied onto the next occurrence of a recurring task.
TASK_RECURRENCE_COPY = ("Subject", "Priority", "AccountId", "Description",
                        "IsRecurring", "RecurrenceType",
                        "RecurrenceInterval", "RecurrenceEndDate",
                        "RecurrenceCount")


def task_recurrence_error(rec, clean):
    """Validate recurrence settings on a Task. Returns an error string or None."""
    is_rec = clean.get("IsRecurring", (rec or {}).get("IsRecurring"))
    if not is_rec:
        return None
    rtype = clean.get("RecurrenceType", (rec or {}).get("RecurrenceType"))
    if not rtype:
        return "RecurrenceType is required for a recurring Task"
    interval = clean.get("RecurrenceInterval",
                         (rec or {}).get("RecurrenceInterval") or 1)
    if (interval or 0) < 1:
        return "RecurrenceInterval must be at least 1"
    count = clean.get("RecurrenceCount", (rec or {}).get("RecurrenceCount"))
    if count is not None and count < 1:
        return "RecurrenceCount must be at least 1"
    return None


def advance_recurrence(date_str, rtype, interval):
    """Advance an ISO date by a recurrence step. Never raises."""
    from calendar import monthrange
    try:
        d = datetime.fromisoformat(str(date_str)[:10]).date()
    except (ValueError, TypeError):
        d = datetime.now(timezone.utc).date()
    interval = max(int(interval or 1), 1)
    if rtype == "Weekly":
        d += timedelta(weeks=interval)
    elif rtype == "Monthly":
        month = d.month - 1 + interval
        year = d.year + month // 12
        month = month % 12 + 1
        d = d.replace(year=year, month=month,
                      day=min(d.day, monthrange(year, month)[1]))
    else:  # Daily (and unknown types fall back to daily)
        d += timedelta(days=interval)
    return d.isoformat()


def maybe_create_next_task_occurrence(user, rec):
    """Create the next occurrence after a recurring Task is completed.

    Stops when RecurrenceCount occurrences exist or the next due date passes
    RecurrenceEndDate. Failures are logged, never raised: completing the task
    is the primary action.
    """
    log = logging.getLogger("forcelet")
    try:
        if not rec.get("IsRecurring"):
            return
        occ = rec.get("OccurrenceNumber") or 1
        total = rec.get("RecurrenceCount")
        if total and occ >= total:
            return
        rtype = rec.get("RecurrenceType") or "Daily"
        interval = rec.get("RecurrenceInterval") or 1
        base = rec.get("DueDate") or utcnow()[:10]
        next_due = advance_recurrence(base, rtype, interval)
        end = rec.get("RecurrenceEndDate")
        if end and next_due > str(end)[:10]:
            return
        new = {k: rec.get(k) for k in TASK_RECURRENCE_COPY
               if rec.get(k) is not None}
        new.update({"Status": "Not Started", "DueDate": next_due,
                    "OccurrenceNumber": occ + 1})
        status, payload = _do_create(user, "Task", new)
        if status >= 400:
            log.warning("recurring task: next occurrence not created: %s",
                        payload)
    except Exception:
        log.warning("recurring task: next occurrence failed", exc_info=True)


def _resolve_relationship_values(obj, obj_name, values):
    """Resolve Lookup/MasterDetail/PolymorphicLookup values to record Ids.

    Accepts plain Id strings (existence-checked against the target objects)
    and ``{"ExternalIdField": value}`` indirect references. Mutates ``values``
    in place. Returns an error string on the first failure, else None.
    """
    store, registry, _sec = ctx()
    for f in _datamodel.relationship_fields(obj):
        fname = f["name"]
        if fname not in values:
            continue
        try:
            values[fname] = _datamodel.resolve_lookup_value(
                store, registry, obj_name, f, values[fname])
        except ValueError as e:
            return str(e)
    return None


def _check_hierarchy_cycles(obj_name, rid_or_none, values):
    """Reject self-referencing relationship values that would create a cycle.

    Applies to Lookup/MasterDetail/PolymorphicLookup fields whose target
    includes the object being written. Returns an error string on the first
    failure, else None.
    """
    store, registry, _sec = ctx()
    obj = registry.get_object(obj_name)
    if not obj:
        return None
    for f in _datamodel.relationship_fields(obj):
        ref = f.get("reference_to")
        refs = ref if isinstance(ref, list) else [ref]
        if obj_name not in refs or f["name"] not in values:
            continue
        try:
            _datamodel.validate_hierarchy_no_cycle(
                store, registry, obj_name, rid_or_none, f, values[f["name"]])
        except ValueError as e:
            return str(e)
    return None


def _do_create(user, obj_name, body, allow_duplicates=False):
    store, registry, security = ctx()
    obj = registry.get_object(obj_name)
    if not obj or not security.can(user, "create", obj_name):
        return 404, {"error": "Unknown object or no access"}
    rt = body.get("RecordType") or automation.default_record_type(store, obj_name)
    if rt != "Default" and rt not in [r["name"] for r in automation.get_record_types(store, obj_name)]:
        return 422, {"error": f"Unknown record type '{rt}'"}
    editable = set(security.editable_fields(user, obj))
    values = {k: v for k, v in body.items()
              if k in editable and k not in ("RecordType",)}
    is_person = values.get("IsPersonAccount") or rt == "PersonAccount"
    if obj_name == "Account" and is_person and "Name" in editable:
        values["IsPersonAccount"] = True
        if not values.get("Name"):
            # person accounts derive their display name from the person fields
            values["Name"] = _datamodel.person_display_name(values) or "Person Account"
    # Relationship values: resolve indirect {"ExternalId": value} references
    # to record Ids and existence-check plain Ids (incl. polymorphic targets).
    # Must run before validate_record, which would mangle dict values.
    rel_err = _resolve_relationship_values(obj, obj_name, values)
    if rel_err:
        return 422, {"error": "Validation failed", "details": [rel_err]}
    # Dynamic Forms: a required field hidden by a visibility rule must not
    # block the save. Rules are evaluated against the submitted values.
    df_hidden = _dynforms.hidden_fields(store, obj_name, values)
    clean, errors = registry.validate_record(obj, values, skip_required=df_hidden)
    if errors:
        return 422, {"error": "Validation failed", "details": errors}
    md_err = _datamodel.validate_md_parents_exist(store, obj, clean)
    if md_err:
        return 422, {"error": "Validation failed", "details": [md_err]}
    hier_err = _check_hierarchy_cycles(obj_name, None, clean)
    if hier_err:
        return 422, {"error": "Validation failed", "details": [hier_err]}
    if obj_name == "CampaignMember":
        serr = campaign_member_status_error(
            store, clean.get("CampaignId"), clean.get("Status"))
        if serr:
            return 422, {"error": serr}
    if obj_name == "Task":
        terr = task_recurrence_error(None, clean)
        if terr:
            return 422, {"error": terr}
    # Declarative duplicate rules (MatchingRule/DuplicateRule) take precedence:
    # an explicit Block/Warn decides the outcome; otherwise the legacy
    # mf_matching_rules check applies.
    dup_action, dup_message = _duprules.evaluate_duplicate_rules(
        store, obj_name, "create", clean)
    if dup_action == "block" and not allow_duplicates:
        return 409, {"error": dup_message}
    if dup_action == "warn" and not allow_duplicates:
        dup_warning = dup_message
    else:
        dup_warning = None
        dups = automation.check_duplicates(store, obj_name, clean)
        if dups and not allow_duplicates:
            return 409, {"error": "Possible duplicates found", "duplicates": dups}
    clean["owner_id"] = (automation.apply_assignment_rules(
        store, registry, security, obj_name, clean, user) or user["id"])
    clean["created_by"] = user["id"]
    clean["record_type"] = rt
    # AutoNumber fields are system-assigned here (user input was already
    # rejected by validate_record), before before_insert triggers run so
    # triggers/flows see the assigned value. next_sequence is atomic under
    # the store lock, so concurrent creates never hand out the same number
    # (a later failure can leave a gap in the sequence — same as Salesforce).
    _auto_fields = {f["name"] for f in obj.get("fields", [])
                    if f.get("type") == "AutoNumber" and f.get("active") is not False}
    for f in obj.get("fields", []):
        if f["name"] in _auto_fields:
            clean[f["name"]] = _datamodel.next_auto_number(store, obj_name, f)
    terr = automation.run_triggers(store, registry, security, obj_name,
                                  "before_insert", clean, None, user)
    if terr:
        return 422, {"error": "Trigger failed", "details": terr}
    clean2, errors = registry.validate_record(obj, {k: v for k, v in clean.items()
                                                    if k in editable and k not in _auto_fields},
                                              partial=True)
    if errors:
        return 422, {"error": "Validation failed", "details": errors}
    clean.update(clean2)
    vr_errors = automation.check_validation_rules(store, obj_name, {**clean, "record_type": rt})
    if vr_errors:
        return 422, {"error": "Validation rule failed", "details": vr_errors}
    rid = store.insert(obj_name, clean)
    rec = store.get(obj_name, rid)
    # Divisions: new records inherit the creator's division (from their
    # profile's default_division). Admins creating records stay global
    # unless they move the record explicitly.
    try:
        from .. import divisions as _divisions
        user_div = _divisions.user_division_id(store, security, user)
        if user_div:
            _divisions.set_record_division(store, obj_name, rid, user_div)
    except Exception:
        pass  # division stamping must never break the save pipeline
    automation.start_case_milestones(store, obj_name, rec)
    if clean.get("owner_id") and clean["owner_id"] != user["id"]:
        owner = security.get_user(clean["owner_id"])
        if owner:
            label = clean.get("Name") or clean.get("Subject") or rid
            store.notify(owner["id"], "assignment",
                         f"{obj_name} assigned to you",
                         f"{label} was assigned to you by "
                         f"{user.get('name')}.",
                         obj_name, rid)
    terr = automation.run_triggers(store, registry, security, obj_name,
                                  "after_insert", rec, None, user)
    if terr:
        store.delete(obj_name, rid)  # compensate: roll back the insert
        return 422, {"error": "Trigger failed", "details": terr}
    automation.run_flows(store, registry, security, obj_name, "create", rec, None, user)
    automation.apply_escalation_rules(store, registry, security, obj_name,
                                      rec, None, user)
    automation.run_auto_responses(store, registry, security, obj_name, rec, user)
    automation.dispatch_webhooks(store, obj_name, "create", serialize(user, obj, rec), user)
    automation.submit_for_approval(store, security, obj_name, rec, user)
    store.emit_change(obj_name, rid, "create", user,
                      snapshot={k: v for k, v in store.get(obj_name, rid).items()
                                if not _crypto.is_encrypted(v)})
    final_rec = store.get(obj_name, rid)
    try:
        _emailalerts.fire_email_alerts(store, obj_name, "Create", final_rec,
                                       user=user, security=security)
    except Exception:
        pass  # email alerts are fire-and-forget; never break the save pipeline
    payload = serialize(user, obj, final_rec)
    if dup_warning:
        payload["warning"] = dup_warning
    recompute_stored_rollups(user, obj_name, final_rec)
    return 201, payload


#: Legal Contract Status transitions. Terminal states have no outgoing edges.
CONTRACT_STATUS_FLOW = {
    "Draft": {"Activated", "Cancelled"},
    "Activated": {"Expired", "Cancelled"},
    "Expired": set(),
    "Cancelled": set(),
}
#: Contract terms that freeze once the contract leaves Draft.
CONTRACT_LOCKED_FIELDS = ("AccountId", "StartDate", "EndDate", "ContractTerm")

#: Built-in CampaignMember statuses used when a campaign defines none.
DEFAULT_MEMBER_STATUSES = ["Sent", "Responded"]


def campaign_member_statuses(store, campaign_id):
    """Active member-status names for a campaign (defaults if none defined)."""
    rows = sorted(
        (r for r in store.config_all("mf_campaign_member_statuses")
         if r.get("campaign_id") == campaign_id and r.get("active", True)),
        key=lambda r: (r.get("sort_order") or 0, r.get("name") or ""))
    return [r["name"] for r in rows] if rows else list(DEFAULT_MEMBER_STATUSES)


def campaign_member_status_error(store, campaign_id, status):
    """Validate a CampaignMember Status against its campaign's statuses."""
    if not status:
        return None
    allowed = campaign_member_statuses(store, campaign_id)
    if status not in allowed:
        return (f"Status '{status}' is not a member status of this campaign "
                f"(allowed: {', '.join(allowed)})")
    return None


def contract_guard_error(rec, clean):
    """Enforce the Contract lifecycle. Returns an error string or None.

    - Status may only follow CONTRACT_STATUS_FLOW; terminal states are final.
    - Activating requires a StartDate.
    - Term fields (account, dates, term length) cannot change once the
      contract has left Draft.
    """
    old_status = rec.get("Status") or "Draft"
    new_status = clean.get("Status", old_status)
    if new_status != old_status:
        allowed = CONTRACT_STATUS_FLOW.get(old_status, set())
        if new_status not in allowed:
            return (f"Cannot move Contract from '{old_status}' to "
                    f"'{new_status}'")
        if new_status == "Activated" and not (
                clean.get("StartDate") or rec.get("StartDate")):
            return "StartDate is required to activate a Contract"
    if old_status != "Draft":
        locked = [f for f in CONTRACT_LOCKED_FIELDS
                  if f in clean and clean[f] != rec.get(f)]
        if locked:
            return (f"Cannot change {', '.join(locked)} on a "
                    f"{old_status} Contract")
    return None


def _do_update(user, obj_name, rid, body, allow_duplicates=False):
    store, registry, security = ctx()
    obj = registry.get_object(obj_name)
    rec = obj and store.get(obj_name, rid)
    if not obj or not rec or not security.can(user, "edit", obj_name) \
            or not security.can_see_record(user, rec, obj_name):
        return 404, {"error": "Not found"}
    if automation.pending_request_for(store, obj_name, rid) and not security.is_admin(user):
        return 423, {"error": "Record is locked: an approval request is pending"}
    try:
        _datamodel.assert_mutable(obj)
    except ValueError as e:
        return 422, {"error": str(e)}
    editable = set(security.editable_fields(user, obj))
    values = {k: v for k, v in body.items() if k in editable}
    # Resolve relationship values before validation (see _do_create).
    rel_err = _resolve_relationship_values(obj, obj_name, values)
    if rel_err:
        return 422, {"error": "Validation failed", "details": [rel_err]}
    clean, errors = registry.validate_record(obj, values, partial=True)
    if errors:
        return 422, {"error": "Validation failed", "details": errors}
    if obj_name == "Contract":
        gerr = contract_guard_error(rec, clean)
        if gerr:
            return 422, {"error": gerr}
    if obj_name == "CampaignMember" and "Status" in clean:
        serr = campaign_member_status_error(
            store, clean.get("CampaignId") or rec.get("CampaignId"),
            clean.get("Status"))
        if serr:
            return 422, {"error": serr}
    if obj_name == "Task":
        terr = task_recurrence_error(rec, clean)
        if terr:
            return 422, {"error": terr}
    for f in _datamodel.md_fields(obj):
        if f["name"] in clean and clean[f["name"]] != rec.get(f["name"]) \
                and not f.get("reparentable", True):
            return 422, {"error": f"{f.get('label', f['name'])} is not reparentable"}
    md_err = _datamodel.validate_md_parents_exist(store, obj, clean)
    if md_err:
        return 422, {"error": "Validation failed", "details": [md_err]}
    hier_err = _check_hierarchy_cycles(obj_name, rid, clean)
    if hier_err:
        return 422, {"error": "Validation failed", "details": [hier_err]}
    working = {**rec, **clean}
    terr = automation.run_triggers(store, registry, security, obj_name,
                                  "before_update", working, rec, user)
    if terr:
        return 422, {"error": "Trigger failed", "details": terr}
    allowed = {f["name"] for f in obj["fields"]}
    folded = {k: working[k] for k in allowed
              if working.get(k) != rec.get(k) and k not in clean}
    if folded:
        clean2, errors = registry.validate_record(obj, folded, partial=True)
        if errors:
            return 422, {"error": "Validation failed", "details": errors}
        clean.update(clean2)
    merged = {**rec, **clean}
    # Declarative duplicate rules (MatchingRule/DuplicateRule) take precedence:
    # an explicit Block/Warn decides the outcome; otherwise the legacy
    # mf_matching_rules check applies.
    dup_action, dup_message = _duprules.evaluate_duplicate_rules(
        store, obj_name, "update", merged, exclude_id=rid)
    if dup_action == "block" and not allow_duplicates:
        return 409, {"error": dup_message}
    if dup_action == "warn" and not allow_duplicates:
        dup_warning = dup_message
    else:
        dup_warning = None
        dups = automation.check_duplicates(store, obj_name, merged, exclude_id=rid)
        if dups and not allow_duplicates:
            return 409, {"error": "Possible duplicates found", "duplicates": dups}
    vr_errors = automation.check_validation_rules(store, obj_name, merged, rec)
    if vr_errors:
        return 422, {"error": "Validation rule failed", "details": vr_errors}
    automation.log_history(store, obj_name, rid, rec, merged, user)
    store.update(obj_name, rid, clean)
    new_rec = store.get(obj_name, rid)
    automation.complete_case_milestones(store, obj_name, new_rec, rec)
    automation.apply_escalation_rules(store, registry, security, obj_name,
                                      new_rec, rec, user)
    terr = automation.run_triggers(store, registry, security, obj_name,
                                  "after_update", new_rec, rec, user)
    if terr:
        store.update(obj_name, rid, {k: rec[k] for k in clean if k in rec})  # compensate
        return 422, {"error": "Trigger failed", "details": terr}
    automation.run_flows(store, registry, security, obj_name, "update", new_rec, rec, user)
    automation.dispatch_webhooks(store, obj_name, "update", serialize(user, obj, new_rec), user)
    store.emit_change(obj_name, rid, "update", user, changed_fields=list(clean.keys()),
                      snapshot={k: v for k, v in new_rec.items()
                                if not _crypto.is_encrypted(v)})
    try:
        _emailalerts.fire_email_alerts(store, obj_name, "Update", new_rec,
                                       user=user, security=security)
    except Exception:
        pass  # email alerts are fire-and-forget; never break the save pipeline
    payload = serialize(user, obj, new_rec)
    if dup_warning:
        payload["warning"] = dup_warning
    recompute_stored_rollups(user, obj_name, merged, old_rec=rec)
    if obj_name == "KnowledgeArticle" and any(
            k in KB_CONTENT_FIELDS for k in clean):
        snapshot_kb_version(store, rec, user)
    if obj_name == "Task" and clean.get("Status") == "Completed" \
            and rec.get("Status") != "Completed":
        maybe_create_next_task_occurrence(user, merged)
    return 200, payload
