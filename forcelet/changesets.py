"""Change sets: org-to-org metadata deployment. — Forcelet platform module.

A change set is a named, reviewable bundle of metadata components
(custom objects, fields, flows, validation rules, layouts, templates, ...)
captured from a source org.  It can be downloaded as JSON, uploaded into
another org, validated without applying, and then deployed.  Every validate
and deploy is recorded in the deployment history.

Deployment reuses the package machinery in :mod:`forcelet.automation`
(``build_package`` / ``import_package``), so change sets are a curated
*subset* of a package rather than a second deploy engine.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import json

from . import automation
from .field_types import is_valid_api_name
from .store import new_id, utcnow

CHANGESET_VERSION = 1

CS_TABLE = "mf_changesets"
DEPLOY_TABLE = "mf_deployments"

# component type -> (package section, match-key, label)
COMPONENT_TYPES = {
    "custom_object":    ("custom_objects", "name", "Custom object"),
    "object_field":     ("fields", "ref", "Object field"),
    "validation_rule":  ("validation_rules", "name", "Validation rule"),
    "flow":             ("flows", "name", "Flow"),
    "trigger":          ("triggers", "name", "Code trigger"),
    "approval_process": ("approval_processes", "name", "Approval process"),
    "assignment_rule":  ("assignment_rules", "name", "Assignment rule"),
    "layout":           ("layouts", "ref", "Page layout"),
    "email_template":   ("email_templates", "name", "Email template"),
    "list_view":        ("list_views", "name", "List view"),
    "record_type":      ("record_types", "name", "Record type"),
    "scheduled_job":    ("scheduled_jobs", "name", "Scheduled job"),
    "app":              ("apps", "name", "Application"),
}

STATUSES = ("Draft", "Outbound", "Inbound")


def _ensure(store) -> None:
    store._execute(
        f"""CREATE TABLE IF NOT EXISTS {CS_TABLE} (
            id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
            description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'Draft',
            components TEXT NOT NULL DEFAULT '[]',
            created_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            source_org TEXT NOT NULL DEFAULT '')"""
    )
    # package column holds the uploaded deployable package for Inbound sets
    try:
        store._execute(f"ALTER TABLE {CS_TABLE} ADD COLUMN package TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass
    store._execute(
        f"""CREATE TABLE IF NOT EXISTS {DEPLOY_TABLE} (
            id TEXT PRIMARY KEY, changeset_id TEXT NOT NULL,
            direction TEXT NOT NULL DEFAULT 'inbound',
            status TEXT NOT NULL, results TEXT NOT NULL DEFAULT '{{}}',
            log TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)"""
    )
    store._commit()


def _decode_cs(row: dict) -> dict:
    try:
        components = json.loads(row.get("components") or "[]")
    except Exception:
        components = []
    try:
        package = json.loads(row.get("package") or "")
    except Exception:
        package = None
    return {
        "id": row["id"], "name": row["name"],
        "description": row.get("description") or "",
        "status": row.get("status") or "Draft",
        "components": components if isinstance(components, list) else [],
        "created_by": row.get("created_by") or "",
        "created_at": row.get("created_at"),
        "source_org": row.get("source_org") or "",
        "has_package": bool(package),
        # internal: the stashed deployable package (not for API output)
        "_package": package,
    }


# ------------------------------------------------------------------ CRUD
def list_changesets(store) -> list:
    _ensure(store)
    return [_decode_cs(dict(r)) for r in
            store._execute(f"SELECT * FROM {CS_TABLE} ORDER BY created_at DESC").fetchall()]


def get_changeset(store, cs_id: str) -> dict | None:
    _ensure(store)
    rows = store._execute(f"SELECT * FROM {CS_TABLE} WHERE id=?", (cs_id,)).fetchall()
    return _decode_cs(dict(rows[0])) if rows else None


def create_changeset(store, name: str, description: str = "",
                     created_by: str = "", status: str = "Draft") -> dict:
    _ensure(store)
    name = (name or "").strip()
    if not name:
        raise ValueError("Change set name is required")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    row = {"id": new_id(), "name": name, "description": description or "",
           "status": status, "components": "[]", "created_by": created_by,
           "created_at": utcnow(), "source_org": ""}
    try:
        store._execute(
            f"INSERT INTO {CS_TABLE} (id, name, description, status, components,"
            " created_by, created_at, source_org) VALUES (?,?,?,?,?,?,?,?)",
            (row["id"], row["name"], row["description"], row["status"],
             row["components"], row["created_by"], row["created_at"], row["source_org"]),
        )
    except Exception as e:
        raise ValueError(f"could not create change set: {e}")
    store._commit()
    return _decode_cs(row)


def _save(store, cs: dict) -> None:
    store._execute(
        f"UPDATE {CS_TABLE} SET description=?, status=?, components=?, source_org=?,"
        " package=? WHERE id=?",
        (cs["description"], cs["status"], json.dumps(cs["components"]),
         cs.get("source_org") or "",
         json.dumps(cs.get("_package")) if cs.get("_package") else "", cs["id"]),
    )
    store._commit()


def _public(cs: dict) -> dict:
    """API-safe shape (drops the internal stashed package)."""
    return {k: v for k, v in cs.items() if k != "_package"}


def add_component(store, cs_id: str, ctype: str, ref: str) -> dict:
    if ctype not in COMPONENT_TYPES:
        raise ValueError(f"unknown component type {ctype!r}; "
                         f"valid: {sorted(COMPONENT_TYPES)}")
    ref = (ref or "").strip()
    if not ref:
        raise ValueError("component ref is required")
    cs = get_changeset(store, cs_id)
    if not cs:
        raise ValueError("change set not found")
    comp = {"type": ctype, "ref": ref,
            "label": COMPONENT_TYPES[ctype][2]}
    if comp in cs["components"]:
        return cs
    cs["components"].append(comp)
    _save(store, cs)
    return cs


def remove_component(store, cs_id: str, ctype: str, ref: str) -> dict:
    cs = get_changeset(store, cs_id)
    if not cs:
        raise ValueError("change set not found")
    cs["components"] = [c for c in cs["components"]
                         if not (c.get("type") == ctype and c.get("ref") == ref)]
    _save(store, cs)
    return cs


# ------------------------------------------------------- available components
def available_components(store, registry) -> dict:
    """Everything in this org that can be added to a change set."""
    _ensure(store)
    out: dict = {t: [] for t in COMPONENT_TYPES}
    for obj in registry.list_objects():
        name = obj["name"]
        if obj.get("is_custom"):
            out["custom_object"].append({"ref": name,
                                         "label": obj.get("label", name)})
        for f in obj.get("fields", []):
            out["object_field"].append(
                {"ref": f"{name}.{f['name']}",
                 "label": f"{obj.get('label', name)} · {f.get('label', f['name'])}"})
    for ctype in ("validation_rule", "flow", "trigger", "assignment_rule",
                  "approval_process", "email_template", "list_view",
                  "record_type", "scheduled_job", "app"):
        table = automation.PACKAGE_TABLES[{
            "validation_rule": "validation_rules", "flow": "flows",
            "trigger": "triggers", "assignment_rule": "assignment_rules",
            "approval_process": "approval_processes",
            "email_template": "email_templates", "list_view": "list_views",
            "record_type": "record_types", "scheduled_job": "scheduled_jobs",
            "app": "apps",
        }[ctype]][0]
        for d in store.config_all(table):
            nm = d.get("name") or d.get("id")
            obj = d.get("object")
            out[ctype].append({"ref": nm,
                               "label": f"{obj} · {nm}" if obj else str(nm)})
    for lay in store.layouts_all():
        ref = f"{lay.get('object')}:{lay.get('profile', 'Default')}:{lay.get('record_type', 'Default')}"
        out["layout"].append({"ref": ref, "label": ref.replace(":", " · ")})
    for k in out:
        out[k].sort(key=lambda c: c["label"].lower())
    return out


# ------------------------------------------------------------- packaging
def _component_package(store, registry, components: list) -> dict:
    """Build the deployable package dict containing only the components."""
    full = automation.build_package(store, registry)
    pkg = {"package_version": 1, "name": "forcelet-changeset",
           "exported_at": utcnow(), "custom_objects": [],
           "standard_object_fields": {}, "layouts": [], "config": {}}

    wanted_objects = {c["ref"] for c in components if c["type"] == "custom_object"}
    wanted_fields: dict = {}
    for c in components:
        if c["type"] == "object_field" and "." in c["ref"]:
            obj_name, field_name = c["ref"].split(".", 1)
            wanted_fields.setdefault(obj_name, set()).add(field_name)

    for obj in full.get("custom_objects", []):
        name = obj.get("name")
        fields = []
        if name in wanted_objects:
            fields = obj.get("fields", [])
        elif name in wanted_fields:
            fields = [f for f in obj.get("fields", [])
                      if f.get("name") in wanted_fields[name]]
        if fields or name in wanted_objects:
            pkg["custom_objects"].append({**obj, "fields": fields})

    for obj_name, fields in (full.get("standard_object_fields") or {}).items():
        if obj_name in wanted_fields:
            keep = [f for f in fields if f.get("name") in wanted_fields[obj_name]]
            if keep:
                pkg["standard_object_fields"][obj_name] = keep

    type_to_section = {"validation_rule": "validation_rules", "flow": "flows",
                       "trigger": "triggers", "approval_process": "approval_processes",
                       "assignment_rule": "assignment_rules",
                       "email_template": "email_templates", "list_view": "list_views",
                       "record_type": "record_types", "scheduled_job": "scheduled_jobs",
                       "app": "apps"}
    for ctype, section in type_to_section.items():
        refs = {c["ref"] for c in components if c["type"] == ctype}
        if refs:
            pkg["config"][section] = [
                d for d in (full.get("config", {}).get(section) or [])
                if (d.get("name") or d.get("id")) in refs]

    layout_refs = {c["ref"] for c in components if c["type"] == "layout"}
    if layout_refs:
        pkg["layouts"] = [
            lay for lay in full.get("layouts", [])
            if f"{lay.get('object')}:{lay.get('profile', 'Default')}:"
               f"{lay.get('record_type', 'Default')}" in layout_refs]
    return pkg


def export_changeset(store, registry, cs_id: str) -> dict:
    cs = get_changeset(store, cs_id)
    if not cs:
        raise ValueError("change set not found")
    return {
        "changeset_version": CHANGESET_VERSION,
        "changeset": {"name": cs["name"], "description": cs["description"],
                      "exported_at": utcnow(), "source_org": cs.get("source_org") or "",
                      "components": cs["components"]},
        "package": _component_package(store, registry, cs["components"]),
    }


def import_changeset_doc(store, doc: dict, created_by: str = "") -> dict:
    """Upload a change set file into this org as an Inbound change set."""
    if not isinstance(doc, dict) or doc.get("changeset_version") != CHANGESET_VERSION:
        raise ValueError("not a forcelet change set (changeset_version must be 1)")
    meta = doc.get("changeset") or {}
    cs = create_changeset(store, meta.get("name") or "Uploaded change set",
                          meta.get("description") or "", created_by, status="Inbound")
    for comp in meta.get("components") or []:
        if isinstance(comp, dict) and comp.get("type") in COMPONENT_TYPES and comp.get("ref"):
            cs["components"].append({"type": comp["type"], "ref": comp["ref"],
                                     "label": COMPONENT_TYPES[comp["type"]][2]})
    cs["source_org"] = meta.get("source_org") or ""
    cs["_package"] = doc.get("package") or None
    _save(store, cs)
    return _public(get_changeset(store, cs["id"]))


def _stashed_package(store, cs: dict) -> dict | None:
    return cs.get("_package")


def validate_changeset(store, registry, cs_id: str) -> dict:
    """Structural validation of a change set against THIS org (no changes)."""
    cs = get_changeset(store, cs_id)
    if not cs:
        raise ValueError("change set not found")
    errors, warnings = [], []
    for comp in cs["components"]:
        ctype, ref = comp.get("type"), comp.get("ref")
        if ctype not in COMPONENT_TYPES:
            errors.append(f"unknown component type {ctype!r}")
            continue
        if ctype == "custom_object":
            if not is_valid_api_name(ref):
                errors.append(f"custom object {ref!r} is not a valid API name")
            elif registry.get_object(ref):
                warnings.append(f"custom object {ref} already exists — fields will merge, "
                                "the object will not be replaced")
        elif ctype == "object_field":
            if "." not in ref:
                errors.append(f"field ref {ref!r} must look like Object.Field")
                continue
            obj_name, field_name = ref.split(".", 1)
            obj = registry.get_object(obj_name)
            if not obj:
                errors.append(f"field {ref}: object {obj_name} does not exist here")
            elif not is_valid_api_name(field_name):
                errors.append(f"field {ref}: not a valid API name")
            elif field_name in {f["name"] for f in obj.get("fields", [])}:
                warnings.append(f"field {ref} already exists — it will be skipped")
        elif ctype == "layout":
            obj_name = ref.split(":", 1)[0]
            if not registry.get_object(obj_name):
                errors.append(f"layout {ref}: object {obj_name} does not exist here")
        else:
            # config-backed components: object-scoped ones need the object
            obj_name = None
            table = automation.PACKAGE_TABLES[{
                "validation_rule": "validation_rules", "flow": "flows",
                "trigger": "triggers", "approval_process": "approval_processes",
                "assignment_rule": "assignment_rules", "email_template": "email_templates",
                "list_view": "list_views", "record_type": "record_types",
                "scheduled_job": "scheduled_jobs", "app": "apps"}[ctype]][0]
            for d in store.config_all(table):
                if (d.get("name") or d.get("id")) == ref:
                    obj_name = d.get("object")
                    warnings.append(f"{COMPONENT_TYPES[ctype][2].lower()} {ref!r} already "
                                    "exists — it will be updated in place")
                    break
    return {"errors": errors, "warnings": warnings,
            "component_count": len(cs["components"]),
            "valid": not errors}


def _record_deployment(store, cs_id: str, direction: str, status: str,
                       results: dict, log: str, created_by: str) -> dict:
    row = {"id": new_id(), "changeset_id": cs_id, "direction": direction,
           "status": status, "results": json.dumps(results), "log": log or "",
           "created_by": created_by, "created_at": utcnow()}
    store._execute(
        f"INSERT INTO {DEPLOY_TABLE} (id, changeset_id, direction, status, results,"
        " log, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (row["id"], row["changeset_id"], row["direction"], row["status"],
         row["results"], row["log"], row["created_by"], row["created_at"]),
    )
    store._commit()
    return {**row, "results": results}


def deploy_changeset(store, registry, cs_id: str, user: dict) -> dict:
    """Validate then deploy a change set into this org. Returns deployment."""
    cs = get_changeset(store, cs_id)
    if not cs:
        raise ValueError("change set not found")
    if not cs["components"]:
        raise ValueError("change set has no components")
    direction = "outbound" if cs["status"] == "Outbound" else "inbound"
    validation = validate_changeset(store, registry, cs_id)
    if not validation["valid"]:
        return _record_deployment(store, cs_id, direction, "Failed", validation,
                                  "validation errors:\n" + "\n".join(validation["errors"]),
                                  user.get("username", ""))
    package = _stashed_package(store, cs)
    if package is None:
        # outbound change set built from this org: package it fresh
        package = _component_package(store, registry, cs["components"])
    try:
        summary = automation.import_package(store, registry, package, user)
    except Exception as e:
        return _record_deployment(store, cs_id, direction, "Failed", validation,
                                  f"deploy error: {type(e).__name__}: {e}",
                                  user.get("username", ""))
    log_lines = [f"deployed {cs['name']} ({len(cs['components'])} components)"]
    for k, v in summary.items():
        log_lines.append(f"{k}: {v if not isinstance(v, dict) else json.dumps(v)}")
    if validation["warnings"]:
        log_lines.append("warnings:")
        log_lines.extend(f"- {w}" for w in validation["warnings"])
    return _record_deployment(store, cs_id, direction, "Deployed",
                              {"validation": validation, "summary": summary},
                              "\n".join(log_lines), user.get("username", ""))


def list_deployments(store, cs_id: str | None = None) -> list:
    _ensure(store)
    if cs_id:
        rows = store._execute(
            f"SELECT * FROM {DEPLOY_TABLE} WHERE changeset_id=? ORDER BY created_at DESC",
            (cs_id,)).fetchall()
    else:
        rows = store._execute(
            f"SELECT * FROM {DEPLOY_TABLE} ORDER BY created_at DESC LIMIT 100").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["results"] = json.loads(d.get("results") or "{}")
        except Exception:
            d["results"] = {}
        out.append(d)
    return out
