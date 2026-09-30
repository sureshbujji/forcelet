# Changelog

## 0.7.0 — 2026-09-30

- **App data-model batch** (`forcelet/datamodel.py`, `/api/admin/*`, Setup →
  Data Model): master-detail relationships, Person Accounts, territory
  management, Big Objects + archival, and new Geolocation / Address / Time
  field types. Suite now 210 passing (15 new).
- **App UI for the data model** (`web/index.html`): record pages show a
  title header with Person Account / Big Object pills; the Related tab lists
  Lookup and Master-Detail children with type badges, "View all (N)", and a
  "+ New" child button with the parent pre-selected; Lookup/MasterDetail
  fields render as parent links; Geolocation shows a map link, Address
  formats as lines, Time as HH:MM:SS; Person Accounts get a 👤 person card;
  Account/Opportunity records show a Territories card; Big Objects browse
  read-only (insert allowed, edit/delete hidden) with "View records" /
  "View archived" shortcuts; forms gained MasterDetail (required),
  Geolocation (lat/lng), Address (compound), and Time inputs. Choosing the
  `PersonAccount` record type on create now auto-sets `IsPersonAccount` and
  derives `Name`. Also fixed `showDetail` to refresh describe + layout on
  cross-object navigation (`jumpTo`). Browser QA: all app-UI flows pass
  with zero API/console errors.
- **Master-detail relationships**: new `MasterDetail` field type (listed in
  `/api/field-types`); always required; parent-existence validation;
  self-reference and relationship cycles rejected; `reparentable` flag blocks
  parent changes when false; deleting a parent recursively cascade-deletes
  details (to the recycle bin, with CDC events); optional sharing inheritance
  from master; `GET /api/admin/relationships` lists all Lookup/MasterDetail
  relations.
- **Person Accounts**: one-time org enable
  (`POST /api/admin/person-accounts/enable`) adds `FirstName`, `LastName`,
  `PersonEmail`, `PersonPhone`, `IsPersonAccount` to Account plus the
  `PersonAccount` record type; Account `Name` derives from the person name on
  create/update.
- **Territory management** (`/api/admin/territories`, `/api/admin/territory-rules`):
  hierarchy CRUD with cycle protection and depth, user membership with roles
  (membership covers descendant territories), criteria-based assignment rules
  with priority and activation, rule execution against Accounts, account↔territory
  associations, and territory-based visibility for Accounts/Opportunities.
- **Big Objects** (`__b`, `/api/admin/big-objects`): append-only stores —
  normal and Bulk API inserts allowed; updates and deletes rejected (422);
  field/record counts in the listing; fields via the standard field endpoint.
- **Archival** (`/api/admin/archive-rules`): age-based rules move records into
  a mirrored `<Object>Archive__b` big object (source fields copied,
  `OriginalId` preserved, last-run stats tracked) and remove them from the
  source, with a "Run now" action.
- **New field types**: `Geolocation` (dict / pair / `lat;lng` input,
  range-validated, stored `lat;lng`), `Address` (compound street/city/state/
  postal/country stored as JSON), `Time` (`HH:MM`/`HH:MM:SS` → `HH:MM:SS`).
- **Setup → Data Model UI**: Relationships list, Person Accounts enable,
  Territories (tree, members, assignment rules + run), Big Objects & Archival
  (create, archive rules + run); "Add field to object" supports MasterDetail
  (target + reparentable + sharing inheritance) and the new field types.

## 0.6.0 — 2026-09-30

- **Bulk API 2.0** (`forcelet/devops.py`, `/api/bulk/jobs`): Salesforce-style
  CSV ingest — create job (`Open`), upload CSV (`PUT`, `text/csv`), close to
  process (`UploadComplete` → `InProgress` → `JobComplete`/`Failed`/`Aborted`).
  Operations: `insert`, `update`, `upsert` (external ID field), `delete`.
  Rows run through validation, triggers, flows, permissions, and CDC;
  per-row success/error CSVs downloadable. 10 MB / 50,000-row limit.
- **Streaming events**: in-process pub/sub with a 2,000-event replay buffer.
  CDC mirrors to `/data/<Object>ChangeEvent`; platform events publish to
  `/event/<Name>__e` (`POST /api/streaming/events`). `GET /api/streaming`
  serves Server-Sent Events with `?since=` / `Last-Event-ID` replay; browsers
  authenticate with `?access_token=`. Object streams enforce read permission.
- **Sandboxes & scratch orgs** (`/api/admin/sandboxes`): full SQLite org
  copies — `developer` (metadata only), `partial` (metadata + sample data),
  `full` (everything) — with create/refresh/delete and a login hint showing
  how to serve a sandbox on its own port. Scratch orgs expire in 1–30 days
  (default 7) and are pruned on listing.
