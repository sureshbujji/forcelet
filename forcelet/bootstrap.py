"""First-run seeding: standard objects, roles, profiles, users, layouts, demo data,
and all automation metadata (validation rules, flows, approvals, record types,
sharing/matching rules, permission sets, reports)."""
from __future__ import annotations

import json
import os

from .metadata import MetadataRegistry
from .security import Security, hash_password
from .store import Store

METADATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "metadata")


def bootstrap(db_path: str):
    store = Store(db_path)
    registry = MetadataRegistry(store)
    security = Security(store)

    registry.seed_if_empty(METADATA_DIR)

    # migrate: standard objects introduced after this DB was created, and
    # refresh standard definitions (new flags/fields), preserving any
    # admin-added custom fields on standard objects
    with open(os.path.join(METADATA_DIR, "standard_objects.json")) as f:
        for obj in json.load(f):
            cur = registry.get_object(obj["name"])
            if not cur:
                store.meta_put("mf_objects", obj["name"], obj)
                store.ensure_object_table(obj)
                continue
            std_field_names = {f["name"] for f in obj.get("fields", [])}
            merged_fields = list(obj.get("fields", []))
            merged_fields += [f for f in cur.get("fields", [])
                              if f["name"] not in std_field_names]
            new_def = {**cur, **obj, "fields": merged_fields}
            if new_def != cur:
                store.meta_put("mf_objects", obj["name"], new_def)
            have_cols = set(store.existing_columns(obj["name"]))
            for fld in merged_fields:
                if (not fld.get("formula") and not fld.get("rollup")
                        and fld["name"] not in have_cols):
                    store.add_column(obj["name"], fld)

    # migrate: profile object permissions for objects added later
    with open(os.path.join(METADATA_DIR, "seed_security.json")) as f:
        seed_profiles = {p["name"]: p for p in json.load(f)["profiles"]}
    for name, seed_p in seed_profiles.items():
        cur = store.meta_get("mf_profiles", name)
        if not cur:
            continue
        perms = cur.setdefault("object_permissions", {})
        changed = False
        for obj, grant in (seed_p.get("object_permissions") or {}).items():
            if obj not in perms:
                perms[obj] = grant
                changed = True
        if changed:
            store.meta_put("mf_profiles", name, cur)

    # migrate: named automation seeds added after this DB was created
    with open(os.path.join(METADATA_DIR, "seed_automation.json")) as f:
        auto_seed = json.load(f)
    from .automation import PACKAGE_TABLES
    for kind, (table, keys) in PACKAGE_TABLES.items():
        existing = [{k: it.get(k) for k in keys}
                    for it in store.config_all(table)]
        for item in auto_seed.get(kind, []):
            nk = {k: item.get(k) for k in keys}
            if nk not in existing:
                store.config_put(table, item)
                existing.append(nk)

    with open(os.path.join(METADATA_DIR, "seed_security.json")) as f:
        seed = json.load(f)
    if store.meta_count("mf_roles") == 0:
        for r in seed["roles"]:
            store.meta_put("mf_roles", r["name"], r)
    if store.meta_count("mf_profiles") == 0:
        for p in seed["profiles"]:
            store.meta_put("mf_profiles", p["name"], p)
    if store.meta_count("mf_users") == 0:
        for u in seed["users"]:
            u = {k: v for k, v in u.items() if k != "password_hint"}
            u.setdefault("permission_sets", [])
            u["password_hash"] = hash_password("forcelet")  # demo default; change it
            store.meta_put("mf_users", u["id"], u)

    if store._execute("SELECT COUNT(*) AS n FROM mf_layouts").fetchone()["n"] == 0:
        with open(os.path.join(METADATA_DIR, "seed_layouts.json")) as f:
            for lay in json.load(f)["layouts"]:
                store.layout_put(lay["object"], lay["profile"], lay)

    # ---- automation metadata (seed once) ----
    with open(os.path.join(METADATA_DIR, "seed_automation.json")) as f:
        auto = json.load(f)
    table_for = {
        "validation_rules": "mf_validation_rules",
        "flows": "mf_flows",
        "approval_processes": "mf_approval_processes",
        "record_types": "mf_record_types",
        "reports": "mf_reports",
        "sharing_rules": "mf_sharing_rules",
        "matching_rules": "mf_matching_rules",
        "permission_sets": "mf_permission_sets",
        "triggers": "mf_triggers",
        "list_views": "mf_list_views",
        "scheduled_jobs": "mf_scheduled_jobs",
        "assignment_rules": "mf_assignment_rules",
        "email_templates": "mf_email_templates",
        "auto_responses": "mf_auto_responses",
        "paths": "mf_paths",
        "sla_policies": "mf_sla_policies",
        "escalation_rules": "mf_escalation_rules",
    }
    for key, table in table_for.items():
        if store._execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0:
            for item in auto.get(key, []):
                store.config_put(table, item)
    for username, ps_names in (auto.get("permission_set_assignments") or {}).items():
        user = security.get_user_by_username(username)
        if user:
            for ps in ps_names:
                for cand in store.config_all("mf_permission_sets"):
                    if cand.get("name") == ps:
                        security.assign_permission_set(user["id"], cand["id"])
                        break

    # migrate: Lead.Status gains the 'Converted' picklist value (lead conversion)
    lead = registry.get_object("Lead")
    if lead:
        changed = False
        for f in lead["fields"]:
            if f["name"] == "Status" and "Converted" not in (f.get("picklist_values") or []):
                f["picklist_values"] = (f.get("picklist_values") or []) + ["Converted"]
                changed = True
        if changed:
            store.meta_put("mf_objects", "Lead", lead)

    # default web-to-lead auto-response: links the seeded welcome template
    tpl = next((t for t in store.config_all("mf_email_templates")
                if t.get("name") == "New lead welcome"), None)
    if tpl and not any(r.get("name") == "Web lead auto-response"
                       for r in store.config_all("mf_auto_responses")):
        store.config_put("mf_auto_responses", {
            "name": "Web lead auto-response",
            "object": "Lead",
            "active": True,
            "order": 10,
            "criteria": {"==": [{"field": "LeadSource"}, "Web"]},
            "template_id": tpl["id"],
        })

    # default web/email-to-case auto-response
    case_tpl = next((t for t in store.config_all("mf_email_templates")
                     if t.get("name") == "New case acknowledgment"), None)
    if case_tpl and not any(r.get("name") == "Web case auto-response"
                            for r in store.config_all("mf_auto_responses")):
        store.config_put("mf_auto_responses", {
            "name": "Web case auto-response",
            "object": "Case",
            "active": True,
            "order": 10,
            "criteria": {"in": [{"field": "Origin"}, ["Web", "Email"]]},
            "template_id": case_tpl["id"],
        })

    if store.count("Account") == 0:
        with open(os.path.join(METADATA_DIR, "seed_data.json")) as f:
            data = json.load(f)
        users = {u["username"]: u for u in security.list_users()}
        for rec in data["records"]:
            obj_def = registry.get_object(rec["object"])
            clean, errors = registry.validate_record(obj_def, rec["fields"])
            if errors:
                raise RuntimeError(f"Seed data invalid for {rec['object']}: {errors}")
            owner = users[rec["owner"]]
            clean["owner_id"] = owner["id"]
            clean["created_by"] = owner["id"]
            store.insert(rec["object"], clean)

    # seed a standard price book with demo products (quote-to-cash)
    if store.count("PriceBook") == 0:
        admin = security.get_user_by_username("admin")
        owner_id = admin["id"] if admin else None

        def _seed(obj_name, fields):
            rec = dict(fields)
            rec["owner_id"] = owner_id
            rec["created_by"] = owner_id
            return store.insert(obj_name, rec)

        pb = _seed("PriceBook", {"Name": "Standard Price Book",
                                 "Description": "Standard prices",
                                 "IsActive": True, "IsStandard": True})
        for name, code, family, price in [
                ("Widget Pro", "WDG-PRO", "Hardware", 499.00),
                ("Cloud Sync", "CLD-SYNC", "Software", 99.00),
                ("Onboarding", "SVC-ONB", "Services", 1499.00)]:
            pid = _seed("Product", {"Name": name, "ProductCode": code,
                                    "Family": family, "IsActive": True})
            _seed("PriceBookEntry", {"PriceBookId": pb, "ProductId": pid,
                                     "UnitPrice": price, "IsActive": True})

    # seed automotive demo data (Automotive Cloud-style objects)
    if store.count("VehicleDefinition") == 0:
        rep = security.get_user_by_username("leo")
        owner_id = rep["id"] if rep else None

        def _aseed(obj_name, fields):
            rec = dict(fields)
            rec["owner_id"] = owner_id
            rec["created_by"] = owner_id
            return store.insert(obj_name, rec)

        accts = [a for a in store.query("Account", owner_ids=None, limit=100)
                 if a.get("Name") == "Acme Corp"]
        acme = accts[0]["id"] if accts else None

        air = _aseed("VehicleDefinition", {
            "Name": "Lucid Air", "Make": "Lucid", "ModelYear": 2026,
            "Trim": "Pure", "BodyStyle": "Sedan", "BaseMSRP": 69900.00,
            "Description": "Luxury electric sedan."})
        grav = _aseed("VehicleDefinition", {
            "Name": "Lucid Gravity", "Make": "Lucid", "ModelYear": 2026,
            "Trim": "Touring", "BodyStyle": "SUV", "BaseMSRP": 79900.00,
            "Description": "Luxury electric SUV."})

        v1 = _aseed("Vehicle", {
            "Name": "Air Pure #101", "VIN": "7U4AA1C50RA101101",
            "VehicleDefinitionId": air, "AccountId": acme, "Status": "In Stock",
            "Odometer": 12, "Color": "Eureka Gold", "LicensePlate": ""})
        v2 = _aseed("Vehicle", {
            "Name": "Gravity Touring #201", "VIN": "7U4AA2C58RA201202",
            "VehicleDefinitionId": grav, "AccountId": acme, "Status": "Reserved",
            "Odometer": 8, "Color": "Stellar White", "LicensePlate": ""})

        p_air = _aseed("Product", {"Name": "Lucid Air Pure", "ProductCode": "AIR-PURE",
                                  "Family": "Vehicle", "IsActive": True})
        p_grav = _aseed("Product", {"Name": "Lucid Gravity Touring",
                                   "ProductCode": "GRAV-TOUR", "Family": "Vehicle",
                                   "IsActive": True})
        pbs = [p for p in store.query("PriceBook", owner_ids=None, limit=10)
               if p.get("Name") == "Standard Price Book"]
        if pbs:
            _aseed("PriceBookEntry", {"PriceBookId": pbs[0]["id"], "ProductId": p_air,
                                     "UnitPrice": 69900.00, "IsActive": True})
            _aseed("PriceBookEntry", {"PriceBookId": pbs[0]["id"], "ProductId": p_grav,
                                     "UnitPrice": 79900.00, "IsActive": True})

        ord1 = _aseed("Order", {
            "OrderNumber": "ORD-000001", "AccountId": acme, "Status": "Activated",
            "OrderDate": "2026-09-20",
            "Description": "Acme Corp fleet order: 1 Air Pure + 1 Gravity Touring."})
        _aseed("OrderItem", {"OrderId": ord1, "ProductId": p_air, "VehicleId": v1,
                             "Quantity": 1, "UnitPrice": 69900.00,
                             "LineTotal": 69900.00})
        _aseed("OrderItem", {"OrderId": ord1, "ProductId": p_grav, "VehicleId": v2,
                             "Quantity": 1, "UnitPrice": 79900.00,
                             "LineTotal": 79900.00})

        d1 = _aseed("Delivery", {
            "Name": "Delivery for ORD-000001", "DeliveryNumber": "DLV-000001",
            "OrderId": ord1, "VehicleId": v1, "AccountId": acme,
            "Status": "Scheduled", "ScheduledDate": "2026-10-05",
            "DeliveryAddress": "1 Fleet Way, Austin, TX 78701"})

        _aseed("Asset", {
            "Name": "Air Pure #101 Asset", "AccountId": acme, "ProductId": p_air,
            "VehicleId": v1, "SerialNumber": "7U4AA1C50RA101101",
            "Status": "Registered", "PurchaseDate": "2026-09-20",
            "WarrantyEndDate": "2030-09-20"})

    # seed service / marketing / contracts / knowledge demo data
    if store.count("Campaign") == 0:
        rep = security.get_user_by_username("leo")
        owner_id = rep["id"] if rep else None

        def _bseed(obj_name, fields):
            rec = dict(fields)
            rec["owner_id"] = owner_id
            rec["created_by"] = owner_id
            return store.insert(obj_name, rec)

        accts = [a for a in store.query("Account", owner_ids=None, limit=100)
                 if a.get("Name") == "Acme Corp"]
        acme = accts[0]["id"] if accts else None
        contacts = store.query("Contact", owner_ids=None, limit=100)
        c1 = contacts[0]["id"] if contacts else None
        c2 = contacts[1]["id"] if len(contacts) > 1 else None
        vehs = [v for v in store.query("Vehicle", owner_ids=None, limit=100)
                if v.get("VIN") == "7U4AA1C50RA101101"]
        v1 = vehs[0]["id"] if vehs else None

        camp = _bseed("Campaign", {
            "Name": "Q4 Launch Webinar", "Type": "Webinar", "Status": "In Progress",
            "StartDate": "2026-10-15", "EndDate": "2026-10-15",
            "BudgetedCost": 5000.00, "ExpectedRevenue": 200000.00,
            "Description": "Launch webinar for the 2026 lineup."})
        _bseed("CampaignMember", {"CampaignId": camp, "ContactId": c1,
                                 "Status": "Responded", "Responded": True})
        _bseed("CampaignMember", {"CampaignId": camp, "ContactId": c2,
                                 "Status": "Sent", "Responded": False})

        _bseed("Contract", {
            "ContractNumber": "C-000001", "AccountId": acme, "Status": "Activated",
            "StartDate": "2026-10-01", "EndDate": "2027-09-30", "ContractTerm": 12,
            "Description": "Acme Corp fleet service contract."})

        _bseed("KnowledgeArticle", {
            "Title": "How to pair your phone key", "ArticleNumber": "KA-000001",
            "Summary": "Pair a smartphone as a vehicle key.",
            "Body": "Open the mobile app, go to Vehicle > Phone Key, and follow the prompts.",
            "Status": "Published", "Category": "How-To", "ViewCount": 128})
        _bseed("KnowledgeArticle", {
            "Title": "Charging troubleshooting", "ArticleNumber": "KA-000002",
            "Summary": "Steps when the vehicle will not charge.",
            "Body": "Check the charge port light, try a different charger, then contact service.",
            "Status": "Published", "Category": "Troubleshooting", "ViewCount": 342})

        wo = _bseed("WorkOrder", {
            "WorkOrderNumber": "W-000001", "AccountId": acme, "ContactId": c1,
            "VehicleId": v1, "Status": "In Progress", "Priority": "High",
            "Subject": "Annual inspection",
            "Description": "Annual multi-point inspection and software update.",
            "ScheduledStart": "2026-10-06T09:00:00", "ScheduledEnd": "2026-10-06T12:00:00"})
        _bseed("ServiceAppointment", {
            "Name": "Inspection visit", "WorkOrderId": wo, "Status": "Scheduled",
            "ScheduledStart": "2026-10-06T09:00:00",
            "ScheduledEnd": "2026-10-06T12:00:00",
            "Technician": "Sam Rivera",
            "Address": "1 Fleet Way, Austin, TX 78701"})

        _bseed("Event", {
            "Subject": "Q4 business review", "EventType": "Meeting",
            "StartDateTime": "2026-10-20T14:00:00",
            "EndDateTime": "2026-10-20T15:00:00",
            "Location": "Acme HQ, Austin",
            "AccountId": acme, "ContactId": c1,
            "Description": "Quarterly review with Acme fleet team."})

    return store, registry, security
