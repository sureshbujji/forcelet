# Changelog

## Unreleased

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
