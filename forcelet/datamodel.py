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
            if f.get("type") in ("Lookup", "MasterDetail", "PolymorphicLookup")]


def _reference_targets(field: dict) -> list:
    """Object names a relationship field can point at (list for polymorphic)."""
    ref = field.get("reference_to")
    if isinstance(ref, list):
        return [r for r in ref if isinstance(r, str) and r]
    return [ref] if ref else []


# ------------------------------------------------------- auto-number fields
def next_auto_number(store, obj_name: str, field: dict) -> str:
    """Assign the next auto-number for a field: prefix + zero-padded sequence.

    The counter is per (object, field) and lives in the mf_sequences table;
    the increment is atomic under the store lock, so concurrent creates never
    hand out the same number (sequence gaps are possible if a later step of
    the create fails — same trade-off Salesforce makes).
    """
    key = f"{obj_name}.{field['name']}"
    start = field.get("auto_start")
    start = 1 if not isinstance(start, int) or isinstance(start, bool) or start < 0 else start
    seq = store.next_sequence(key, start=start)
    width = field.get("auto_width")
    width = 4 if not isinstance(width, int) or isinstance(width, bool) or not 1 <= width <= 10 else width
    prefix = field.get("auto_prefix") or ""
    return f"{prefix}{seq:0{width}d}"


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
        for f in relationship_fields(cur_def):
            stack.extend(_reference_targets(f))
    for flag in ("unique", "external_id", "encrypted"):
        if field.get(flag):
            raise ValueError(f"MasterDetail fields cannot be {flag}")
    if field.get("formula") or field.get("rollup") or field.get("type") == "Formula":
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


def resolve_lookup_value(store, registry, obj_name: str, field_def: dict, value) -> str | None:
    """Resolve a relationship value to a record Id. Raises ValueError when invalid.

    Accepts:
      - a plain Id string, existence-checked against the target object(s);
      - a dict ``{"<ExternalIdFieldName>": value}`` for indirect resolution
        (0 matches or more than 1 match raises);
      - None (or blank) for optional fields — passes through as None, but
        raises when the field is required.
    Works for Lookup, MasterDetail, and PolymorphicLookup fields.
    """
    label = field_def.get("label") or field_def.get("name") or "Field"
    ftype = field_def.get("type")
    if value is None or (isinstance(value, str) and value.strip() == ""):
        if field_def.get("required") or ftype == "MasterDetail":
            raise ValueError(f"{label} is required")
        return None
    targets = _reference_targets(field_def)
    targets = [t for t in targets if registry.get_object(t)]
    if not targets:
        raise ValueError(f"{label} has no valid target object configured")
    if isinstance(value, dict):
        if len(value) != 1:
            raise ValueError(f"{label}: indirect reference must be a single "
                             "{<ExternalIdField>: value} mapping")
        (ext_field, ext_val), = value.items()
        for target in targets:
            if ext_field not in registry.field_map(registry.get_object(target)):
                raise ValueError(f"{label}: '{ext_field}' is not a field on {target}")
        matches = []
        for target in targets:
            try:
                rows = store.query(target, limit=100000)
            except Exception:
                continue
            for row in rows:
                if row.get(ext_field) is not None and str(row.get(ext_field)) == str(ext_val):
                    matches.append((target, row["id"]))
        if not matches:
            raise ValueError(f"{label}: no {', '.join(targets)} record found "
                             f"with {ext_field}='{ext_val}'")
        if len(matches) > 1:
            raise ValueError(f"{label}: multiple records match {ext_field}='{ext_val}' "
                             f"({len(matches)} found)")
        return matches[0][1]
    vid = str(value).strip()
    for target in targets:
        try:
            rec = store.get(target, vid)
        except Exception:
            rec = None
        if rec:
            return vid
    raise ValueError(f"{label}: no {', '.join(targets)} record found with Id '{vid}'")


