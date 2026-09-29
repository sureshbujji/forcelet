# Forcelet architecture

Built by **Suresh Itha**.

Forcelet is a metadata-driven CRM: objects, fields, profiles, roles, sharing
rules, layouts, validation, flows, and triggers are all data, interpreted at
runtime by a small Python core. There is no code generation and no per-object
boilerplate — adding an object is a JSON document.

## Runtime layout

```
forcelet/
  api/              REST API, one module per domain (auth, records, chatter, …)
  api/_shared.py    auth decorators, serialization, shared DML helpers
  store.py          SQLite persistence: record tables + mf_* metadata tables
  metadata.py       object/field registry (standard + runtime custom objects)
  security.py       profiles, permission sets, role hierarchy, sharing, FLS
  automation.py     triggers, validation rules, flows, approvals, rollups,
                    duplicate rules, SLA engine, forecasting helpers
  expressions.py    safe expression language for validation/rollup filters
  field_types.py    14 field types + validation
  assistant.py      rule-based conversational assistant over CRM data
  openapi.py        OpenAPI 3.0 spec generated from the Flask route table
  bootstrap.py      schema creation, metadata seeding, demo data
  ml.py             deterministic logistic-regression lead scoring
  crypto.py         field encryption helpers
web/index.html      single-page UI (no build step, no dependencies)
```

## Request lifecycle (record write)

1. **Auth** — `require_auth` resolves Bearer/API-key/OAuth token → user.
2. **Permission check** — profile + permission sets → object CRUD; role
   hierarchy + sharing rules → record visibility; field-level security →
   readable/editable fields.
3. **Clean + validate** — field types, required, picklists, validation rules,
   duplicate rules (409 + match list unless `?allow_duplicates=true`).
4. **Triggers** — `before_insert`/`before_update` code triggers run in a
   sandbox; `add_error()` aborts with 422 and rolls back.
5. **Write + history** — row written, field history logged, change event
   emitted (CDC), webhooks dispatched.
6. **After-effects** — `after_*` triggers, flows, rollup recomputation is lazy
   (computed on read), approval locks, SLA milestone stamping.

Reads apply the same security layers plus formula evaluation and rollup
computation on the fly, so computed fields never go stale.

## Key design decisions

- **Metadata, not migrations.** `metadata/standard_objects.json` and
  `seed_*.json` define the org; `bootstrap.py` applies them idempotently.
- **Sharing is computed, not stored.** Role hierarchy + criteria sharing rules
  are evaluated per query, so org changes take effect immediately.
- **Automation is data.** Triggers are sandboxed Python stored as metadata;
  flows are JSON action lists; both run through the same DML pipeline, so
  flow-created records fire triggers exactly like API-created ones.
- **Deterministic AI.** Lead scoring is a fixed logistic-regression model —
  no API keys, no network, reproducible scores.
- **Batteries included, zero services.** One SQLite file, one process, one
  static HTML file. Docker image available for deployment.
