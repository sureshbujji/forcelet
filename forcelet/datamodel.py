"""App data-model platform features.

- Master-detail relationships: required parent reference, cascade delete,
  sharing inheritance, reparenting control.
- Person Accounts: org-level enablement adding person fields to Account.
- Territory management: territory hierarchy, assignment rules, user mapping,
  territory-based account sharing.
- Big Objects: append-only high-volume objects + archival rules that move
  aged records into them.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import json

from .expressions import eval_expr, record_context

MAX_MD_DEPTH = 5


# ------------------------------------------------------- master-detail
def md_fields(obj_def: dict) -> list:
    """MasterDetail field definitions on an object."""
    return [f for f in obj_def.get("fields", []) if f.get("type") == "MasterDetail"]


def relationship_fields(obj_def: dict) -> list:
    return [f for f in obj_def.get("fields", [])
            if f.get("type") in ("Lookup", "MasterDetail")]


def validate_master_detail(registry, obj_name: str, field: dict):
    """Validate a MasterDetail field definition. Raises ValueError."""
    target = field.get("reference_to")
    if not target:
        raise ValueError("MasterDetail fields require 'reference_to' (parent object)")
    if not registry.get_object(target):
        raise ValueError(f"MasterDetail target object '{target}' does not exist")
    if target == obj_name:
        raise ValueError("A master-detail field cannot reference its own object")
    # cycle check: walk reference_to chains from the target back toward obj_name
    seen, stack = set(), [target]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if cur == obj_name:
            raise ValueError("Master-detail would create a relationship cycle")
        cur_def = registry.get_object(cur)
        if not cur_def:
            continue
        stack.extend(f.get("reference_to") for f in relationship_fields(cur_def)
                     if f.get("reference_to"))
    for flag in ("unique", "external_id", "encrypted"):
        if field.get(flag):
            raise ValueError(f"MasterDetail fields cannot be {flag}")
    if field.get("formula") or field.get("rollup"):
        raise ValueError("MasterDetail fields cannot be computed")


def md_parents(obj_def: dict, record: dict) -> list:
    """(parent_object, parent_id) pairs from a record's master-detail fields."""
    out = []
    for f in md_fields(obj_def):
        pid = record.get(f["name"])
        if pid:
            out.append((f["reference_to"], pid))
    return out


def validate_md_parents_exist(store, obj_def: dict, values: dict) -> str | None:
    """Error string when a master-detail value points at a missing parent."""
    for f in md_fields(obj_def):
        if f["name"] in values and values[f["name"]]:
            try:
                parent = store.get(f["reference_to"], values[f["name"]])
            except Exception:
                parent = None
            if not parent:
                return (f"{f.get('label', f['name'])} references a "
                        f"{f['reference_to']} record that does not exist")
    return None


def cascade_delete(store, registry, user: dict, obj_name: str, rid: str,
                   depth: int = 0) -> list:
    """Recursively delete master-detail children of a record.

    Each child is recycled, change-logged, and deleted. Returns a list of
    (object, id) tuples that were cascade-deleted. Depth-guarded.
    """
    deleted = []
    if depth >= MAX_MD_DEPTH:
        return deleted
    for child_def in registry.list_objects():
        for f in md_fields(child_def):
            if f.get("reference_to") != obj_name:
                continue
            cobj = child_def["name"]
            try:
                rows = store.query(cobj, limit=100000)
            except Exception:
                continue
            for row in rows:
                if row.get(f["name"]) == rid:
                    # recurse first so grandchildren go before children
                    deleted.extend(cascade_delete(store, registry, user, cobj,
                                                  row["id"], depth + 1))
                    store.emit_change(cobj, row["id"], "delete", user,
                                      changed_fields=list(row.keys()),
                                      snapshot={k: v for k, v in row.items()})
                    store.recycle_put(cobj, row, user["id"])
                    store.delete(cobj, row["id"])
                    deleted.append((cobj, row["id"]))
    return deleted


