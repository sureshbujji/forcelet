"""Schema migration runner. — Forcelet platform module.

The Store creates tables with CREATE TABLE IF NOT EXISTS, which handles fresh
databases but cannot evolve an existing one. Migrations fill that gap: each
migration is a numbered function that mutates the schema of an existing
database exactly once.

Rules:
- Never edit an applied migration; add a new one.
- Migrations must be idempotent-safe (guard with IF NOT EXISTS / PRAGMA
  checks) so a half-applied run can be retried.
- The current schema version is stored in mf_meta under "schema_version".

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

MIGRATIONS: list[tuple[int, str, object]] = []


def migration(version: int, description: str):
    """Register a schema migration."""
    def deco(fn):
        MIGRATIONS.append((version, description, fn))
        return fn
    return deco


def _table_exists(store, table: str) -> bool:
    row = store._execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    return row is not None


@migration(1, "baseline: core metadata tables (created by Store)")
def _m001_baseline(store):
    # The baseline schema is created by Store._init_meta_tables; this
    # migration only exists so the version chain has a starting point.
    assert _table_exists(store, "mf_objects")


@migration(2, "login history: per-attempt sign-in audit table")
def _m002_login_history(store):
    store._execute(
        """CREATE TABLE IF NOT EXISTS mf_login_history
           (id TEXT PRIMARY KEY, user_id TEXT, username TEXT, at TEXT,
            ip TEXT, user_agent TEXT, success INTEGER DEFAULT 0,
            failure_reason TEXT DEFAULT '')""")
    store._execute(
        "CREATE INDEX IF NOT EXISTS idx_login_history_user "
        "ON mf_login_history (user_id, at)")
    store._execute(
        "CREATE INDEX IF NOT EXISTS idx_login_history_at "
        "ON mf_login_history (at)")
    store._commit()


@migration(3, "duplicate rules: Criteria expression field")
def _m003_duplicate_rule_criteria(store):
    # Add the Criteria field definition to the DuplicateRule object and its
    # column to the data table, for databases seeded before the field existed.
    obj = store.meta_get("mf_objects", "DuplicateRule")
    if obj:
        fields = obj.get("fields", [])
        if not any(f.get("name") == "Criteria" for f in fields):
            fields.append({
                "name": "Criteria",
                "label": "Criteria (expression JSON — rule fires only when true)",
                "type": "TextArea",
                "required": False,
                "unique": False,
                "length": None,
                "picklist_values": None,
                "reference_to": None,
                "default": None,
                "formula": None,
                "rollup": None,
                "encrypted": False,
                "external_id": False,
                "reparentable": None,
                "help_text": "Optional expression JSON evaluated against the record being saved. The rule only fires when the criteria evaluates to true; blank means always.",
            })
            obj["fields"] = fields
            store.meta_put("mf_objects", "DuplicateRule", obj)
    try:
        store._execute('ALTER TABLE sobj_DuplicateRule ADD COLUMN "Criteria" TEXT')
        store._commit()
    except Exception:
        # Column already present (or table absent) — idempotent-safe.
        pass


def run_migrations(store) -> int:
    """Apply every pending migration. Returns the resulting schema version."""
    raw = store.meta_kv_get("schema_version", "0") or "0"
    try:
        current = int(raw)
    except ValueError:
        current = 0
    if current == 0 and _table_exists(store, "mf_objects"):
        # Database created before migrations existed: adopt it as v1.
        current = 1
        store.meta_kv_set("schema_version", "1")
    applied = current
    for version, _desc, fn in sorted(MIGRATIONS, key=lambda m: m[0]):
        if version > current:
            fn(store)
            store.meta_kv_set("schema_version", str(version))
            applied = version
            current = version
    return applied