def validate_hierarchy_no_cycle(store, registry, obj_name: str, rid_or_none: str | None,
                                field_def: dict, parent_id: str | None):
    """Raise ValueError if setting ``parent_id`` via a self-referencing field
    would create a cycle.

    ``rid_or_none`` is the record being saved (None for new records). Only
    applies when ``field_def["reference_to"] == obj_name``. Walks up the
    parent chain with a visited set; a repeated node, reaching
    ``rid_or_none``, or a chain deeper than 50 all raise ValueError.

    NOTE (Phase 2 wiring): datamodel.py has no record-save hook — field
    validation lives in forcelet/api/_shared.py next to the
    ``validate_md_parents_exist`` calls. Wire this there for self-referencing
    Lookup/MasterDetail/PolymorphicLookup fields on create and update.
    """
    if not parent_id:
        return
    _ref = field_def.get("reference_to")
    _refs = _ref if isinstance(_ref, list) else [_ref]
    if obj_name not in _refs:
        return  # not a self-reference; nothing to check
    fname = field_def["name"]
    flabel = field_def.get("label") or fname
    if rid_or_none and str(parent_id) == str(rid_or_none):
        raise ValueError(f"{flabel} cannot reference the record itself")
    seen = set()
    cur = str(parent_id)
    depth = 0
    while cur:
        if cur in seen:
            raise ValueError(f"{flabel}: parent chain contains a cycle")
        seen.add(cur)
        if rid_or_none and cur == str(rid_or_none):
            raise ValueError(f"{flabel}: this would create a circular reference")
        if depth >= 50:
            raise ValueError(f"{flabel}: parent chain too deep (possible cycle)")
        depth += 1
        try:
            rec = store.get(obj_name, cur)
        except Exception:
            rec = None
        nxt = rec.get(fname) if rec else None
        cur = str(nxt) if nxt else None


# ------------------------------------------------------- lookup-based cascade registry
# Detail-like children held via plain Lookup (not MasterDetail). Salesforce
# semantics: deleting the parent deletes these children; they are recycled
# like MasterDetail children. MasterDetail children are discovered
# dynamically in cascade_delete; this registry covers the Lookup-based
# standard children. PriceBook is special: an entry referenced by any line
# item blocks the delete (see check_delete_blockers) instead of cascading.
CASCADE_CHILDREN = {
    "Opportunity": [
        ("OpportunityLineItem", "OpportunityId"),
        ("OpportunityContactRole", "OpportunityId"),
        ("OpportunityTeamMember", "OpportunityId"),
        ("OpportunitySplit", "OpportunityId"),
    ],
    # RevenueSchedule.OpportunityLineItemId is a required lookup: deleting an
    # Opportunity cascades its line items, so schedules must cascade too or
    # the fix would create new orphans.
    "OpportunityLineItem": [
        ("RevenueSchedule", "OpportunityLineItemId"),
    ],
    "Campaign": [
        ("CampaignMember", "CampaignId"),
        ("CampaignInfluence", "CampaignId"),
    ],
    "WorkOrder": [
        ("ServiceAppointment", "WorkOrderId"),
    ],
    "PriceBook": [
        ("PriceBookEntry", "PriceBookId"),
    ],
    "Quote": [
        ("QuoteLineItem", "QuoteId"),
    ],
    # Contract: no line-item object exists; nothing to cascade.
}

# Field names that appear in the curated CASCADE_CHILDREN registry above.
# Used by get_delete_behavior() to preserve the L6 cascade fixes. A few
# unrelated Lookup fields share these names (e.g. Quote.OpportunityId,
# QuoteSync.QuoteId); those carry an explicit `delete_behavior` in
# standard_objects.json so the name-based match never misfires.
_CURATED_CASCADE_FIELDS = {
    field_name
    for _parent, pairs in CASCADE_CHILDREN.items()
    for _child_obj, field_name in pairs
}

_DELETE_BEHAVIORS = ("clear", "block", "cascade")


def get_delete_behavior(field_def: dict) -> str:
    """Delete behavior for a relationship field: 'clear', 'block', or 'cascade'.

    Precedence:
      1. explicit `delete_behavior` on the field definition wins;
      2. MasterDetail always cascades;
      3. fields in the curated L6 CASCADE_CHILDREN registry cascade;
      4. required lookups block the delete;
      5. everything else clears the FK (Salesforce default for lookups).
    PolymorphicLookup is treated like Lookup.
    """
    explicit = field_def.get("delete_behavior")
    if isinstance(explicit, str) and explicit.strip().lower() in _DELETE_BEHAVIORS:
        return explicit.strip().lower()
    ftype = field_def.get("type")
    if ftype == "MasterDetail":
        return "cascade"
    if ftype in ("Lookup", "PolymorphicLookup") \
            and field_def.get("name") in _CURATED_CASCADE_FIELDS:
        return "cascade"
    if field_def.get("required"):
        return "block"
    return "clear"


