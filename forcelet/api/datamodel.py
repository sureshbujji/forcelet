"""App data-model endpoints: territories, person accounts, big objects, archival.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

from flask import Flask, jsonify, request

from .. import datamodel
from ._shared import _audit, current_user, require_admin, require_auth


def register(app: Flask):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security

    # ------------------------------------------------------- territories
    def _terr_with_depth(t):
        d, cur = 0, t.get("parent_id")
        while cur:
            parent = store.config_get(datamodel.TERRITORY_TABLE, cur)
            if not parent:
                break
            d += 1
            cur = parent.get("parent_id")
        return {**t, "depth": d}

    @app.get("/api/admin/territories")
    @require_auth
    @require_admin
    def list_territories():
        return jsonify(datamodel.territory_tree(store))

    @app.post("/api/admin/territories")
    @require_auth
    @require_admin
    def create_territory():
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name is required"}), 422
        parent = body.get("parent_id")
        if parent and not store.config_get(datamodel.TERRITORY_TABLE, parent):
            return jsonify({"error": "Unknown parent territory"}), 422
        tid = store.config_put(datamodel.TERRITORY_TABLE, {
            "name": name, "parent_id": parent,
            "description": body.get("description") or ""})
        _audit("create", "territory", name)
        return jsonify(_terr_with_depth(store.config_get(datamodel.TERRITORY_TABLE, tid))), 201

    @app.patch("/api/admin/territories/<tid>")
    @require_auth
    @require_admin
    def update_territory(tid):
        old = store.config_get(datamodel.TERRITORY_TABLE, tid)
        if not old:
            return jsonify({"error": "Not found"}), 404
        body = request.get_json(silent=True) or {}
        body.pop("id", None)
        merged = {**old, **body}
        # prevent hierarchy cycles
        seen, cur = set(), merged.get("parent_id")
        while cur:
            if cur == tid or cur in seen:
                return jsonify({"error": "Territory hierarchy would cycle"}), 422
            seen.add(cur)
            parent = store.config_get(datamodel.TERRITORY_TABLE, cur)
            cur = parent and parent.get("parent_id")
        store.config_put(datamodel.TERRITORY_TABLE, {**merged, "id": tid})
        _audit("update", "territory", merged.get("name") or tid)
        return jsonify(_terr_with_depth(store.config_get(datamodel.TERRITORY_TABLE, tid)))

    @app.delete("/api/admin/territories/<tid>")
    @require_auth
    @require_admin
    def delete_territory(tid):
        old = store.config_get(datamodel.TERRITORY_TABLE, tid)
        if not old:
            return jsonify({"error": "Not found"}), 404
        children = [t for t in store.config_all(datamodel.TERRITORY_TABLE)
                    if t.get("parent_id") == tid]
        if children:
            return jsonify({"error": "Territory has child territories; delete them first"}), 422
        datamodel.ensure_territory_tables(store)
        store._execute("DELETE FROM mf_territory_members WHERE territory_id=?", (tid,))
        store._execute("DELETE FROM mf_account_territories WHERE territory_id=?", (tid,))
        store._execute("DELETE FROM mf_record_territories WHERE territory_id=?", (tid,))
        for rule in store.config_all(datamodel.TERRITORY_RULE_TABLE):
            if rule.get("territory_id") == tid:
                store.config_delete(datamodel.TERRITORY_RULE_TABLE, rule["id"])
        store.config_delete(datamodel.TERRITORY_TABLE, tid)
        store._commit()
        _audit("delete", "territory", (old or {}).get("name") or tid)
        return jsonify({"deleted": True})

    @app.get("/api/admin/territories/<tid>/users")
    @require_auth
    @require_admin
    def territory_users(tid):
        if not store.config_get(datamodel.TERRITORY_TABLE, tid):
            return jsonify({"error": "Not found"}), 404
        members = datamodel.territory_members(store, tid)
        users = {u["id"]: u.get("name", u["username"]) for u in security.list_users()}
        for m in members:
            m["user_name"] = users.get(m["user_id"], m["user_id"])
        return jsonify(members)

    @app.post("/api/admin/territories/<tid>/users")
    @require_auth
    @require_admin
    def territory_add_user(tid):
        body = request.get_json(silent=True) or {}
        user = security.get_user(body.get("user_id") or "")
        if not user:
            return jsonify({"error": "Unknown user"}), 422
        try:
            datamodel.assign_user_to_territory(store, tid, user["id"],
                                              body.get("role") or "member")
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("update", "territory", f"{tid}: add user {user['username']}")
        return jsonify({"added": True}), 201

    @app.delete("/api/admin/territories/<tid>/users/<uid>")
    @require_auth
    @require_admin
    def territory_remove_user(tid, uid):
        ok = datamodel.remove_user_from_territory(store, tid, uid)
        return jsonify({"deleted": ok})

    # ------------------------------------------------- territory rules
    @app.get("/api/admin/territory-rules")
    @require_auth
    @require_admin
    def list_territory_rules():
        rules = store.config_all(datamodel.TERRITORY_RULE_TABLE)
        terrs = {t["id"]: t["name"] for t in store.config_all(datamodel.TERRITORY_TABLE)}
        for r in rules:
            r["territory_name"] = terrs.get(r.get("territory_id"), "?")
        return jsonify(sorted(rules, key=lambda r: r.get("priority") or 0))

    @app.post("/api/admin/territory-rules")
    @require_auth
    @require_admin
    def create_territory_rule():
        body = request.get_json(silent=True) or {}
        if not store.config_get(datamodel.TERRITORY_TABLE, body.get("territory_id") or ""):
            return jsonify({"error": "Unknown territory"}), 422
        obj_name = body.get("object") or "Account"
        if not registry.get_object(obj_name):
            return jsonify({"error": f"Unknown object '{obj_name}'"}), 422
        rid = store.config_put(datamodel.TERRITORY_RULE_TABLE, {
            "name": body.get("name") or "Rule",
            "object": obj_name,
            "territory_id": body.get("territory_id"),
            "criteria": body.get("criteria") or {},
            "priority": int(body.get("priority") or 0),
            "active": bool(body.get("active", True))})
        _audit("create", "territory-rule", body.get("name") or rid)
        return jsonify(store.config_get(datamodel.TERRITORY_RULE_TABLE, rid)), 201

    @app.patch("/api/admin/territory-rules/<rid>")
    @require_auth
    @require_admin
    def update_territory_rule(rid):
        old = store.config_get(datamodel.TERRITORY_RULE_TABLE, rid)
        if not old:
            return jsonify({"error": "Not found"}), 404
        body = request.get_json(silent=True) or {}
        body.pop("id", None)
        if body.get("object") and not registry.get_object(body["object"]):
            return jsonify({"error": f"Unknown object '{body['object']}'"}), 422
        store.config_put(datamodel.TERRITORY_RULE_TABLE, {**old, **body, "id": rid})
        _audit("update", "territory-rule", old.get("name") or rid)
        return jsonify(store.config_get(datamodel.TERRITORY_RULE_TABLE, rid))

    @app.delete("/api/admin/territory-rules/<rid>")
    @require_auth
    @require_admin
    def delete_territory_rule(rid):
        ok = store.config_delete(datamodel.TERRITORY_RULE_TABLE, rid)
        return jsonify({"deleted": ok})

    @app.post("/api/admin/territory-rules/run")
    @require_auth
    @require_admin
    def run_territory_rules():
        body = request.get_json(silent=True) or {}
        try:
            result = datamodel.run_territory_assignment(store, registry,
                                                       body.get("rule_id"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("update", "territory-rule", f"assignment run: {result}")
        return jsonify(result)

    @app.get("/api/sobjects/Account/<rid>/territories")
    @require_auth
    def account_territories(rid):
        user = request.mf_user
        acc = store.get("Account", rid)
        if not acc or not security.can_see_record(user, acc, "Account"):
            return jsonify({"error": "Not found"}), 404
        tids = datamodel.account_territories(store, rid)
        terrs = {t["id"]: t["name"] for t in store.config_all(datamodel.TERRITORY_TABLE)}
        return jsonify([{"id": t, "name": terrs.get(t, "?")} for t in tids])

    @app.get("/api/sobjects/<obj_name>/<rid>/territories")
    @require_auth
    def record_territories(obj_name, rid):
        user = request.mf_user
        obj = registry.get_object(obj_name)
        rec = obj and store.get(obj_name, rid)
        if not obj or not rec or not security.can_see_record(user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        tids = datamodel.record_territories(store, obj_name, rid)
        terrs = {t["id"]: t["name"] for t in store.config_all(datamodel.TERRITORY_TABLE)}
        return jsonify([{"id": t, "name": terrs.get(t, "?")} for t in tids])

    # ------------------------------------------------- person accounts
    @app.get("/api/admin/person-accounts")
    @require_auth
    @require_admin
    def person_account_status():
        return jsonify({"enabled": datamodel.person_accounts_enabled(store)})

    @app.post("/api/admin/person-accounts/enable")
    @require_auth
    @require_admin
    def person_account_enable():
        already = datamodel.person_accounts_enabled(store)
        try:
            result = datamodel.enable_person_accounts(store, registry)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("update", "person-accounts", "enabled")
        return jsonify({**result, "already_enabled": already})

    # ------------------------------------------------- big objects
    @app.get("/api/admin/big-objects")
    @require_auth
    @require_admin
    def list_big_objects():
        return jsonify([{"name": o["name"], "label": o.get("label"),
                         "fields": len(o.get("fields", [])),
                         "records": store.count(o["name"])}
                        for o in registry.list_objects()
                        if datamodel.is_big_object(o)])

    @app.post("/api/admin/big-objects")
    @require_auth
    @require_admin
    def create_big_object():
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        if not name.endswith("__b"):
            return jsonify({"error": "Big Object API names must end with __b"}), 422
        try:
            obj = datamodel.ensure_big_object(store, registry, name,
                                              body.get("label") or "")
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("create", "big-object", name)
        return jsonify({"name": obj["name"], "label": obj.get("label")}), 201

    # ------------------------------------------------- archive rules
    @app.get("/api/admin/archive-rules")
    @require_auth
    @require_admin
    def list_archive_rules():
        return jsonify(store.config_all(datamodel.ARCHIVE_RULE_TABLE))

    @app.post("/api/admin/archive-rules")
    @require_auth
    @require_admin
    def create_archive_rule():
        body = request.get_json(silent=True) or {}
        obj_name = body.get("object")
        obj = registry.get_object(obj_name or "")
        if not obj:
            return jsonify({"error": "Unknown object"}), 422
        if datamodel.is_big_object(obj):
            return jsonify({"error": "Cannot archive from a Big Object"}), 422
        try:
            age = int(body.get("age_days") or 365)
            if age <= 0:
                raise ValueError()
        except (TypeError, ValueError):
            return jsonify({"error": "age_days must be a positive integer"}), 422
        target = (body.get("target") or "").strip() or f"{obj_name}Archive__b"
        if not target.endswith("__b"):
            return jsonify({"error": "Archive target must be a Big Object name ending in __b"}), 422
        rid = store.config_put(datamodel.ARCHIVE_RULE_TABLE, {
            "name": body.get("name") or f"{obj_name} archival",
            "object": obj_name, "age_days": age, "target": target,
            "active": bool(body.get("active", True))})
        _audit("create", "archive-rule", body.get("name") or rid)
        return jsonify(store.config_get(datamodel.ARCHIVE_RULE_TABLE, rid)), 201

    @app.patch("/api/admin/archive-rules/<rid>")
    @require_auth
    @require_admin
    def update_archive_rule(rid):
        old = store.config_get(datamodel.ARCHIVE_RULE_TABLE, rid)
        if not old:
            return jsonify({"error": "Not found"}), 404
        body = request.get_json(silent=True) or {}
        body.pop("id", None)
        store.config_put(datamodel.ARCHIVE_RULE_TABLE, {**old, **body, "id": rid})
        return jsonify(store.config_get(datamodel.ARCHIVE_RULE_TABLE, rid))

    @app.delete("/api/admin/archive-rules/<rid>")
    @require_auth
    @require_admin
    def delete_archive_rule(rid):
        return jsonify({"deleted": store.config_delete(datamodel.ARCHIVE_RULE_TABLE, rid)})

    @app.post("/api/admin/archive-rules/<rid>/run")
    @require_auth
    @require_admin
    def run_archive_rule(rid):
        user = request.mf_user
        try:
            result = datamodel.run_archive_rule(store, registry, rid, user)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("update", "archive-rule", f"run {rid}: moved {result['moved']}")
        return jsonify(result)

    # ------------------------------------------------- relationships view
    @app.get("/api/admin/relationships")
    @require_auth
    @require_admin
    def list_relationships():
        rows = []
        for obj in registry.list_objects():
            for f in datamodel.relationship_fields(obj):
                rows.append({"object": obj["name"], "field": f["name"],
                             "label": f.get("label"), "type": f["type"],
                             "target": f.get("reference_to"),
                             "required": bool(f.get("required")),
                             "reparentable": f.get("reparentable")})
        return jsonify(sorted(rows, key=lambda r: (r["object"], r["field"])))
