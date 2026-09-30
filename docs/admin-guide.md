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
| Master-detail / lookup relationship | Admin Setup → Data Model → Relationships (add field, type `MasterDetail`) |
| Enable Person Accounts | Admin Setup → Data Model → Person Accounts |
| Territory hierarchy / assignment rules | Admin Setup → Data Model → Territories |
| Big object / archive rule | Admin Setup → Data Model → Big Objects & Archival |
| Page layouts per profile | Admin Setup → Page Layouts |
| Custom application (tabs, layouts, profile access) | Admin Setup → App Manager, or `POST /api/admin/apps` |
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
| Change set (deploy selected metadata) | Admin Setup → Change Sets |
| Field history tracking config | Admin Setup → Monitoring → Field history tracking |
| Recycle bin (restore / purge) | 🗑 header button |

Cron expressions (e.g. `0 9 * * mon-fri`) are accepted anywhere a scheduled
job takes `interval_minutes`; the search box header toggle switches between
Keyword and Semantic (`GET /api/search/semantic`) search.

## Custom applications

Like Salesforce apps: a named bundle of navigation tabs (objects +
utilities such as Reports, Dashboards, Chatter, Forecasts). Admins build
them in **Admin Setup → App Manager** — ordered tabs, an optional tab
label, a per-tab page-layout override (use another profile's layout for
an object inside this app), and a per-tab related-list selection
(none selected = all). **Profile access** controls who sees the app and
which app is each profile's default; an app with no profile access rows
is visible to everyone. Object tabs are additionally filtered by the
user's object permissions, so a tab never leaks records the user can't
read. Users switch apps from the **▦ App Launcher** in the header; the
choice is remembered per browser. With no apps configured, the UI keeps
its legacy single tab bar. `Seed Sales & Service apps` creates two
starter apps; apps travel in metadata packages and change sets
(`"app"` component type).

## Automation building blocks

- **Code triggers** (Admin Setup → Triggers): sandboxed Python with
  `record`, `old`, `query()`, `add_error()` helpers and `before_/after_`
  insert/update/delete events.
- **Flows**: JSON action lists — `create_record`, `update_record`,
  `set_fields`, `send_email`, `http_callout`, `notify`. Screen flows add
  interactive multi-screen wizards.
- **Expressions**: `{"and": [{"==": [{"field": "Stage"}, "Won"]}]}` style
  conditions for validation rules, flow entry criteria, sharing rules.

## Platform / DevOps (Setup → DevOps)

- **Bulk API 2.0** (`/api/bulk/jobs`): CSV ingest with Salesforce-style
  job lifecycle — create (`Open`) → upload CSV (`PUT`, `text/csv`) →
  close (`PATCH {"state":"UploadComplete"}`) to process → `JobComplete`.
  Operations: `insert`, `update`, `upsert` (needs `externalIdFieldName`),
  `delete`. Rows run through validation, triggers, flows, and CDC.
  Per-row success/error files: `GET /api/bulk/jobs/<id>/successful|failed`.
- **Streaming events** (`GET /api/streaming` as SSE): change-data-capture
  on `/data/<Object>ChangeEvent` topics and platform events on
  `/event/<Name>__e` (`POST /api/streaming/events`). Replay with
  `?since=` or `Last-Event-ID`; in the browser, authenticate with
  `?access_token=`.
- **Sandboxes** (`/api/admin/sandboxes`): full SQLite copies of the org —
  `developer` (metadata only), `partial` (metadata + sample data),
  `full` (everything). Scratch orgs expire (1–30 days, default 7).
  `GET .../login` prints the command to serve a sandbox on its own port.
- **Source tracking** (`GET /api/admin/source/changes?since=<ISO>`):
  every metadata change since a timestamp, from the setup audit trail —
  the basis of a source-pull workflow.
- **Custom metadata types** (`__mdt`, `/api/admin/metadata-types`) and
  **custom settings** (`/api/admin/custom-settings`): deployable typed
  configuration. Read them from formulas/flows with
  `{"custom_metadata": {"type": "Tier__mdt", "record": "Gold",
  "field": "SlaHours"}}` and `{"custom_setting": {"name": "OrgDefaults",
  "field": "sla_hours"}}`. Types and records travel in packages.
- **Managed packages**: add `namespace` + `version` (and `managed=1`) to
  the package export; imports register in the installed-package list with
  version guards (same-version reinstall and downgrades are rejected).
- **External objects** (`__x`, `/api/admin/external-objects`): read-only
  virtual objects over an OData v4 service (direct URL or via a named
  credential); queried live at `/api/xdata/<api_name>` with `$top`,
  `$filter`, `$select`, `$orderby`, `$skip`, `$count`, `$expand`,
  `$search` passthrough.

## Data model (Setup → Data Model)

- **Relationships** (`GET /api/admin/relationships`): Lookup fields are
  optional links; `MasterDetail` fields are always required, validate that the
  parent exists, reject self-references and relationship cycles, and cascade
  delete children (to the recycle bin, with CDC events) when a parent is
  deleted. Clear `reparentable` to lock a detail to its parent; tick
  "inherits sharing" so detail visibility follows the master. Add via
  "Add field to object" → Type `MasterDetail` + target object.
- **Person Accounts** (`/api/admin/person-accounts`): one-time org enable
  adds `FirstName`, `LastName`, `PersonEmail`, `PersonPhone`,
  `IsPersonAccount` to Account and the `PersonAccount` record type; Account
  `Name` is derived from the person name.
- **Territories** (`/api/admin/territories`): hierarchy with cycle protection;
  users are assigned with roles, and membership covers descendant territories.
  Assignment rules (`/api/admin/territory-rules`) use expression criteria,
  priorities, and activation; "Run assignment now" rebuilds Account↔territory
  associations. Territory members gain visibility to Accounts/Opportunities
  in their territories.
- **Big Objects** (`__b`, `/api/admin/big-objects`): append-only stores for
  high-volume records — inserts (normal and Bulk API) allowed; updates and
  deletes return 422. Fields are added through the standard field endpoint.
- **Archival** (`/api/admin/archive-rules`): age-based rules move records
  older than N days into a mirrored `<Object>Archive__b` big object (source
  fields copied, `OriginalId` preserved), then remove them from the source.
  "Run now" reports moved count; last-run stats are shown per rule.
- **Field types**: `Geolocation` accepts a dict, a `[lat, lng]` pair, or
  `"lat;lng"` (range-validated, stored as `lat;lng`); `Address` is a compound
  street/city/state/postal/country stored as JSON; `Time` accepts `HH:MM` or
  `HH:MM:SS` and normalizes to `HH:MM:SS`.

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
