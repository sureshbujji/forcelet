"""DevOps platform services for Forcelet.

Covers the platform/DevOps concepts Salesforce admins and release engineers
rely on:

* **Bulk API 2.0 ingest** — CSV jobs (insert / update / upsert / delete) with
  per-row success/error result files, mirroring the Bulk API v2 lifecycle
  (Open -> UploadComplete -> InProgress -> JobComplete).
* **Streaming events** — an in-process event broker fed by every change-data
  capture event plus explicit platform events, delivered over Server-Sent
  Events with replay (``Last-Event-ID`` / ``?since=``).
* **Sandboxes & scratch orgs** — full SQLite copies of the org database.
  Developer sandboxes copy metadata only, partial sandboxes copy metadata
  plus a sample of data, full sandboxes copy everything. Scratch orgs are
  sandboxes with an expiry date and are pruned automatically.
* **Source tracking** — list every metadata change since a timestamp, derived
  from the setup audit trail (the basis of a ``pull`` workflow).
* **Custom Metadata Types** (``__mdt``) and **Custom Settings** — deployable
  typed configuration records, readable from formulas/flows via
  ``{"custom_metadata": {"type": ..., "record": ..., "field": ...}}``.
* **Managed packages** — namespace + version on packages, install registry,
  upgrade guards (downgrades rejected).
* **External objects** (``__x``) — read-only virtual objects backed by an
  OData v4 service through a named credential.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from collections import deque

from .store import new_id, utcnow
from .field_types import is_valid_api_name, validate_value

BULK_JOB_TABLE = "mf_bulk_api_jobs"
SANDBOX_TABLE = "mf_sandboxes"
CMDT_TABLE = "mf_cmdt"
CMDT_RECORD_TABLE = "mf_cmdt_records"
CUSTOM_SETTING_TABLE = "mf_custom_settings"
INSTALLED_PACKAGE_TABLE = "mf_installed_packages"
EXTERNAL_OBJECT_TABLE = "mf_external_objects"


# ------------------------------------------------------------ streaming broker
class StreamBroker:
    """In-process pub/sub with a bounded replay buffer."""

    def __init__(self, buffer_size: int = 2000):
        self._cond = threading.Condition()
        self._buffer: deque = deque(maxlen=buffer_size)
        self._seq = 0

    def publish(self, topic: str, payload: dict) -> dict:
        with self._cond:
            self._seq += 1
            event = {"seq": self._seq, "topic": topic,
                     "at": utcnow(), "payload": payload}
            self._buffer.append(event)
            self._cond.notify_all()
            return event

    def events_since(self, since: int, topics: set[str] | None = None,
                    limit: int = 200):
        with self._cond:
            out = [e for e in self._buffer
                   if e["seq"] > since and (topics is None or e["topic"] in topics)]
            return out[-limit:]

    def wait(self, timeout: float):
        with self._cond:
            self._cond.wait(timeout)


broker = StreamBroker()


def publish_change_event(object_name: str, record_id: str, event: str,
                         user: dict, changed_fields=None, snapshot=None) -> dict:
    """Mirror a change-data-capture event onto the streaming broker."""
    return broker.publish(
        f"/data/{object_name}ChangeEvent",
        {"object_name": object_name, "record_id": record_id, "event": event,
         "user_id": (user or {}).get("id"), "username": (user or {}).get("username"),
         "changed_fields": changed_fields or [], "snapshot": snapshot or {}})


def publish_platform_event(name: str, payload: dict, user: dict | None = None) -> dict:
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
        raise ValueError("Invalid platform event name")
    return broker.publish(f"/event/{name}__e",
                          {"event_name": name, "payload": payload or {},
                           "published_by": (user or {}).get("username")})


# ------------------------------------------------------------ Bulk API 2.0 ingest
BULK_OPERATIONS = ("insert", "update", "upsert", "delete")
BULK_OPEN, BULK_UPLOADED = "Open", "UploadComplete"
BULK_RUNNING, BULK_DONE = "InProgress", "JobComplete"
BULK_FAILED, BULK_ABORTED = "Failed", "Aborted"
MAX_BULK_ROWS = 50000
MAX_CSV_BYTES = 10 * 1024 * 1024


def create_ingest_job(store, user, obj_name: str, operation: str,
                      external_id_field: str | None = None, registry=None) -> dict:
    from . import automation  # noqa: F401  (registry check helper lives in api layer)
    from . import datamodel as _datamodel
    operation = (operation or "insert").lower()
    if operation not in BULK_OPERATIONS:
        raise ValueError(f"operation must be one of {BULK_OPERATIONS}")
    if operation == "upsert" and not external_id_field:
        raise ValueError("externalIdFieldName is required for upsert")
    if registry is not None and operation in ("update", "upsert", "delete"):
        obj_def = registry.get_object(obj_name)
        if obj_def and _datamodel.is_big_object(obj_def):
            raise ValueError(f"{obj_name} is a Big Object and is append-only "
                             "(only insert jobs are allowed)")
    job = {"object": obj_name, "operation": operation,
           "external_id_field": external_id_field,
           "state": BULK_OPEN, "csv_text": "", "column_delimiter": ",",
           "processed": 0, "succeeded": 0, "failed": 0,
           "results": [], "error": "",
           "created_by": user["id"], "created_at": utcnow()}
    jid = store.config_put(BULK_JOB_TABLE, job)
    return store.config_get(BULK_JOB_TABLE, jid)


def upload_job_data(store, job_id: str, csv_text: str) -> dict:
    job = store.config_get(BULK_JOB_TABLE, job_id)
    if not job:
        raise KeyError("job not found")
    if job["state"] != BULK_OPEN:
        raise ValueError("job is not open for uploads")
    if len(csv_text.encode("utf-8")) > MAX_CSV_BYTES:
        raise ValueError("CSV exceeds 10 MB limit")
    try:
        rows = list(csv.DictReader(io.StringIO(csv_text),
                                   delimiter=job.get("column_delimiter") or ","))
    except Exception as e:
        raise ValueError(f"unparseable CSV: {e}")
    if len(rows) > MAX_BULK_ROWS:
        raise ValueError(f"CSV exceeds {MAX_BULK_ROWS} rows")
    job["csv_text"] = csv_text
    job["row_count"] = len(rows)
    store.config_put(BULK_JOB_TABLE, job)
    return job


def _process_ingest_job(db_path: str, job_id: str):
    """Background worker: run CSV rows through triggers/validation/flows."""
    from .bootstrap import bootstrap
    from . import automation
    bstore, bregistry, bsecurity = bootstrap(db_path)
    job = bstore.config_get(BULK_JOB_TABLE, job_id)
    if not job or job["state"] == BULK_ABORTED:
        return
    user = next((u for u in bsecurity.list_users()
                 if u["id"] == job.get("created_by")), None) \
        or bsecurity.get_user_by_username("admin")
    query, create, update = automation._trigger_dml_ops(
        bstore, bregistry, bsecurity, user, 0, [])
    obj_name, op = job["object"], job["operation"]
    ext_field = job.get("external_id_field")
    job["state"] = BULK_RUNNING
    bstore.config_put(BULK_JOB_TABLE, job)

    def delete(obj_name_, rid):
        from . import datamodel as _datamodel
        obj = bregistry.get_object(obj_name_)
        rec = obj and bstore.get(obj_name_, rid)
        if not rec:
            raise automation.TriggerAbort("Record not found")
        if not bsecurity.can(user, "delete", obj_name_):
            raise automation.TriggerAbort(f"No delete access on {obj_name_}")
        blocker = _datamodel.check_delete_blockers(bstore, bregistry, obj_name_, rid)
        if blocker:
            raise automation.TriggerAbort(blocker)
        errs = automation.run_triggers(bstore, bregistry, bsecurity, obj_name_,
                                       "before_delete", rec, None, user, 1)
        if errs:
            raise automation.TriggerAbort("; ".join(errs))
        _datamodel.cascade_delete(bstore, bregistry, user, obj_name_, rid)
        bstore.delete(obj_name_, rid)
        errs = automation.run_triggers(bstore, bregistry, bsecurity, obj_name_,
                                       "after_delete", rec, None, user, 1)
        if errs:
            raise automation.TriggerAbort("; ".join(errs))
        bstore.emit_change(obj_name_, rid, "delete", user,
                           changed_fields=list(rec.keys()), snapshot=rec)
        return rec

    def clean_row(row: dict, for_update: bool):
        out = {}
        for k, v in row.items():
            if k is None:
                continue
            k = k.strip()
            if k.lower() in ("id", "sf__id", "sf__created", "sf__error"):
                continue  # identity/result columns are handled separately
            if v == "" or v is None:
                if not for_update:
                    out[k] = None
                continue  # empty cell on update = leave the field alone
            out[k] = v
        return out

    rows = list(csv.DictReader(io.StringIO(job.get("csv_text") or ""),
                               delimiter=job.get("column_delimiter") or ","))
    ext_index = {}
    if op == "upsert" and ext_field:
        for r in query(obj_name):
            key = r.get(ext_field)
            if key not in (None, ""):
                ext_index[str(key)] = r["id"]
    ok, failed, results = 0, 0, []
    try:
        for i, raw in enumerate(rows):
            if i % 25 == 0:  # abort check
                cur = bstore.config_get(BULK_JOB_TABLE, job_id) or {}
                if cur.get("state") == BULK_ABORTED:
                    return
            rid, created, err = "", "false", ""
            try:
                if op == "insert":
                    rec = create(obj_name, clean_row(raw, False))
                    rid, created = rec["id"], "true"
                    bstore.emit_change(obj_name, rid, "create", user,
                                       changed_fields=list(raw.keys()), snapshot=rec)
                elif op == "update":
                    rid = (raw.get("Id") or raw.get("id") or "").strip()
                    if not rid:
                        raise ValueError("row is missing Id")
                    fields = clean_row(raw, True)
                    update(obj_name, rid, fields)
                    bstore.emit_change(obj_name, rid, "update", user,
                                       changed_fields=list(fields.keys()))
                elif op == "upsert":
                    fields = clean_row(raw, True)
                    key = str(raw.get(ext_field) or "")
                    match_id = ext_index.get(key)
                    if match_id:
                        rid = match_id
                        update(obj_name, rid,
                               {k: v for k, v in fields.items() if k != ext_field})
                        bstore.emit_change(obj_name, rid, "update", user,
                                           changed_fields=list(fields.keys()))
                    else:
                        rec = create(obj_name, clean_row(raw, False))
                        rid, created = rec["id"], "true"
                        if key:
                            ext_index[key] = rid
                        bstore.emit_change(obj_name, rid, "create", user,
                                           changed_fields=list(raw.keys()), snapshot=rec)
                elif op == "delete":
                    rid = (raw.get("Id") or raw.get("id") or "").strip()
                    if not rid:
                        raise ValueError("row is missing Id")
                    delete(obj_name, rid)
                else:
                    raise ValueError(f"unknown operation '{op}'")
                ok += 1
            except Exception as e:  # noqa: BLE001 - per-row errors are collected
                failed += 1
                err = f"{type(e).__name__}: {e}"[:500]
                if not rid:
                    rid = (raw.get("Id") or raw.get("id") or "")
            results.append({"sf__Id": rid, "sf__Created": created, "sf__Error": err})
            if i % 50 == 0:
                job["processed"], job["succeeded"], job["failed"] = i + 1, ok, failed
                bstore.config_put(BULK_JOB_TABLE, job)
    except Exception as e:  # noqa: BLE001 - job-level failure
        job.update({"state": BULK_FAILED, "error": str(e)[:500]})
        bstore.config_put(BULK_JOB_TABLE, job)
        return
    job.update({"state": BULK_DONE, "processed": ok + failed,
                "succeeded": ok, "failed": failed, "results": results, "error": ""})
    bstore.config_put(BULK_JOB_TABLE, job)


def close_job(store, job_id: str) -> dict:
    job = store.config_get(BULK_JOB_TABLE, job_id)
    if not job:
        raise KeyError("job not found")
    if job["state"] != BULK_OPEN:
        raise ValueError("only Open jobs can be closed for processing")
    if not (job.get("csv_text") or "").strip():
        raise ValueError("no CSV data uploaded")
    job["state"] = BULK_UPLOADED
    store.config_put(BULK_JOB_TABLE, job)
    t = threading.Thread(target=_process_ingest_job,
                         args=(store.db_path, job_id), daemon=True)
    t.start()
    return store.config_get(BULK_JOB_TABLE, job_id)


def abort_job(store, job_id: str) -> dict:
    job = store.config_get(BULK_JOB_TABLE, job_id)
    if not job:
        raise KeyError("job not found")
    if job["state"] in (BULK_DONE, BULK_FAILED):
        raise ValueError("completed jobs cannot be aborted")
    job["state"] = BULK_ABORTED
    store.config_put(BULK_JOB_TABLE, job)
    return job


def job_results_csv(store, job_id: str, kind: str) -> str:
    """Return successful/failed result rows as CSV (Bulk API v2 style)."""
    job = store.config_get(BULK_JOB_TABLE, job_id)
    if not job:
        raise KeyError("job not found")
    header = [h for h in (csv.DictReader(
        io.StringIO(job.get("csv_text") or "")).fieldnames or [])
        if h and h.lower() not in ("sf__id", "sf__created", "sf__error")]
    rows = list(csv.DictReader(io.StringIO(job.get("csv_text") or "")))
    buf = io.StringIO()
    if kind == "successful":
        w = csv.DictWriter(buf, fieldnames=[*header, "sf__Id", "sf__Created"])
        w.writeheader()
        for raw, res in zip(rows, job.get("results") or []):
            if not res.get("sf__Error"):
                w.writerow({h: raw.get(h, "") for h in header}
                           | {"sf__Id": res["sf__Id"],
                              "sf__Created": res["sf__Created"]})
    else:
        w = csv.DictWriter(buf, fieldnames=[*header, "sf__Id", "sf__Error"])
        w.writeheader()
        for raw, res in zip(rows, job.get("results") or []):
            if res.get("sf__Error"):
                w.writerow({h: raw.get(h, "") for h in header}
                           | {"sf__Id": res["sf__Id"],
                              "sf__Error": res["sf__Error"]})
    return buf.getvalue()


# ------------------------------------------------------------ sandboxes & scratch orgs
SANDBOX_KINDS = ("developer", "partial", "full")
PARTIAL_SAMPLE_ROWS = 200


def sanitize_sandbox_db(db_path: str) -> dict:
    """Strip live credentials from a sandbox database copy.

    A sandbox is a byte copy of the production database; without this step
    it would hand out working production credentials (sessions, API keys,
    password hashes, TOTP secrets, named-credential secrets). Runs after
    every sandbox create/refresh, for every sandbox kind — developer and
    partial sandboxes keep config/metadata tables too.

    What it does:
      * deletes all session, refresh-token, and API-key rows
      * replaces every user's password hash with an unusable random value
        and sets ``must_change_password`` so the existing forced-change
        login flow applies
      * clears TOTP secrets
      * clears named-credential secret values (names/endpoints kept)
      * leaves encrypted business-field data untouched (same key decrypts)

    Returns a dict of counts for audit purposes.
    """
    import secrets as _secrets
    counts = {"sessions": 0, "refresh_tokens": 0, "api_keys": 0,
              "users": 0, "named_credentials": 0}
    con = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for table, key in (("mf_sessions", "sessions"),
                           ("mf_refresh_tokens", "refresh_tokens"),
                           ("mf_api_keys", "api_keys")):
            if table in tables:
                cur = con.execute(f'DELETE FROM "{table}"')
                counts[key] = cur.rowcount if cur.rowcount >= 0 else 0
        if "mf_users" in tables:
            for uid, definition in con.execute(
                    "SELECT id, definition FROM mf_users").fetchall():
                try:
                    u = json.loads(definition or "{}")
                except Exception:
                    continue
                # "!" prefix can never verify (verify_password expects
                # pbkdf2$iter$salt$dk); the random tail avoids collisions.
                u["password_hash"] = "!" + _secrets.token_hex(32)
                u.pop("totp_secret", None)
                u["must_change_password"] = True
                con.execute("UPDATE mf_users SET definition=? WHERE id=?",
                            (json.dumps(u), uid))
                counts["users"] += 1
        if "mf_named_credentials" in tables:
            for rid, definition in con.execute(
                    "SELECT id, definition FROM mf_named_credentials").fetchall():
                try:
                    c = json.loads(definition or "{}")
                except Exception:
                    continue
                if "secret_enc" in c or "secret" in c:
                    c.pop("secret_enc", None)
                    c.pop("secret", None)
                    con.execute(
                        "UPDATE mf_named_credentials SET definition=? WHERE id=?",
                        (json.dumps(c), rid))
                    counts["named_credentials"] += 1
        con.commit()
    finally:
        con.close()
    return counts


def sandbox_root() -> str:
    root = os.environ.get("FORCELET_SANDBOX_DIR") or os.path.join(
        os.path.expanduser("~"), ".forcelet", "sandboxes")
    os.makedirs(root, exist_ok=True)
    return root


def _data_tables(db_path: str) -> list[str]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'sobj_%'"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


def create_sandbox(store, user, name: str, kind: str = "developer",
                   scratch: bool = False, expires_in_days: int | None = None,
                   sanitize: bool = True) -> dict:
    kind = (kind or "developer").lower()
    if kind not in SANDBOX_KINDS:
        raise ValueError(f"kind must be one of {SANDBOX_KINDS}")
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,40}", name):
        raise ValueError("name must be 2-41 chars: letters, digits, _ or -")
    if any(s["name"].lower() == name.lower() for s in store.config_all(SANDBOX_TABLE)):
        raise ValueError(f"sandbox '{name}' already exists")
    dest_dir = os.path.join(sandbox_root(), name)
    os.makedirs(dest_dir, exist_ok=False)
    dest = os.path.join(dest_dir, "forcelet.db")
    src = sqlite3.connect(store.db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    if kind in ("developer", "partial"):
        # Developer = metadata only; Partial = metadata + a sample of records.
        con = sqlite3.connect(dest)
        try:
            for table in _data_tables(dest):
                if kind == "developer":
                    con.execute(f'DELETE FROM "{table}"')
                else:
                    con.execute(
                        f'DELETE FROM "{table}" WHERE rowid NOT IN '
                        f'(SELECT rowid FROM "{table}" ORDER BY rowid DESC '
                        f'LIMIT {PARTIAL_SAMPLE_ROWS})')
            con.commit()
        finally:
            con.close()
    if sanitize:
        # Never ship live production credentials in a sandbox copy.
        sanitize_sandbox_db(dest)
    expires_at = None
    if scratch:
        days = expires_in_days or 7
        if not 1 <= days <= 30:
            raise ValueError("scratch org expiry must be 1-30 days")
        from datetime import datetime, timedelta, timezone
        expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(
            timespec="seconds")
    rec = {"name": name, "kind": kind, "scratch": bool(scratch),
           "db_path": dest, "status": "active",
           "created_by": user["id"], "created_at": utcnow(),
           "expires_at": expires_at}
    rid = store.config_put(SANDBOX_TABLE, rec)
    return store.config_get(SANDBOX_TABLE, rid)


def refresh_sandbox(store, sandbox_id: str, sanitize: bool = True) -> dict:
    sb = store.config_get(SANDBOX_TABLE, sandbox_id)
    if not sb:
        raise KeyError("sandbox not found")
    dest = sb["db_path"]
    if not os.path.exists(dest):
        raise ValueError("sandbox database file is missing")
    src = sqlite3.connect(store.db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            # backup() overwrites the target; wrap in a fresh copy of current prod.
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    if sb["kind"] in ("developer", "partial"):
        con = sqlite3.connect(dest)
        try:
            for table in _data_tables(dest):
                if sb["kind"] == "developer":
                    con.execute(f'DELETE FROM "{table}"')
                else:
                    con.execute(
                        f'DELETE FROM "{table}" WHERE rowid NOT IN '
                        f'(SELECT rowid FROM "{table}" ORDER BY rowid DESC '
                        f'LIMIT {PARTIAL_SAMPLE_ROWS})')
            con.commit()
        finally:
            con.close()
    if sanitize:
        # A refresh is a fresh production copy: sanitize credentials again.
        sanitize_sandbox_db(dest)
    sb["status"] = "active"
    sb["refreshed_at"] = utcnow()
    store.config_put(SANDBOX_TABLE, sb)
    return sb


def delete_sandbox(store, sandbox_id: str) -> bool:
    sb = store.config_get(SANDBOX_TABLE, sandbox_id)
    if not sb:
        return False
    parent = os.path.dirname(sb.get("db_path") or "")
    if parent and os.path.isdir(parent) \
            and os.path.abspath(parent).startswith(sandbox_root() + os.sep):
        shutil.rmtree(parent, ignore_errors=True)
    return store.config_delete(SANDBOX_TABLE, sandbox_id)


def list_sandboxes(store) -> list[dict]:
    """List sandboxes, pruning expired scratch orgs first."""
    now = utcnow()
    for sb in store.config_all(SANDBOX_TABLE):
        if sb.get("scratch") and sb.get("expires_at") and sb["expires_at"] < now:
            delete_sandbox(store, sb["id"])
    return store.config_all(SANDBOX_TABLE)


# ------------------------------------------------------------ source tracking
_COMPONENT_FALLBACK = {
    "object": "CustomObject", "field": "CustomField", "layout": "Layout",
    "flow": "Flow", "app": "CustomApplication", "profile": "Profile",
    "role": "Role", "permission-set": "PermissionSet",
    "validation-rule": "ValidationRule", "approval-process": "ApprovalProcess",
    "assignment-rule": "AssignmentRule", "named-credential": "NamedCredential",
    "webhook": "Webhook", "trigger": "ApexTrigger", "report": "Report",
    "dashboard": "Dashboard", "email-template": "EmailTemplate",
    "record-type": "RecordType", "path": "Path", "macro": "Macro",
    "case-queue": "Queue", "sla-policy": "SLAPolicy",
    "custom-metadata-type": "CustomMetadataType",
    "custom-setting": "CustomSetting", "external-object": "ExternalObject",
    "bulk-job": "BulkJob", "scheduled-job": "ScheduledJob",
    "package": "Package", "sharing-rule": "SharingRule",
    "matching-rule": "MatchingRule", "forecast-quota": "ForecastQuota",
}


def source_changes(store, since: str | None = None, limit: int = 1000) -> dict:
    """Metadata changes since an ISO timestamp — the basis of source pull."""
    since = (since or "1970-01-01T00:00:00").strip()
    rows = store._execute(
        "SELECT at, user_id, username, action, entity_type, entity_name, details"
        " FROM mf_audit_trail WHERE at > ? ORDER BY at ASC LIMIT ?",
        (since, max(1, min(int(limit), 5000)))).fetchall()
    skip = {"login", "logout", "totp"}
    changes = []
    for r in rows:
        etype = r["entity_type"] or ""
        if etype in skip or etype.startswith("api-key") or etype.startswith("refresh"):
            continue
        changes.append({
            "type": _COMPONENT_FALLBACK.get(
                etype, "".join(p.capitalize() for p in etype.split("-")) or "Unknown"),
            "action": r["action"], "name": r["entity_name"],
            "at": r["at"], "user": r["username"],
            "details": (r["details"] or "")[:300],
        })
    server_time = utcnow()
    latest = max([c["at"] for c in changes], default=since)
    return {"changes": changes, "count": len(changes),
            "since": since, "server_time": server_time,
            "latest_change": latest if changes else None}


# ------------------------------------------------------------ custom metadata types & settings
CMDT_FIELD_TYPES = ("Text", "TextArea", "Number", "Checkbox", "Date",
                    "DateTime", "Picklist", "Percent", "Currency", "URL", "Email")


def _validate_cmdt(defn: dict, for_update: bool = False):
    label = (defn.get("label") or "").strip()
    api_name = (defn.get("api_name") or "").strip()
    if not label:
        raise ValueError("label is required")
    if not is_valid_api_name(api_name) or not api_name.endswith("__mdt"):
        raise ValueError("api_name must be a valid API name ending in __mdt")
    fields = defn.get("fields") or []
    if not isinstance(fields, list) or not fields:
        raise ValueError("at least one field is required")
    seen = set()
    for f in fields:
        fname = (f.get("name") or "").strip()
        if not is_valid_api_name(fname):
            raise ValueError(f"invalid field name '{fname}'")
        if fname in seen:
            raise ValueError(f"duplicate field name '{fname}'")
        seen.add(fname)
        if f.get("type") not in CMDT_FIELD_TYPES:
            raise ValueError(f"field '{fname}': unsupported type '{f.get('type')}'")
        if f.get("type") == "picklist" and not f.get("options"):
            raise ValueError(f"picklist field '{fname}' needs options")
    return label, api_name


def create_cmdt(store, user, defn: dict) -> dict:
    label, api_name = _validate_cmdt(defn)
    if any(t["api_name"].lower() == api_name.lower()
           for t in store.config_all(CMDT_TABLE)):
        raise ValueError(f"custom metadata type '{api_name}' already exists")
    rec = {"label": label, "api_name": api_name,
           "description": (defn.get("description") or "").strip(),
           "fields": defn["fields"],
           "created_by": user["id"], "created_at": utcnow()}
    rid = store.config_put(CMDT_TABLE, rec)
    return store.config_get(CMDT_TABLE, rid)


def update_cmdt(store, type_id: str, defn: dict) -> dict:
    old = store.config_get(CMDT_TABLE, type_id)
    if not old:
        raise KeyError("custom metadata type not found")
    merged = {**old, **{k: v for k, v in defn.items() if k != "api_name"}}
    _validate_cmdt(merged, for_update=True)
    store.config_put(CMDT_TABLE, merged)
    return merged


def delete_cmdt(store, type_id: str) -> bool:
    for r in store.config_all(CMDT_RECORD_TABLE):
        if r.get("type_id") == type_id:
            store.config_delete(CMDT_RECORD_TABLE, r["id"])
    return store.config_delete(CMDT_TABLE, type_id)


def get_cmdt(store, api_name: str):
    return next((t for t in store.config_all(CMDT_TABLE)
                 if t["api_name"].lower() == (api_name or "").lower()), None)


def _validate_cmdt_record(cmdt: dict, developer_name: str, values: dict):
    developer_name = (developer_name or "").strip()
    if not is_valid_api_name(developer_name):
        raise ValueError("developer_name must be a valid API name")
    clean = {}
    fmap = {f["name"]: f for f in cmdt.get("fields", [])}
    for fname, fdef in fmap.items():
        if fname in (values or {}):
            ok, norm, err = validate_value(
                {"type": fdef["type"], "label": fdef.get("label", fname),
                 "required": fdef.get("required"), "options": fdef.get("options")},
                values[fname])
            if not ok:
                raise ValueError(f"field '{fname}': {err}")
            clean[fname] = norm
        elif fdef.get("required"):
            raise ValueError(f"field '{fname}' is required")
        elif "default" in fdef:
            clean[fname] = fdef["default"]
    unknown = set((values or {})) - set(fmap)
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
    return developer_name, clean


def create_cmdt_record(store, user, type_id: str, developer_name: str,
                       values: dict) -> dict:
    cmdt = store.config_get(CMDT_TABLE, type_id)
    if not cmdt:
        raise KeyError("custom metadata type not found")
    developer_name, clean = _validate_cmdt_record(cmdt, developer_name, values)
    if any(r.get("type_id") == type_id
           and r.get("developer_name", "").lower() == developer_name.lower()
           for r in store.config_all(CMDT_RECORD_TABLE)):
        raise ValueError(f"record '{developer_name}' already exists")
    rec = {"type_id": type_id, "type_api_name": cmdt["api_name"],
           "developer_name": developer_name, "values": clean,
           "created_by": user["id"], "created_at": utcnow()}
    rid = store.config_put(CMDT_RECORD_TABLE, rec)
    return store.config_get(CMDT_RECORD_TABLE, rid)


def update_cmdt_record(store, record_id: str, developer_name: str,
                       values: dict) -> dict:
    old = store.config_get(CMDT_RECORD_TABLE, record_id)
    if not old:
        raise KeyError("record not found")
    cmdt = store.config_get(CMDT_TABLE, old["type_id"])
    if not cmdt:
        raise KeyError("custom metadata type not found")
    developer_name, clean = _validate_cmdt_record(cmdt, developer_name, values)
    for r in store.config_all(CMDT_RECORD_TABLE):
        if r["id"] != record_id and r.get("type_id") == old["type_id"] \
                and r.get("developer_name", "").lower() == developer_name.lower():
            raise ValueError(f"record '{developer_name}' already exists")
    old.update({"developer_name": developer_name, "values": clean})
    store.config_put(CMDT_RECORD_TABLE, old)
    return old


def custom_metadata_value(store, type_name: str, record_name: str, field_name: str):
    """Resolver for {"custom_metadata": ...} expressions in formulas/flows."""
    cmdt = get_cmdt(store, type_name or "")
    if not cmdt:
        raise ValueError(f"Unknown custom metadata type '{type_name}'")
    rec = next((r for r in store.config_all(CMDT_RECORD_TABLE)
                if r.get("type_id") == cmdt["id"]
                and r.get("developer_name", "").lower() == (record_name or "").lower()),
               None)
    if not rec:
        raise ValueError(f"Unknown record '{record_name}' on {type_name}")
    if field_name not in {f["name"] for f in cmdt.get("fields", [])}:
        raise ValueError(f"Unknown field '{field_name}' on {type_name}")
    return (rec.get("values") or {}).get(field_name)


def register_expression_resolvers(store):
    """Wire $CustomMetadata / $CustomSetting into the expression engine."""
    from . import expressions
    expressions.set_custom_metadata_resolver(
        lambda t, r, f: custom_metadata_value(store, t, r, f))
    expressions.set_custom_setting_resolver(
        lambda n, f: custom_setting_value(store, n, f))


# ------------------------------------------------------------ custom settings
def _validate_setting(defn: dict):
    name = (defn.get("name") or "").strip()
    if not is_valid_api_name(name):
        raise ValueError("name must be a valid API name")
    values = defn.get("values")
    if not isinstance(values, dict):
        raise ValueError("values must be an object")
    return name


def upsert_custom_setting(store, user, defn: dict) -> dict:
    name = _validate_setting(defn)
    existing = next((s for s in store.config_all(CUSTOM_SETTING_TABLE)
                     if s["name"].lower() == name.lower()), None)
    rec = {"name": name, "label": (defn.get("label") or name).strip(),
           "description": (defn.get("description") or "").strip(),
           "values": defn["values"],
           "updated_by": user["id"], "updated_at": utcnow()}
    created = not existing
    if existing:
        rec["id"] = existing["id"]
    rid = store.config_put(CUSTOM_SETTING_TABLE, rec)
    return store.config_get(CUSTOM_SETTING_TABLE, rid), created


def custom_setting_value(store, name: str, field_name: str):
    rec = next((s for s in store.config_all(CUSTOM_SETTING_TABLE)
                if s["name"].lower() == (name or "").lower()), None)
    if not rec:
        raise ValueError(f"Unknown custom setting '{name}'")
    return (rec.get("values") or {}).get(field_name)


def export_custom_metadata_package(store) -> dict:
    """Portable custom-metadata payload with type api_names (no local ids)."""
    return {
        "types": [{k: t[k] for k in ("label", "api_name", "description", "fields")}
                  for t in store.config_all(CMDT_TABLE)],
        "records": [{"type_api_name": r.get("type_api_name"),
                     "developer_name": r.get("developer_name"),
                     "values": r.get("values") or {}}
                    for r in store.config_all(CMDT_RECORD_TABLE)],
    }


def import_custom_metadata_package(store, user, data: dict) -> dict:
    data = data or {}
    n_types = n_records = 0
    for t in data.get("types", []):
        existing = get_cmdt(store, t.get("api_name") or "")
        if existing:
            update_cmdt(store, existing["id"], t)
        else:
            create_cmdt(store, user, t)
        n_types += 1
    for r in data.get("records", []):
        cmdt = get_cmdt(store, r.get("type_api_name") or "")
        if not cmdt:
            continue
        existing = next(
            (x for x in store.config_all(CMDT_RECORD_TABLE)
             if x.get("type_id") == cmdt["id"]
             and x.get("developer_name", "").lower()
             == (r.get("developer_name") or "").lower()), None)
        if existing:
            update_cmdt_record(store, existing["id"], r.get("developer_name"),
                               r.get("values") or {})
        else:
            create_cmdt_record(store, user, cmdt["id"], r.get("developer_name"),
                               r.get("values") or {})
        n_records += 1
    return {"types": n_types, "records": n_records}


# ------------------------------------------------------------ managed packages
def parse_version(v: str) -> tuple:
    parts = []
    for p in (v or "").strip().split("."):
        m = re.fullmatch(r"(\d+)(.*)", p)
        parts.append((int(m.group(1)) if m else 0, m.group(2) if m else p))
    return tuple(parts) or ((0, ""),)


def check_package_install(store, pkg: dict):
    """Enforce managed-package install/upgrade rules. Returns install record or None."""
    ns = (pkg.get("namespace") or "").strip()
    if not ns:
        return None
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,14}", ns):
        raise ValueError("namespace must be 2-15 chars: letters, digits, _")
    version = (pkg.get("version") or "1.0").strip()
    existing = next((p for p in store.config_all(INSTALLED_PACKAGE_TABLE)
                     if p.get("namespace", "").lower() == ns.lower()), None)
    if existing:
        if parse_version(version) <= parse_version(existing.get("version") or "0"):
            raise ValueError(
                f"Package '{ns}' version {existing.get('version')} is already "
                f"installed; cannot install {version} (downgrades and reinstalls "
                f"of the same version are rejected)")
    return {"namespace": ns, "version": version, "existing": existing}


def record_package_install(store, user, pkg: dict, summary: dict) -> dict:
    ns = (pkg.get("namespace") or "").strip()
    rec = {"namespace": ns,
           "name": pkg.get("name") or ns,
           "version": (pkg.get("version") or "1.0").strip(),
           "managed": bool(pkg.get("managed")),
           "installed_by": user["id"], "installed_at": utcnow(),
           "summary": summary}
    existing = next((p for p in store.config_all(INSTALLED_PACKAGE_TABLE)
                     if p.get("namespace", "").lower() == ns.lower()), None)
    if existing:
        rec["id"] = existing["id"]
    rid = store.config_put(INSTALLED_PACKAGE_TABLE, rec)
    return store.config_get(INSTALLED_PACKAGE_TABLE, rid)


# ------------------------------------------------------------ external objects
def _validate_external_object(defn: dict):
    label = (defn.get("label") or "").strip()
    api_name = (defn.get("api_name") or "").strip()
    if not label:
        raise ValueError("label is required")
    if not is_valid_api_name(api_name) or not api_name.endswith("__x"):
        raise ValueError("api_name must be a valid API name ending in __x")
    url = (defn.get("odata_url") or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError("odata_url must start with http(s)://")
    entity_set = (defn.get("entity_set") or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", entity_set):
        raise ValueError("entity_set must be a valid OData entity set name")
    return label, api_name, url, entity_set


def upsert_external_object(store, user, defn: dict) -> dict:
    label, api_name, url, entity_set = _validate_external_object(defn)
    existing = next((o for o in store.config_all(EXTERNAL_OBJECT_TABLE)
                     if o["api_name"].lower() == api_name.lower()
                     and o.get("id") != defn.get("id")), None)
    if existing:
        raise ValueError(f"external object '{api_name}' already exists")
    rec = {"label": label, "api_name": api_name, "odata_url": url,
           "entity_set": entity_set,
           "named_credential": (defn.get("named_credential") or "").strip(),
           "key_field": (defn.get("key_field") or "Id").strip(),
           "description": (defn.get("description") or "").strip(),
           "updated_by": user["id"], "updated_at": utcnow()}
    created = True
    if defn.get("id"):
        rec["id"] = defn["id"]
        created = store.config_get(EXTERNAL_OBJECT_TABLE, defn["id"]) is None
    rid = store.config_put(EXTERNAL_OBJECT_TABLE, rec)
    return store.config_get(EXTERNAL_OBJECT_TABLE, rid), created


def query_external_object(store, api_name: str, params: dict) -> dict:
    """Query an external object through its OData v4 service.

    ``params`` may carry OData query options ($top, $filter, $select,
    $orderby, $skip, $count); anything else is ignored.
    """
    from . import automation
    obj = next((o for o in store.config_all(EXTERNAL_OBJECT_TABLE)
                if o["api_name"].lower() == (api_name or "").lower()), None)
    if not obj:
        raise KeyError("external object not found")
    allowed = ("$top", "$filter", "$select", "$orderby", "$skip", "$count",
               "$expand", "$search")
    q = {k: v for k, v in (params or {}).items() if k in allowed and v not in (None, "")}
    from urllib.parse import urlencode
    path = f"/{obj['entity_set']}" + (("?" + urlencode(q)) if q else "")
    if obj.get("named_credential"):
        res = automation.invoke_callout(store, obj["named_credential"], "GET", path,
                                        headers={"Accept": "application/json"})
    else:
        import urllib.request
        try:
            req = urllib.request.Request(obj["odata_url"] + path,
                                         headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                res = {"ok": 200 <= resp.status < 300, "status": resp.status,
                       "body": resp.read().decode("utf-8", "replace"), "error": None}
        except Exception as e:  # noqa: BLE001 - surfaced as callout error
            res = {"ok": False, "status": None, "body": None, "error": str(e)}
    if not res.get("ok"):
        raise ValueError(f"OData callout failed: {res.get('error') or res.get('status')}")
    try:
        data = json.loads(res.get("body") or "{}")
    except Exception:
        raise ValueError("OData service did not return JSON")
    records = data.get("value", data) if isinstance(data, dict) else data
    return {"object": obj["api_name"], "entity_set": obj["entity_set"],
            "records": records if isinstance(records, list) else [records],
            "odata_count": data.get("@odata.count") if isinstance(data, dict) else None}