- **Source tracking** (`GET /api/admin/source/changes?since=<ISO>`): every
  metadata change since a timestamp, derived from the setup audit trail —
  the basis of a source-pull workflow.
- **Custom Metadata Types** (`__mdt`, `/api/admin/metadata-types`) and
  **Custom Settings** (`/api/admin/custom-settings`): deployable typed
  configuration with typed records; readable from formulas/flows via
  `{"custom_metadata": {...}}` and `{"custom_setting": {...}}`; both travel
  in metadata packages.
- **Managed packages**: exports accept `namespace`, `version`, `managed=1`;
  imports register in the installed-package list
  (`GET /api/admin/packages/installed`) with version guards — same-version
  reinstall and downgrades rejected, upgrades allowed.
- **External Objects** (`__x`, `/api/admin/external-objects`): read-only
  virtual objects backed by OData v4 (direct URL or named credential),
  queried live at `/api/xdata/<api_name>` with full OData query-option
  passthrough (`$top`, `$filter`, `$select`, `$orderby`, `$skip`, `$count`,
  `$expand`, `$search`).
- **Setup → DevOps section**: new cards for Bulk API 2.0, Streaming events
  (live SSE listener + platform-event publisher), Sandboxes, Source tracking,
  Custom metadata types, Custom settings, and External objects; the Metadata
  package card gains namespace/version/managed export and an installed-package
  list.
