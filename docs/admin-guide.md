# Forcelet admin guide

Built by **Suresh Itha**.

## First run

```bash
pip install -r requirements.txt
python run.py                 # http://localhost:5000
```

Demo logins (password `forcelet` for all): **admin** (System Administrator),
**leo** / **maya** (Standard User), **ana** (Read Only).

## Everyday admin tasks

| Task | Where |
|---|---|
| Create a custom object / field | Admin Setup → Objects, or `POST /api/admin/objects` |
| Page layouts per profile | Admin Setup → Page Layouts |
| Validation rule | Admin Setup → Validation Rules |
| Flow (record-triggered / screen) | Flows tab |
| Approval process | Admin Setup → Approvals |
| Sharing rule | Admin Setup → Sharing Rules |
| Duplicate / matching rule | Admin Setup → Duplicate Rules |
| Scheduled job | Admin Setup → Scheduled Jobs |
| Webhook | Admin Setup → Webhooks |
| Email template / auto-response | Admin Setup → Email |
| SLA policy / escalation rule | Admin Setup → SLA |
| Named credential | Admin Setup → Named Credentials |
| API key / OAuth client | Admin Setup → API Keys / OAuth |
| Import / export CSV | Object list view → Import/Export |
| Metadata package (export org config) | Admin Setup → Packaging |
| Recycle bin (restore / purge) | 🗑 header button |

## Automation building blocks

- **Code triggers** (Admin Setup → Triggers): sandboxed Python with
  `record`, `old`, `query()`, `add_error()` helpers and `before_/after_`
  insert/update/delete events.
- **Flows**: JSON action lists — `create_record`, `update_record`,
  `set_fields`, `send_email`, `http_callout`, `notify`. Screen flows add
  interactive multi-screen wizards.
- **Expressions**: `{"and": [{"==": [{"field": "Stage"}, "Won"]}]}` style
  conditions for validation rules, flow entry criteria, sharing rules.

## API

- Full route reference: `GET /api/openapi.json` (OpenAPI 3.0, generated).
- Auth: `POST /api/login` → Bearer token; or `X-API-Key`; or OAuth2
  `POST /oauth/token`.
- Upsert: `PUT /api/sobjects/<obj>/upsert/<field>/<value>`.
- Duplicates: `GET /api/sobjects/<obj>/<id>/duplicates`,
  `POST /api/sobjects/<obj>/<id>/merge`.
- Assistant: `POST /api/assistant` `{"message": "..."}`.

## UI tour

- **⌘K / Ctrl+K** — command palette: jump to objects, records, actions.
- **✨** — Forcelet Assistant chat (rule-based): pipeline summaries, open
  cases, record lookup, task creation.
- **Dashboards** — report charts (bar/donut toggle per card).
- Record pages — tabbed: Details / Related / Activity / Chatter / Files / History.
- **🌙** — dark mode (persisted per browser).

## Security notes

- Passwords are PBKDF2-hashed; secrets (named credentials, API keys) are
  encrypted at rest and never returned by the API.
- Field-level encryption available per field; encrypted values are masked
  in history/CDC snapshots.
- This is a demo platform: run behind HTTPS with a real secret key and a
  strong admin password before any serious use.
