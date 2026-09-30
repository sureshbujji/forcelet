"""Divisions: partition records into business units.

A division is a named business unit (e.g. "EMEA", "Commercial"). Records are
assigned to at most one division (or none = global); users see records in
their own division plus global records. A user's division comes from their
profile's ``default_division`` (see :mod:`forcelet.security`).

Record->division links live in the ``mf_record_divisions`` table (created by
migration 004), keyed by (object_name, record_id), so no per-object schema
change is needed. Division definitions live in the ``mf_divisions`` config
table.

Scope note: divisions filter *visibility* (list queries, record access).
They do not implement per-division sharing math (sharing rules, territory
inheritance, and role hierarchy keep working on top of the division filter).

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

TABLE = "mf_divisions"
RECORD_TABLE = "mf_record_divisions"


def get_division(store, division_id: str) -> dict | None:
    if not division_id:
        return None
    return store.config_get(TABLE, division_id)


def get_division_by_name(store, name: str) -> dict | None:
    want = (name or "").strip().lower()
    if not want:
        return None
    for d in store.config_all(TABLE):
        if (d.get("name") or "").strip().lower() == want:
            return d
    return None


def user_division_id(store, security, user: dict) -> str | None:
    """Return the division id for a user (from their profile), or None."""
    if not user:
        return None
    prof = security.get_profile(user.get("profile") or "")
    div_id = (prof or {}).get("default_division") or ""
    if div_id and get_division(store, div_id) and \
            get_division(store, div_id).get("active", True):
        return div_id
    return None


def record_division_id(store, obj_name: str, record_id: str) -> str | None:
    if not obj_name or not record_id:
        return None
    try:
        row = store._fetchone(
            f"SELECT division_id FROM {RECORD_TABLE} "
            "WHERE object_name=? AND record_id=?",
            (obj_name, record_id))
    except Exception:
        return None
    return row[0] if row else None


def set_record_division(store, obj_name: str, record_id: str,
                        division_id: str | None) -> None:
    if division_id:
        if not get_division(store, division_id):
            raise ValueError(f"Unknown division '{division_id}'")
    if division_id is None:
        # Global records have no row in the side table.
        clear_record_division(store, obj_name, record_id)
        return
    store._execute(
        f"INSERT OR REPLACE INTO {RECORD_TABLE} "
        "(object_name, record_id, division_id) VALUES (?, ?, ?)",
        (obj_name, record_id, division_id))
    store._commit()


def clear_record_division(store, obj_name: str, record_id: str) -> None:
    store._execute(
        f"DELETE FROM {RECORD_TABLE} WHERE object_name=? AND record_id=?",
        (obj_name, record_id))
    store._commit()


def record_division_name(store, obj_name: str, record_id: str) -> str:
    div_id = record_division_id(store, obj_name, record_id)
    div = get_division(store, div_id) if div_id else None
    return (div or {}).get("name", "")


def record_visible_to_user(store, security, user: dict,
                           obj_name: str | None, record: dict) -> bool:
    """Division visibility check.

    - System admins see everything.
    - A user with a division sees records in that division and global
      (division-less) records.
    - A user with no division sees only global records.
    """
    if security.is_admin(user):
        return True
    if not obj_name or not record or not record.get("id"):
        return True
    user_div = user_division_id(store, security, user)
    rec_div = record_division_id(store, obj_name, record["id"])
    if rec_div is None:
        return True  # global records are visible to everyone
    return user_div == rec_div


def count_records(store, division_id: str) -> int:
    row = store._fetchone(
        f"SELECT COUNT(*) FROM {RECORD_TABLE} WHERE division_id=?",
        (division_id,))
    return int(row[0]) if row else 0


def move_records(store, division_id: str | None,
                 moves: list[tuple[str, str]]) -> int:
    """Assign a list of (object_name, record_id) pairs to a division.

    ``division_id`` of None moves them back to global.
    """
    n = 0
    for obj_name, record_id in moves:
        set_record_division(store, obj_name, record_id, division_id)
        n += 1
    return n