# ------------------------------------------------------- person accounts
PERSON_ACCOUNT_FIELDS = [
    {"name": "FirstName", "label": "First Name", "type": "Text", "length": 80},
    {"name": "LastName", "label": "Last Name", "type": "Text", "length": 80},
    {"name": "PersonEmail", "label": "Person Email", "type": "Email"},
    {"name": "PersonPhone", "label": "Person Phone", "type": "Phone"},
    {"name": "IsPersonAccount", "label": "Is Person Account", "type": "Checkbox"},
]

PERSON_SETTINGS_TABLE = "mf_person_accounts"


def person_accounts_enabled(store) -> bool:
    doc = store.config_get(PERSON_SETTINGS_TABLE, "settings")
    return bool(doc and doc.get("enabled"))


def enable_person_accounts(store, registry) -> dict:
    """Add person fields to Account and ensure the PersonAccount record type."""
    account = registry.get_object("Account")
    if not account:
        raise ValueError("Account object does not exist")
    fmap = registry.field_map(account)
    added = []
    for spec in PERSON_ACCOUNT_FIELDS:
        if spec["name"] not in fmap:
            registry.add_field("Account", spec)
            added.append(spec["name"])
    # ensure record type
    existing = [rt for rt in store.config_all("mf_record_types")
                if rt.get("object") == "Account" and rt.get("name") == "PersonAccount"]
    if not existing:
        store.config_put("mf_record_types", {
            "name": "PersonAccount", "label": "Person Account",
            "object": "Account",
            "description": "Individual consumer account (person account)"})
    store.config_put(PERSON_SETTINGS_TABLE, {"id": "settings", "enabled": True,
                                             "enabled_at": __import__("datetime").datetime.now(
                                                 __import__("datetime").timezone.utc).isoformat()})
    return {"enabled": True, "fields_added": added}


def person_display_name(record: dict) -> str | None:
    if record.get("IsPersonAccount"):
        first = (record.get("FirstName") or "").strip()
        last = (record.get("LastName") or "").strip()
        full = f"{first} {last}".strip()
        if full:
            return full
    return None


# ------------------------------------------------------- territories
TERRITORY_TABLE = "mf_territories"
TERRITORY_RULE_TABLE = "mf_territory_rules"