def _price_book_entry_referenced(store, entry_id: str) -> bool:
    """True when any opportunity or quote line item references this entry."""
    for child_obj, field in (("OpportunityLineItem", "PriceBookEntryId"),
                             ("QuoteLineItem", "PriceBookEntryId")):
        try:
            rows = store.query(child_obj, limit=100000)
        except Exception:
            continue
        if any(r.get(field) == entry_id for r in rows):
            return True
    return False


def check_delete_blockers(store, registry, obj_name: str, rid: str) -> str | None:
    """Error message when deleting this record is blocked (Salesforce semantics).

    A Price Book cannot be deleted while any of its entries is referenced by
    an opportunity or quote line item; the admin must remove those references
    first. Returns None when the delete may proceed.
    """
    if obj_name == "PriceBook" and registry.get_object("PriceBookEntry"):
        for row in store.query("PriceBookEntry", limit=100000):
            if row.get("PriceBookId") == rid \
                    and _price_book_entry_referenced(store, row["id"]):
                name = row.get("Name") or row["id"]
                return (f"Cannot delete Price Book: entry '{name}' is referenced "
                        "by opportunity or quote line items")
    return None


def cascade_delete(store, registry, user: dict, obj_name: str, rid: str,
                   depth: int = 0) -> list:
    """Delete detail children of a record according to each field's delete behavior.

    Discovers every relationship field (MasterDetail, Lookup,
    PolymorphicLookup) pointing at ``obj_name`` and applies
    :func:`get_delete_behavior` per field:

    - ``cascade``: children are recycled (linked to this parent via
      ``parent_ref``), change-logged, and deleted, recursing first so
      grandchildren go before children;
    - ``clear``: the FK on each referencing child is set to NULL and the
      change is audit-logged (never applied to required fields — those
      fall through to ``block``);
    - ``block``: raises ValueError when any referencing child exists.

    "Block" is enforced for all children before any mutation happens, so a
    blocked delete never leaves half-cleared children behind. Depth-guarded.
    Raises ValueError when a Salesforce-semantics blocker (see
    check_delete_blockers) is hit.
    """
    deleted = []
    if depth >= MAX_MD_DEPTH:
        return deleted
    blocker = check_delete_blockers(store, registry, obj_name, rid)
    if blocker:
        raise ValueError(blocker)
    pairs = []
    for child_def in registry.list_objects():
        for f in relationship_fields(child_def):
            if obj_name in _reference_targets(f):
                pairs.append((child_def["name"], f))
    # Pass 1: collect referencing rows; enforce "block" before mutating.
    refs_by_pair = []
    for cobj, fdef in pairs:
        fname = fdef["name"]
        behavior = get_delete_behavior(fdef)
        if behavior == "clear" and fdef.get("required"):
            behavior = "block"  # safety net: never null a required FK
        try:
            rows = store.query(cobj, limit=100000)
        except Exception:
            continue
        refs = [r for r in rows if r.get(fname) == rid]
        if behavior == "block" and refs:
            raise ValueError(f"Cannot delete {obj_name}: {len(refs)} related "
                             f"{cobj} record(s) exist")
        if refs:
            refs_by_pair.append((cobj, fdef, behavior, refs))
    # Pass 2: apply cascade / clear.
    for cobj, fdef, behavior, refs in refs_by_pair:
        fname = fdef["name"]
        for row in refs:
            if behavior == "cascade":
                # recurse first so grandchildren go before children
                deleted.extend(cascade_delete(store, registry, user, cobj,
                                              row["id"], depth + 1))
                store.emit_change(cobj, row["id"], "delete", user,
                                  changed_fields=list(row.keys()),
                                  snapshot={k: v for k, v in row.items()})
                store.recycle_put(cobj, row, user["id"],
                                  parent_ref={"object": obj_name, "id": rid})
                store.delete(cobj, row["id"])
                deleted.append((cobj, row["id"]))
            elif behavior == "clear":
                store.update(cobj, row["id"], {fname: None})
                store.emit_change(cobj, row["id"], "update", user,
                                  changed_fields=[fname],
                                  snapshot={"cleared_field": fname,
                                            "old_value": row.get(fname),
                                            "parent_deleted": rid})
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
        if f["name"] not in tmap and not f.get("formula") and not f.get("rollup") \
                and f.get("type") != "Formula":
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
