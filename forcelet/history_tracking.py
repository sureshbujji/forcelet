"""Per-object field history tracking configuration. — Forcelet platform module.

The raw change log already lives in ``mf_history`` (written by
``automation.log_history`` on every record update).  This module adds the
Salesforce-style control plane on top of it:

* per-object enable/disable of history tracking,
* per-object field selection (empty list = track every field),
* per-object retention (days) with a purge routine.

Objects without a config row use the platform default: tracking enabled for
all fields, 180-day retention — which preserves the historical behaviour of
always logging.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .store import new_id, utcnow

TABLE = "mf_history_tracking"
HISTORY_TABLE = "mf_history"

DEFAULT_RETENTION_DAYS = 180


def _ensure(store) -> None:
    store._execute(
        f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            id TEXT PRIMARY KEY,
            object_name TEXT UNIQUE NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            fields TEXT NOT NULL DEFAULT '[]',
            retention_days INTEGER NOT NULL DEFAULT {DEFAULT_RETENTION_DAYS},
            updated_at TEXT NOT NULL)"""
    )
    store._commit()


def _decode(row: dict) -> dict:
    try:
        fields = json.loads(row.get("fields") or "[]")
    except Exception:
        fields = []
    return {
        "id": row["id"],
        "object_name": row["object_name"],
        "enabled": bool(row.get("enabled", 1)),
        "fields": fields if isinstance(fields, list) else [],
        "retention_days": int(row.get("retention_days") or DEFAULT_RETENTION_DAYS),
        "updated_at": row.get("updated_at"),
    }


def default_config(object_name: str) -> dict:
    return {
        "id": None,
        "object_name": object_name,
        "enabled": True,
        "fields": [],
        "retention_days": DEFAULT_RETENTION_DAYS,
        "updated_at": None,
    }


def get_config(store, object_name: str) -> dict | None:
    """Raw stored config, or None when the object was never configured."""
    _ensure(store)
    rows = store._execute(
        f"SELECT * FROM {TABLE} WHERE object_name=?", (object_name,)
    ).fetchall()
    return _decode(dict(rows[0])) if rows else None


def effective_config(store, object_name: str) -> dict:
    """Config merged with platform defaults (never None)."""
    return get_config(store, object_name) or default_config(object_name)


def set_config(store, object_name: str, enabled: bool = True,
               fields: list | None = None,
               retention_days: int = DEFAULT_RETENTION_DAYS) -> dict:
    """Create or replace the tracking config for an object."""
    _ensure(store)
    if retention_days is not None and int(retention_days) < 1:
        raise ValueError("retention_days must be >= 1")
    fields = list(fields or [])
    existing = get_config(store, object_name)
    row = {
        "id": existing["id"] if existing else new_id(),
        "object_name": object_name,
        "enabled": 1 if enabled else 0,
        "fields": json.dumps(fields),
        "retention_days": int(retention_days or DEFAULT_RETENTION_DAYS),
        "updated_at": utcnow(),
    }
    store._execute(
        f"""INSERT INTO {TABLE} (id, object_name, enabled, fields, retention_days, updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(object_name) DO UPDATE SET
              enabled=excluded.enabled, fields=excluded.fields,
              retention_days=excluded.retention_days, updated_at=excluded.updated_at""",
        (row["id"], row["object_name"], row["enabled"], row["fields"],
         row["retention_days"], row["updated_at"]),
    )
    store._commit()
    return _decode(row)


def list_configs(store, registry) -> list:
    """Every object with its effective config plus a history row count."""
    _ensure(store)
    out = []
    for obj in registry.list_objects():
        name = obj["name"]
        cfg = effective_config(store, name)
        n = store._execute(
            f"SELECT COUNT(*) c FROM {HISTORY_TABLE} WHERE object_name=?", (name,)
        ).fetchone()
        out.append({**cfg, "label": obj.get("label", name),
                    "history_rows": (n["c"] if n else 0)})
    return out


def tracked_field_names(store, object_name: str) -> set | None:
    """None → track every field; empty set → tracking disabled."""
    cfg = effective_config(store, object_name)
    if not cfg["enabled"]:
        return set()
    return set(cfg["fields"]) if cfg["fields"] else None


def purge_old_history(store) -> dict:
    """Delete history rows older than each object's retention window.

    Returns {object_name: deleted_row_count}.
    """
    _ensure(store)
    now = datetime.now(timezone.utc)
    result: dict = {}
    objects = {r["object_name"] for r in
               store._execute(f"SELECT DISTINCT object_name FROM {HISTORY_TABLE}").fetchall()}
    for object_name in sorted(objects):
        cfg = effective_config(store, object_name)
        cutoff = (now - timedelta(days=cfg["retention_days"])).isoformat()
        cur = store._execute(
            f"DELETE FROM {HISTORY_TABLE} WHERE object_name=? AND changed_at < ?",
            (object_name, cutoff),
        )
        if cur.rowcount:
            result[object_name] = cur.rowcount
    store._commit()
    return result