def ensure_territory_tables(store):
    with store._lock:
        c = store.conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS mf_territory_members
                     (territory_id TEXT, user_id TEXT, role TEXT DEFAULT 'member',
                      PRIMARY KEY (territory_id, user_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_account_territories
                     (account_id TEXT, territory_id TEXT,
                      PRIMARY KEY (account_id, territory_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_record_territories
                     (object TEXT, record_id TEXT, territory_id TEXT,
                      PRIMARY KEY (object, record_id, territory_id))""")
        # one-time migration: legacy account assignments become object='Account'
        c.execute("""INSERT OR IGNORE INTO mf_record_territories
                     (object, record_id, territory_id)
                     SELECT 'Account', account_id, territory_id
                     FROM mf_account_territories""")
        store.conn.commit()


def territory_tree(store) -> list:
    """Territories nested as a tree (children lists)."""
    ensure_territory_tables(store)
    terrs = {t["id"]: {**t, "children": []} for t in store.config_all(TERRITORY_TABLE)}
    roots = []
    for t in terrs.values():
        parent = t.get("parent_id")
        if parent and parent in terrs:
            terrs[parent]["children"].append(t)
        else:
            roots.append(t)
    return roots


def territory_descendants(store, territory_id: str) -> set:
    """A territory plus all territories below it."""
    ensure_territory_tables(store)
    children = {}
    for t in store.config_all(TERRITORY_TABLE):
        children.setdefault(t.get("parent_id"), []).append(t["id"])
    out, stack = set(), [territory_id]
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.add(cur)
        stack.extend(children.get(cur, []))
    return out


def user_territory_ids(store, user_id: str) -> set:
    """Territories a user belongs to, including all descendants."""
    ensure_territory_tables(store)
    rows = store._execute("SELECT territory_id FROM mf_territory_members WHERE user_id=?",
                          (user_id,)).fetchall()
    out = set()
    for r in rows:
        out |= territory_descendants(store, r["territory_id"])
    return out


def assign_user_to_territory(store, territory_id: str, user_id: str, role: str = "member"):
    ensure_territory_tables(store)
    if not store.config_get(TERRITORY_TABLE, territory_id):
        raise ValueError("Unknown territory")
    store._execute("INSERT OR REPLACE INTO mf_territory_members (territory_id, user_id, role)"
                   " VALUES (?, ?, ?)", (territory_id, user_id, role))
    store._commit()


def remove_user_from_territory(store, territory_id: str, user_id: str) -> bool:
    ensure_territory_tables(store)
    cur = store._execute("DELETE FROM mf_territory_members WHERE territory_id=? AND user_id=?",
                         (territory_id, user_id))
    store._commit()
    return cur.rowcount > 0


def territory_members(store, territory_id: str) -> list:
    ensure_territory_tables(store)
    return [dict(r) for r in store._execute(
        "SELECT * FROM mf_territory_members WHERE territory_id=?", (territory_id,)).fetchall()]


def record_territories(store, obj_name: str, record_id: str) -> list:
    """Territory ids assigned to any record (per-object)."""
    ensure_territory_tables(store)
    return [r["territory_id"] for r in store._execute(
        "SELECT territory_id FROM mf_record_territories WHERE object=? AND record_id=?",
        (obj_name, record_id)).fetchall()]


def account_territories(store, account_id: str) -> list:
    return record_territories(store, "Account", account_id)


def run_territory_assignment(store, registry, rule_id: str | None = None) -> dict:
    """Evaluate active assignment rules; each rule targets its own object.

    Rules without an explicit object keep the legacy behavior (Account).
    Returns {"records_assigned": n, "rules_run": m} (plus the legacy
    "accounts_assigned" alias for backward compatibility).
    """
    ensure_territory_tables(store)
    rules = [r for r in store.config_all(TERRITORY_RULE_TABLE)
             if r.get("active", True) and (rule_id is None or r["id"] == rule_id)]
    rules.sort(key=lambda r: (r.get("priority") or 0))
    if rule_id and not rules:
        raise ValueError("Unknown or inactive rule")
    assigned = 0
    for rule in rules:
        criteria = rule.get("criteria") or {}
        tid = rule.get("territory_id")
        obj_name = rule.get("object") or "Account"
        if not tid or not store.config_get(TERRITORY_TABLE, tid):
            continue
        if not registry.get_object(obj_name):
            continue
        for rec in store.query(obj_name, limit=100000):
            try:
                match = eval_expr(criteria, record_context(rec))
            except Exception:
                match = False
            if match:
                store._execute("INSERT OR IGNORE INTO mf_record_territories"
                               " (object, record_id, territory_id) VALUES (?, ?, ?)",
                               (obj_name, rec["id"], tid))
                assigned += 1
    store._commit()
    return {"records_assigned": assigned, "accounts_assigned": assigned,
            "rules_run": len(rules)}


def territory_grants_access(store, user: dict, obj_name: str, record: dict) -> bool:
    """True when the user's territories cover this record (any object).

    Opportunities additionally inherit their account's territories
    (legacy behavior).
    """
    ensure_territory_tables(store)
    terr_ids = set(record_territories(store, obj_name, record.get("id")))
    if obj_name == "Opportunity":
        account_id = record.get("AccountId") or record.get("account_id")
        if account_id:
            terr_ids |= set(record_territories(store, "Account", account_id))
    if not terr_ids:
        return False
    return bool(terr_ids & user_territory_ids(store, user["id"]))


# ------------------------------------------------------- big objects
def is_big_object(obj_def: dict | None) -> bool:
    return bool(obj_def and obj_def.get("is_big_object"))


def assert_mutable(obj_def: dict):
    if is_big_object(obj_def):
        raise ValueError(f"{obj_def['name']} is a Big Object and is append-only "
                         "(records cannot be updated or deleted)")


ARCHIVE_RULE_TABLE = "mf_archive_rules"


def ensure_big_object(store, registry, name: str, label: str = "") -> dict:
    """Create a Big Object (__b) if it does not exist yet."""
    if not name.endswith("__b"):
        raise ValueError("Big Object API names must end with __b")
    existing = registry.get_object(name)
    if existing:
        if not is_big_object(existing):
            raise ValueError(f"Object '{name}' exists and is not a Big Object")
        return existing
    obj = registry.create_object(name, label or name[:-3], f"{label or name[:-3]}s")
    obj["is_big_object"] = True
    store.meta_put("mf_objects", name, obj)
    return obj


def run_archive_rule(store, registry, rule_id: str, user: dict) -> dict:
    """Move aged records of rule.object into its Big Object archive."""
    from datetime import datetime, timezone, timedelta
    rule = store.config_get(ARCHIVE_RULE_TABLE, rule_id)
    if not rule:
        raise ValueError("Unknown archive rule")
    obj_name = rule.get("object")
    obj_def = registry.get_object(obj_name)
    if not obj_def:
        raise ValueError(f"Unknown object '{obj_name}'")
    if is_big_object(obj_def):
        raise ValueError("Cannot archive from a Big Object")
    age_days = int(rule.get("age_days") or 365)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    target_name = rule.get("target") or f"{obj_name}Archive__b"
    target = ensure_big_object(store, registry, target_name, f"{obj_def.get('label')} Archive")
    # mirror source fields onto the archive object (once)
    tmap = registry.field_map(target)
    for f in obj_def.get("fields", []):
        if f["name"] not in tmap and not f.get("formula") and not f.get("rollup"):
            spec = {k: f.get(k) for k in ("name", "label", "type", "length",
                                          "picklist_values", "reference_to")}
            spec = {k: v for k, v in spec.items() if v is not None}
            try:
                registry.add_field(target_name, spec)
            except ValueError:
                pass  # e.g. MasterDetail on archive: store raw id as Text instead
    target = registry.get_object(target_name)  # re-fetch: add_field mutated the stored def
    tmap = registry.field_map(target)
    moved = 0
    for rec in store.query(obj_name, limit=100000):
        if (rec.get("created_date") or "") >= cutoff:
            continue
        archived = {"OriginalId": rec["id"]} if "OriginalId" in tmap else {}
        for fname in tmap:
            if fname in rec and fname != "OriginalId":
                archived[fname] = rec[fname]
        if "OriginalId" not in tmap:
            try:
                registry.add_field(target_name, {"name": "OriginalId", "label": "Original Id",
                                                 "type": "Text", "length": 15})
                tmap = registry.field_map(target)
            except ValueError:
                pass
            archived["OriginalId"] = rec["id"]
        store.insert(target_name, archived)
        store.delete(obj_name, rec["id"])
        moved += 1
    rule["last_run"] = datetime.now(timezone.utc).isoformat()
    rule["last_moved"] = moved
    rule["target"] = target_name
    store.config_put(ARCHIVE_RULE_TABLE, rule)
    return {"moved": moved, "target": target_name}


# ------------------------------------------------------- display helpers
def format_geolocation(value) -> str:
    if not value:
        return ""
    parts = str(value).split(";")
    if len(parts) == 2:
        return f"{parts[0]}, {parts[1]}"
    return str(value)


def format_address(value) -> str:
    if not value:
        return ""
    try:
        addr = json.loads(value) if isinstance(value, str) else value
    except Exception:
        return str(value)
    parts = [addr.get("street"), addr.get("city"), addr.get("state"),
             addr.get("postal_code"), addr.get("country")]
    return ", ".join(p for p in parts if p)