- Fixes: SSE browser clients authenticate via `?access_token=` (EventSource
  can't set headers); stream UI listens to named topic events, not just
  `onmessage`.

## 0.5.0 — 2026-09-30

- **Custom applications (Salesforce-style Apps):** admins create named
  applications in Setup → App Manager (`forcelet/apps.py`, `mf_apps`
  table, `GET/POST/PUT/DELETE /api/admin/apps` plus a
  `POST /api/admin/apps/seed` for starter Sales & Service apps). Each
  app holds ordered object + utility tabs, per-tab page-layout overrides
  (honored by `GET /api/layout/<obj>?app=...` with the profile layout as
  fallback), per-tab related-list selection, and per-profile visibility
  with defaults. Users switch apps from the ▦ App Launcher in the header
  (choice persisted per browser); tabs are filtered by object
  permissions, and admins always keep an Admin Setup tab. With no apps
  configured the legacy single tab bar is unchanged. Apps are included in
  metadata packages and change sets (`"app"` component type).
- Fixes: all-false profile-access rows no longer hide an app from
  everyone (empty access = visible to all); the launcher refreshes after
  App Manager changes without re-login.

## 0.4.0 — 2026-09-30

- **Field History Tracking:** per-object configuration — enable/disable
  tracking, choose which fields are tracked (empty = all), and set a
  per-object retention window in days (`mf_history_tracking_config`,
  `GET/PUT /api/admin/history-tracking`, `POST /api/admin/history-tracking/
  purge`). The record-history writer honors the config (disabled objects log
  nothing; field lists gate which changes are recorded). Setup → Monitoring
  gets a Field history tracking card with an editable per-object table and a
  purge action.
- **Cron scheduling:** scheduled jobs accept a five-field cron expression
  (`minute hour day-of-month month day-of-week`, with steps, lists, ranges,
  and month/weekday names) in addition to `interval_minutes`
  (`forcelet/cron.py`; `POST /api/admin/scheduled-jobs/validate-cron` for
  live syntax checking, `GET /api/admin/scheduled-jobs/<id>/runs` for per-job
  run history). The Setup UI shows a cron input with live validation, a
  human-readable schedule column, and a Runs action per job.
- **Change sets:** bundle selected metadata into a named change set
  (custom objects, fields, validation rules, flows, triggers, approval
  processes, assignment rules, layouts, email/list-view templates, record
  types, scheduled jobs). Draft → Outbound → download as JSON → upload into
  another org as Inbound → validate without applying → deploy, with every
  validation and deployment recorded in the deployment history
  (`forcelet/changesets.py`, `/api/admin/change-sets`, Setup → Integrations
  card with a full management UI).
- **Semantic search:** dependency-free TF-IDF vector search over readable
  records (`forcelet/semantic.py`, `GET /api/search/semantic?q=&limit=`)
  with cosine-similarity ranking, relevance scores, result snippets, a
  pluggable provider registry, and a two-minute result cache. The header
  search box gains a Keyword/Semantic mode toggle; semantic results render
  with relevance bars.
- **Tests:** 20 new tests in `tests/test_platform.py` covering cron parsing/
  due logic, history gating and purge, change-set validate/deploy round
  trips, and semantic ranking (suite now 166 passing).

## 0.3.0

- **Functionality batch:** eight platform features, all with UI.
  Calendar view (month/agenda) for Event, Task, and ServiceAppointment with
  drag-to-reschedule; Quote PDF generation (`GET /api/quotes/<id>/pdf`,
  pure-stdlib PDF 1.4); case queues with filter pills on the Case list and a
  Queues & Macros admin page; agent macros (`set_fields`/`add_comment`/
  `reassign`) runnable from the Case record page; async bulk insert/update/
  upsert jobs with a monitor page (`/api/bulk-jobs`); scheduled report and
  dashboard subscriptions with emailed digests (`/api/report-subscriptions`,
  Subscribe buttons on Reports/Dashboards); flow version history with
  one-click rollback in the visual flow builder (`/api/admin/flows/<id>/
  versions`); TOTP two-factor auth (setup/enable/disable, challenge on
  password login, 🔐 2FA in the header); live knowledge suggestions on the
  public Web-to-Case form (`/api/public/knowledge-suggest`).
- **UI batch:** visual flow builder (drag-to-arrange node canvas with
  click-to-edit side panel, guided condition builder, canvas positions saved
  per flow); dispatch console (ServiceAppointment timeline with technician
  lanes, now-marker, date picker); inline list editing (text/number/
  picklist/checkbox/date); dashboard builder (admin Customize mode,
  add/remove/drag-reorder report widgets, live preview); file preview
  lightbox (image/PDF/text); split view (list + record preview). New
  `mf_dashboards` config table and `GET/POST/PUT/DELETE /api/dashboards`.
- **Admin Setup overhaul:** Salesforce-style Setup Home with Quick Find search
  and 28 tiles grouped into Data Model, Automation, Security & Access,
  Integrations, and Monitoring, each tile showing a live configured count;
  section views with breadcrumbs, sticky section nav, per-section filter, and
  collapsible cards. Every config kind now auto-lists its records with count
  badges, Active/Inactive pills, and Edit / Activate / Deactivate / Delete
  actions (new `PATCH /api/admin/<kind>/<rid>` endpoint). Guided condition
  builder (field/operator/value, match-all/any) replaces hand-written JSON for
  validation rules, flows, assignment, auto-response, sharing, and escalation
  criteria.
- **Service objects:** `WorkOrder` (auto-numbered `W-000001…`, lifecycle path)
  and `ServiceAppointment` with a flow that completes the work order when its
  appointment is completed; `Event` with start/end validation.
- **Campaigns & contracts:** `Campaign` + `CampaignMember` with response
  roll-ups; `Contract` with end-date validation and `C-000001…` numbering.
- **Knowledge base:** `KnowledgeArticle` (`KA-000001…`) attached to cases.
- **Recycle bin:** soft deletes are archived, restorable, and purgeable
  (`GET /api/recycle-bin`, restore/permanent-delete endpoints, admin empty-bin).
- **Duplicate merge:** find duplicates per record and merge with winner
  selection, field picking, and child re-parenting.
- **OpenAPI 3.0:** generated spec at `/api/openapi.json` (107 routes).
- **Forcelet Assistant:** rule-based chat answering pipeline, cases, orders,
  and inventory questions; creates tasks on request.
- **UI refresh:** design tokens + dark mode, ⌘K/Ctrl+K command palette,
  tabbed record pages, dashboard charts (bar/donut), toasts, skeleton
  loaders, mobile-responsive layout.
- **API split:** `forcelet/api.py` (1,800 lines) → `forcelet/api/` package,
  one module per domain.
- **Docs:** `docs/architecture.md`, `docs/data-model.md`,
  `docs/admin-guide.md`; README "What's new in 0.2.0" section.
- **Repo hygiene:** GitHub Actions CI, `pyproject.toml`, CONTRIBUTING,
  issue templates.

## 0.2.0

- **Automotive pack (Auto Cloud-style):** new standard objects
  `VehicleDefinition`, `Vehicle`, `Asset`, `Order`, `OrderItem`, `Delivery`
  with auto-numbering triggers (`ORD-`/`DLV-`), line-total trigger,
  `Order.TotalAmount` roll-up, VIN (17-char) and delivery-date validation
  rules, duplicate-VIN detection, Vehicle lifecycle and Order fulfillment
  paths, flows for auto-creating deliveries on order activation and marking
  vehicles delivered, plus Lucid Air/Gravity demo data.
- **Forecasting:** monthly quotas, weighted pipeline rollups, quota
  attainment by role subtree.
- **Screen flows:** multi-screen wizards with validation and finish actions.
- **Case SLA milestones and escalation:** policies by priority, breach
  monitoring, escalation rules.
- **HTTP callouts and named credentials:** no-auth/basic/bearer/API-key
  auth with encrypted secrets.
- **Web-to-Case and Email-to-Case:** public form, inbound webhook,
  auto-response rules.
- Project renamed from Miniforce to **Forcelet**.
