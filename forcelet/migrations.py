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
