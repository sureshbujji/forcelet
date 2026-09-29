# ⚡ Forcelet

[![CI](https://github.com/sureshbujji/forcelet/actions/workflows/ci.yml/badge.svg)](https://github.com/sureshbujji/forcelet/actions)

Built by **Suresh Itha**.

A **metadata-driven, Salesforce-style CRM platform** in Python. Everything is
configurable at runtime — no code changes needed:

| Salesforce concept | Forcelet equivalent |
|---|---|
| Standard objects (Account, Contact, Lead, Opportunity, Case, Task) | Seeded from `metadata/standard_objects.json` |
| Custom objects | `POST /api/admin/objects` (or the Admin Setup UI) |
| Custom fields (14 types) | `POST /api/admin/objects/:obj/fields` |
| Profiles & permission sets | Object CRUD + field-level read/edit per profile; additive permission sets |
| Roles & sharing | Role hierarchy + criteria-based sharing rules |
| Page layouts | Per-object, per-profile, per-record-type sections; `GET /api/layout/:obj` |
| Validation rules | Expression-JSON rules evaluated on every save |
| Formula fields | Computed on read, never stored (e.g. `DaysOpen`) |
| Flows | Trigger-based automation (create/update/set-field/log actions, `{{Trigger.*}}` templates) |
| Approval processes | Entry conditions, manager/role/user approvers, record locking, inbox UI |
| Record types | Per-type picklist values + layout overrides |
| Reports & dashboards | Filter/group/aggregate engine with bar charts |
| Field history | Every change logged with who/when |
| Duplicate management | Matching rules block (or warn on) duplicates |
| Data import/export | CSV download + bulk CSV import |
| Webhooks | Signed HTTP callbacks on create/update/delete, with delivery log |

Plus a REST API, a single-page web UI, Docker packaging, and a pytest suite.

## Quickstart

```bash
pip install -r requirements.txt
python run.py
# open http://localhost:5000
```

Or with Docker:

```bash
docker compose up --build
# open http://localhost:5000  (data persists in the forcelet-data volume)
```

Demo logins (pick one on the sign-in screen — password for all is `forcelet`,
change it via the API or after signing in):

| User | Profile | Role |
|---|---|---|
| `admin` | System Administrator | CEO — sees everything |
| `maya` | Standard User | Sales Manager — sees her team's records |
| `leo` | Standard User | Sales Rep — owns the seed records |
| `ana` | Read Only | Support Agent — different branch, sees little |

Try this: sign in as `leo`, create an Account. Switch to `maya` — she sees it
(role hierarchy). Switch to `ana` — she doesn't (separate branch). Open an
Opportunity as `ana` — the **Amount** field is hidden by field-level security.

More things to try:

- **Validation rule**: create an Opportunity with a past Close Date while it's
  still open — the save is rejected.
- **Flow**: move an Opportunity to *Negotiation* — a follow-up Task appears.
- **Approvals**: set Discount % above 20 on an Opportunity — it's auto-submitted,
  locked for edits, and lands in `maya`'s approval inbox.
- **Record types**: describe Opportunity with `?record_type=Enterprise` to see
  the extra *Security Review* stage.
- **Sharing rule**: create a *Critical* Case as `leo` — `ana` (Support branch)
  can see it even though it's outside her hierarchy.
- **Duplicates**: create two Leads with the same email — the second is blocked
  with a 409 (or pass `?allow_duplicates=true`).
- **Reports**: open the Reports tab and run *Pipeline by Stage*.

## API tour

```bash
TOKEN=$(curl -s -X POST localhost:5000/api/login \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"forcelet"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
H="Authorization: Bearer $TOKEN"

# create a custom object + fields (like Setup → Object Manager)
curl -X POST localhost:5000/api/admin/objects -H "$H" -H 'Content-Type: application/json' \
  -d '{"name":"Invoice","label":"Invoice","plural":"Invoices"}'
curl -X POST localhost:5000/api/admin/objects/Invoice/fields -H "$H" -H 'Content-Type: application/json' \
  -d '{"name":"Total","label":"Total","type":"Currency","required":true}'

# use it like any standard object
curl -X POST localhost:5000/api/sobjects/Invoice -H "$H" -H 'Content-Type: application/json' \
  -d '{"Total": 4999.50}'

# a validation rule: Total must be positive
curl -X POST localhost:5000/api/admin/validation-rules -H "$H" -H 'Content-Type: application/json' \
  -d '{"name":"Total positive","object":"Invoice","message":"Total must be positive","active":true,
       "condition":{"<":[{"field":"Total"},0]}}'

# a formula field: double of Total, computed on read
curl -X POST localhost:5000/api/admin/objects/Invoice/fields -H "$H" -H 'Content-Type: application/json' \
  -d '{"name":"DoubleTotal","label":"Double Total","type":"Currency",
       "formula":{"*":[{"field":"Total"},2]}}'

# a code trigger: set Probability from Stage before save
curl -X POST localhost:5000/api/admin/triggers -H "$H" -H 'Content-Type: application/json' \
  -d '{"name":"Stage → Probability","object":"Opportunity","active":true,
       "events":["before_insert","before_update"],"order":10,
       "code":"PROB={\"Prospecting\":10,\"Closed Won\":100}\nif record.get(\"Stage\") in PROB:\n    record[\"Probability\"]=PROB[record[\"Stage\"]]"}'

# a roll-up summary: total pipeline on the Account, computed on read
curl -X POST localhost:5000/api/admin/objects/Account/fields -H "$H" -H 'Content-Type: application/json' \
  -d '{"name":"BiggestDeal","label":"Biggest Deal","type":"Currency",
       "rollup":{"object":"Opportunity","via":"AccountId","func":"max","field":"Amount"}}'

# a list view + filtered, sorted, searchable list
curl -X POST localhost:5000/api/list-views -H "$H" -H 'Content-Type: application/json' \
  -d '{"object":"Opportunity","name":"My pipeline","shared":true,
       "columns":["Name","Amount","Stage"],
       "filters":{"==":[{"field":"OwnerId"},{"user_id":true}]},
       "sort_by":"Amount","sort_dir":"desc"}'
curl "localhost:5000/api/sobjects/Opportunity?view=<id>&search=acme&sort=Name&dir=asc" -H "$H"

# export everything as CSV
curl localhost:5000/api/sobjects/Invoice/export -H "$H" -o invoices.csv
```

Full endpoint list: `/api/objects`, `/api/describe/:obj`, `/api/layout/:obj`,
`/api/sobjects/:obj` (GET/POST; `?view=`, `?search=`, `?sort=`, `?dir=`), `/api/sobjects/:obj/:id` (GET/PATCH/DELETE),
`/api/sobjects/:obj/:id/history`, `/api/sobjects/:obj/export`,
`/api/sobjects/:obj/:id/submit-approval`, `/api/approvals`,
`/api/approvals/:id/{approve,reject}`, `/api/reports`, `/api/reports/:id/run`,
`/api/list-views/:obj` (GET), `/api/list-views` (POST), `/api/list-views/:id` (DELETE),
`/api/admin/{objects,users,roles,profiles,layouts,import/:obj,webhook-deliveries}`,
`/api/admin/{validation-rules,flows,approval-processes,sharing-rules,matching-rules,record-types,permission-sets,webhooks,reports,triggers}`,
`/api/admin/users/:id/permission-sets`, `/api/change-password`, `/api/field-types`.

## Expression language

Validation rules, flow conditions, sharing criteria, approval entry conditions,
and report filters all use the same JSON expression language:

```json
{"and": [
  {"==": [{"field": "Stage"}, "Negotiation"]},
  {"!=": [{"field_old": "Stage"}, "Negotiation"]},
  {"<": [{"field": "CloseDate"}, {"today": true}]}
]}
```

Supported: `==` `!=` `<` `<=` `>` `>=` `in`, `and` `or` `not`, `contains`,
`isblank`, `len`, arithmetic `+ - * /`, `days_between`, `{"field": …}`,
`{"field_old": …}`, `{"today": true}`, `{"now": true}`, `{"user_id": true}`
(resolves to the current user's id — used by the seeded "My Open Pipeline" list view). Field references
understand Salesforce casing (`CreatedDate`, `Id`) as well as stored names.

## Code triggers

Admins can attach Python triggers to any object at six events:
`before_insert`, `after_insert`, `before_update`, `after_update`,
`before_delete`, `after_delete` (`/api/admin/triggers`).

Trigger code runs in a sandbox — no imports, files, or network — with this context:

- `record` — the record; mutable in `before_*` events
- `old` — previous values (`None` on insert)
- `user` — the user performing the save
- `errors` — append a message to block the save (like `addError`); after-event
  errors roll the operation back (insert/update), `after_delete` errors surface as warnings
- `query(obj, **filters)`, `create(obj, fields)`, `update(obj, id, fields)` —
  sharing-aware DML that runs nested triggers/flows (max depth 3)

Seeded examples: Stage → Probability mapping, blocking Account deletion while
open Opportunities exist, and a welcome Task for new Contacts.

## Roll-up summary fields

Any field can carry a `rollup` spec — `{"object","via","field","func","filter?"}` —
and is then computed on read (sum/avg/min/max/count over child records linked by
the `via` lookup), never stored, and not writable. Seeded on Account:
`TotalPipeline` (sum of Opportunity Amount) and `OpenOpportunityCount`.

## List views

`GET /api/list-views/:obj` lists visible views (personal + shared); anyone can
`POST /api/list-views` to save one (admin-only for `shared: true`), and
`DELETE /api/list-views/:id` removes it. Lists accept `?view=<id>` to apply a
view's filters/sort server-side, plus ad-hoc `?search=` (text match across text
fields) and `?sort=`/`?dir=` params. Column headers in the UI sort on click;
related lists (child records pointing at the current one) render on detail pages.

## Scheduled jobs

`mf_scheduled_jobs` hold Python code that runs on an `interval_minutes` cadence.
Context: `query/create/update` (sharing-aware DML), `today()`, `timedelta`,
`user` (the `run_as` user), `errors`. `POST /api/admin/scheduled-jobs/:id/run`
executes immediately; `run_due_scheduled_jobs()` runs everything overdue
(`run.py` starts a background thread that calls it every 60s), and
`GET /api/admin/scheduled-runs` shows the run log. Seeded: **Close-date
reminders** creates a Task for the owner of every open Opportunity closing
within 7 days.

## Assignment rules

Ordered, criteria-based owner assignment on create — evaluated before triggers.
`assignee` is `{"type":"user","username":"leo"}` or
`{"type":"round_robin","usernames":["leo","maya"]}` (counter persisted on the
rule). Seeded: **Round-robin web leads** alternates Web-sourced Leads between
leo and maya.

## Global search, activities & email

`GET /api/search?q=` searches text fields across every readable object (top 5
per object), with a search box in the header. Detail pages carry an **activity
timeline** (`GET/POST /api/sobjects/:obj/:id/activities`, types note/call/task/
email). `mf_email_templates` support `{{Record.Field}}` / `{{User.Name}}` merge;
`POST /api/sobjects/:obj/:id/send-email` merges, logs to `mf_email_log`, and
adds the mail to the timeline (set `FORCELET_SMTP` to actually deliver).

## Metadata packaging

`GET /api/admin/packages/export` downloads a versioned package (custom objects,
extra standard-object fields, layouts, and all automation config);
`POST /api/admin/packages/import` installs one into another org, upserting by
natural key (list views are re-owned by the importer).

## Audit trail & change data capture

Every setup change (objects, fields, rules, users, roles, profiles, layouts,
permission sets, packages...) is written to `mf_audit_trail` with who/when
(`GET /api/admin/audit-trail`). Every record create/update/delete emits an
event with a monotonic `seq` (`GET /api/change-events?since=&object=
&record_id=`) for replay-style subscribers; encrypted values are omitted from
snapshots.

## OAuth2 tokens, API keys & field encryption

`POST /api/oauth/token` implements the OAuth2 `password` and `refresh_token`
grants (refresh tokens are single-use rotated, SHA-256 hashed at rest).
`GET/POST/DELETE /api/api-keys` mints long-lived `mf_live_...` keys (hashed at
rest, usable as `Bearer` tokens, revocable) for integrations. Fields marked
`encrypted: true` (text-like types) are encrypted at rest with Fernet
(`FORCELET_ENC_KEY` env or a generated `.forcelet.key`, gitignored);
they decrypt transparently for authorized readers, work in expressions and
search, but can't be unique or used in duplicate rules.

### Collaboration, conversion, and lead capture

**Chatter.** `GET/POST /api/feed` (home feed: posts on records you follow, your
own posts, and posts mentioning you), `POST /api/feed/<id>/comments`,
`POST/DELETE /api/feed/<id>/like`, `POST/DELETE /api/feed/follow`, and
`GET /api/feed/following`. Posts can attach to any record; `@username`
mentions are tracked and surfaced. The UI has a Chatter home tab plus a
per-record feed card with post/comment/like/follow controls.

**Kanban + Path.** `GET /api/kanban/<object>` groups records by any readable
picklist (`?group_by=`), with a UI Kanban toggle and one-click stage moves.
`GET /api/paths/<object>` plus `POST/DELETE /api/admin/paths` configure a
Salesforce-style Path: a stage tracker with per-stage guidance on record
detail pages. An Opportunity path over `Stage` is seeded.

**Lead conversion.** `POST /api/sobjects/Lead/<id>/convert` converts a Lead
into an Account (company name), a Contact (person, linked), and optionally an
Opportunity (linked, default `Prospecting` stage, close date +30d), marks the
Lead `Converted`, and records the mapping (retrievable at
`GET /api/sobjects/Lead/<id>/conversion`). Re-conversion is rejected with 422.

**External IDs + upsert.** Fields can be marked `external_id: true`
(Text/Email/Phone/URL/Number only — implicitly unique, not encryptable).
`PUT /api/sobjects/<object>/upsert/<field>/<value>` creates or updates by the
external ID (`{"created": true/false}`). CSV import gains
`?mode=upsert&external_id_field=<field>` for bulk upsert.

**Web-to-Lead + auto-response rules.** A public, unauthenticated
`GET /api/public/web-to-lead` renders an HTML capture form and
`POST /api/public/web-to-lead` (JSON or form data) creates a Lead with
`LeadSource = Web` through the normal create pipeline (validation, triggers,
assignment rules, duplicates allowed). `POST/GET/DELETE
/api/admin/auto-response-rules` manage rules that fire on record creation
(first match by `order`, JSON criteria on the new record) and send a merge
email template; a "Web lead auto-response" rule using the seeded
"New lead welcome" template is included. Fires are logged in the email log
and appended to the record timeline as `Auto-response: …` entries.

### Files, quotes, notifications, and AI scoring
**Files.** `POST /api/sobjects/<object>/<id>/files` (multipart upload, 10 MB
max), `GET /api/sobjects/<object>/<id>/files`, `GET /api/files/<id>`
(download), `DELETE /api/files/<id>` (uploader or admin). Bytes live on disk
next to the DB; visibility follows the parent record. Every record detail page
gets a Files card with upload/download/delete.

**Products, Price Books, Quotes.** New standard objects: `Product`,
`PriceBook`, `PriceBookEntry`, `Quote`, `QuoteLineItem`. A seeded
"Standard Price Book" ships with three products and entries. A seeded trigger
computes `QuoteLineItem.TotalPrice = Quantity × UnitPrice × (1 − Discount%)`
on insert/update, and `Quote.GrandTotal` is a roll-up sum over line items —
so quotes, line items, and totals work through the ordinary CRUD API with
automatic related lists. Price book data is org-wide visible
(`org_wide_visible` object flag); reps can create quotes and line items.

**Automotive (Auto Cloud-style).** New standard objects: `VehicleDefinition`
(model catalog: make, model year, trim, body style, MSRP), `Vehicle` (VIN,
model lookup, account, status lifecycle, odometer), `Asset` (purchased
product/vehicle instance with warranty dates), `Order` (auto-numbered
`ORD-000001…` via trigger, status lifecycle, `TotalAmount` roll-up),
`OrderItem` (order/product/vehicle lookups; trigger computes
`LineTotal = Quantity × UnitPrice`), and `Delivery` (auto-numbered
`DLV-000001…`, order/vehicle/account lookups, scheduled/delivered dates,
driver). Seeded automation: VIN must be 17 characters, delivered date can't
precede the scheduled date, duplicate-VIN detection, a Vehicle lifecycle Path
and an Order fulfillment Path, a flow that auto-creates a `Scheduled`
delivery when an order is Activated, and a flow that marks the vehicle
`Delivered` when its delivery completes. Demo data ships two models, two
vehicles, a fleet order, a delivery, and an asset. Records created by flows
now run `before_insert`/`after_insert` triggers, just like API-created
records.

**Notifications.** In-app bell with an unread badge:
`GET /api/notifications`, `GET /api/notifications/unread-count`,
`POST /api/notifications/read` (`{"all": true}` or `{"ids": [...]}`).
Fired on Chatter `@mentions`, approval requests (approvers of the current
step, via auto- or manual submit), and assignment-rule owner changes.

**AI lead scoring.** `POST /api/admin/ml/train-lead-scoring` trains a
deterministic pure-Python logistic regression on historical Leads
(`Converted` vs `Unqualified`; needs ≥ 10 labeled leads) over email/phone
presence, rating, and lead source, and stores the model (included in metadata
packages). `GET /api/sobjects/Lead/<id>/score` returns a 0–100 score, a
Hot/Warm/Cold grade, and the top contributing factors. The UI shows a score
card on Lead detail pages and a training control in Admin Setup.

## Field types

Text, TextArea, Number, Currency, Percent, Date, DateTime, Checkbox,
Picklist, MultiPicklist, Email, Phone, URL, Lookup (relationship).
Any text-like field can be marked encrypted at rest.

### Case intake, SLA, forecasting, screen flows, and callouts

**Web-to-Case + Email-to-Case.** A public `GET /api/public/web-to-case`
renders an HTML support form and `POST /api/public/web-to-case` (JSON or form
data) creates a Case with `Origin = Web` through the normal create pipeline
(validation, triggers, assignment rules, auto-response rules, escalation
rules). `POST /api/public/email-to-case` (JSON `from`/`subject`/`body`)
creates a Case with `Origin = Email`. `Case.Origin` is a standard picklist
(Web/Email/Phone/Other), and a seeded Web/Email auto-response rule sends the
seeded "Case received" template.

**Case SLA milestones + escalation.** `POST/GET/DELETE
/api/admin/sla-policies` define policies (object + priority → milestone list
with `target_minutes`). On Case create, milestones are stamped with due times
(`GET /api/sobjects/Case/<id>/milestones`; shown as a card on Case detail
pages); closing the Case completes its open milestones. The seeded hourly
"Case SLA monitor" scheduled job marks overdue milestones `breached` and
notifies the case owner and their manager. `POST/GET/DELETE
/api/admin/escalation-rules` define rules that fire on record save or on SLA
breach (set_fields, reassign, notify owner/manager). Seeded: subjects
containing "urgent" escalate to Critical; breached cases are set to Escalated.

**Forecasting.** `POST/GET/DELETE /api/admin/forecast-quotas` manage monthly
quotas per user. `GET /api/forecasts?period=YYYY-MM` rolls up each visible
salesperson: quota, closed-won total, weighted pipeline
(Amount × Probability), forecast, and quota attainment. Closed Lost is
excluded; admins see everyone, others see their role subtree. The UI has a
Forecasts tab with period picker, attainment bars, and an admin quota editor.

**Screen flows.** Interactive multi-screen wizards: `GET /api/screen-flows`
lists active flows, `POST /api/screen-flows/<id>/start` opens a run,
`GET /api/screen-flows/runs/<id>` fetches the current screen, and
`POST /api/screen-flows/runs/<id>/next` submits answers with required-field
and type validation (text, textarea, number, email, phone, date, picklist,
checkbox). Finish actions (e.g. `create_record`) support `{{Trigger.Field}}`
merge fields. Admins create them with `POST /api/admin/flows` and
`flow_type: "screen"`. The UI has a Flows tab that renders the wizard inline;
a seeded "Quick contact create" flow collects details and creates a Contact.

**HTTP callouts + named credentials.** `POST/GET/DELETE
/api/admin/named-credentials` store base URLs with no-auth, basic, bearer
token, or API-key-header auth; secrets are encrypted at rest and never
returned by the API. `POST /api/admin/callouts/invoke` (admin-only) sends a
test callout, and the record-flow `http_callout` action sends templated
callouts from automation, logging results to field history. Secrets are also
exportable/importable via metadata packages (values stay encrypted).

## Tests

```bash
pytest -q
```

132 tests covering metadata CRUD, validation rules, formula fields, flows,
approvals + record locking, record types, reports, field history,
criteria-based sharing, duplicate rules, permission sets, webhooks,
CSV import/export, triggers, roll-ups, list views, scheduled jobs, assignment
rules, global search, activities, email templates, packaging, audit trail,
change data capture, OAuth2 tokens, API keys, field encryption, chatter,
kanban + paths, lead conversion, external IDs + upsert, web-to-lead +
auto-response rules, files, quote-to-cash totals, notifications, AI lead
scoring, web-to-case + email-to-case, case SLA milestones + escalation rules,
forecasting, screen flows, HTTP callouts + named credentials, vehicle
definitions / vehicles / assets / orders / order items / deliveries,
work orders, service appointments, events, campaigns + members, contracts,
knowledge articles, the recycle bin, duplicate find + merge, the assistant,
and API round-trips.

## What's new in 0.2.0

**Seven new standard objects** — Work Orders (+ Service Appointments with a
completion flow), Events, Campaigns (+ Members with response roll-ups),
Contracts (date validation, auto-numbered `C-000001…`), and a Knowledge Base
(auto-numbered `KA-000001…`).

**New platform features** — a Recycle Bin (soft delete → restore → purge),
duplicate find + **merge** (winner selection, child re-parenting), a generated
**OpenAPI 3.0 spec** at `/api/openapi.json`, and a rule-based **Forcelet
Assistant** (pipeline summaries, open cases, record lookup, task creation)
floating in the UI.

**UX refresh** — design tokens with light/dark mode, ⌘K/Ctrl+K command
palette, tabbed record pages (Details / Related / Activity / Chatter / Files /
History), dashboard charts with bar/donut toggle, toast notifications,
skeleton loaders, and a mobile-responsive layout.

**Repo & docs** — the 1,800-line `api.py` is now a `forcelet/api/` package
(one module per domain), GitHub Actions CI, `pyproject.toml` packaging,
CONTRIBUTING, changelog, issue templates, and a `docs/` folder (architecture,
data model + ERDs, admin guide). See `docs/` for details.

## Notes

- Auth is demo-grade: PBKDF2 password hashes + bearer tokens, but the Flask dev
  server has no rate limiting or HTTPS. For anything real, run behind TLS with a
  proper WSGI server and change the default `forcelet` password. The public
  Web-to-Lead endpoint has no CAPTCHA or rate limiting — fine for demo, not for
  the open internet.
- Storage is SQLite (`forcelet.db`, created on first run); object tables are
  created/altered automatically as metadata changes.
- Seed data lives in `metadata/` as JSON so the whole org definition is
  version-controllable.
