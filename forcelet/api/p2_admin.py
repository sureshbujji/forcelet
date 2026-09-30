"""P2 admin endpoints: divisions extras, seeding, i18n, delegated admin,
login branding, and announcements. — Forcelet REST API domain module.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import random
from datetime import datetime, timezone

from flask import Flask, jsonify, request

from .. import automation
from .. import delegated as _delegated
from .. import divisions as _divisions
from .. import i18n as _i18n
from .. import seedgen as _seedgen
from ..security import totp_required_for_user
from ..settings import LOGIN_KEY, get_settings
from ._shared import _audit, _do_create, require_admin, require_auth


def register(app: Flask):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security

    # ------------------------------------------------------- seed templates
    @app.get("/api/admin/seed-templates")
    @require_auth
    @require_admin
    def seed_list():
        return jsonify(store.config_all(_seedgen.TABLE))

    @app.post("/api/admin/seed-templates")
    @require_auth
    @require_admin
    def seed_create():
        body = request.json or {}
        tpl = {"name": (body.get("name") or "").strip(),
               "object": body.get("object") or "",
               "count": body.get("count") or 0,
               "field_rules": body.get("field_rules") or [],
               "active": body.get("active", True)}
        if not tpl["name"]:
            return jsonify({"error": "Template name is required"}), 422
        errors = _seedgen.validate_template(tpl, registry)
        if errors:
            return jsonify({"error": "Invalid template", "details": errors}), 422
        rid = store.config_put(_seedgen.TABLE, tpl)
        _audit("create", "seed-template", tpl["name"])
        return jsonify(store.config_get(_seedgen.TABLE, rid)), 201

    @app.put("/api/admin/seed-templates/<rid>")
    @require_auth
    @require_admin
    def seed_update(rid):
        old = store.config_get(_seedgen.TABLE, rid)
        if not old:
            return jsonify({"error": "Unknown seed template"}), 404
        body = request.json or {}
        body.pop("id", None)
        tpl = {**old, **body}
        errors = _seedgen.validate_template(tpl, registry)
        if errors:
            return jsonify({"error": "Invalid template", "details": errors}), 422
        store.config_put(_seedgen.TABLE, {**tpl, "id": rid})
        _audit("update", "seed-template", tpl.get("name") or rid)
        return jsonify(store.config_get(_seedgen.TABLE, rid))

    @app.delete("/api/admin/seed-templates/<rid>")
    @require_auth
    @require_admin
    def seed_delete(rid):
        old = store.config_get(_seedgen.TABLE, rid)
        ok = store.config_delete(_seedgen.TABLE, rid)
        if ok:
            _audit("delete", "seed-template", (old or {}).get("name") or rid)
        return jsonify({"deleted": ok})

    @app.post("/api/admin/seed-templates/<rid>/run")
    @require_auth
    @require_admin
    def seed_run(rid):
        tpl = store.config_get(_seedgen.TABLE, rid)
        if not tpl:
            return jsonify({"error": "Unknown seed template"}), 404
        body = request.json or {}
        if body.get("confirm") is not True:
            return jsonify({"error": "Seeding writes real records. Re-submit "
                                     "with {\"confirm\": true} to proceed."}), 422
        errors = _seedgen.validate_template(tpl, registry)
        if errors:
            return jsonify({"error": "Invalid template", "details": errors}), 422
        obj = registry.get_object(tpl["object"])
        fmap = {f["name"]: f for f in obj.get("fields", [])}
        editable = set(security.editable_fields(request.mf_user, obj))
        rng = random.Random()
        created, failed, err_list = 0, 0, []
        count = int(tpl["count"])
        for seq in range(1, count + 1):
            row = _seedgen.generate_row(tpl.get("field_rules"), fmap, seq, rng)
            row = {k: v for k, v in row.items() if k in editable}
            status, payload = _do_create(request.mf_user, tpl["object"], row,
                                         allow_duplicates=True)
            if status == 201:
                created += 1
            else:
                failed += 1
                if len(err_list) < 20:
                    err_list.append({"seq": seq,
                                     "details": [payload.get("error")]})
        tpl["last_run"] = {"at": datetime.now(timezone.utc).isoformat(),
                           "created": created, "failed": failed,
                           "by": request.mf_user["username"]}
        store.config_put(_seedgen.TABLE, tpl)
        _audit("seed", tpl["object"],
               f"template '{tpl['name']}': {created} created, {failed} failed")
        return jsonify({"created": created, "failed": failed,
                        "errors": err_list})

    # ------------------------------------------------------- i18n workbench
    @app.get("/api/admin/labels")
    @require_auth
    @require_admin
    def labels_list():
        return jsonify(store.config_all(_i18n.LABELS_TABLE))

    @app.post("/api/admin/labels")
    @require_auth
    @require_admin
    def labels_create():
        body = request.json or {}
        label = {"key": (body.get("key") or "").strip(),
                 "default_text": body.get("default_text") or "",
                 "description": body.get("description") or "",
                 "category": body.get("category") or ""}
        keys = {l.get("key") for l in store.config_all(_i18n.LABELS_TABLE)}
        errors = _i18n.validate_label(label, keys)
        if errors:
            return jsonify({"error": "Invalid label", "details": errors}), 422
        rid = store.config_put(_i18n.LABELS_TABLE, label)
        _audit("create", "custom-label", label["key"])
        return jsonify(store.config_get(_i18n.LABELS_TABLE, rid)), 201

    @app.put("/api/admin/labels/<rid>")
    @require_auth
    @require_admin
    def labels_update(rid):
        old = store.config_get(_i18n.LABELS_TABLE, rid)
        if not old:
            return jsonify({"error": "Unknown label"}), 404
        body = request.json or {}
        body.pop("id", None)
        if "key" in body and body["key"] != old.get("key"):
            return jsonify({"error": "Renaming a label key is not supported"}), 422
        label = {**old, **body}
        keys = {l.get("key") for l in store.config_all(_i18n.LABELS_TABLE)}
        keys.discard(old.get("key"))
        errors = _i18n.validate_label(label, keys)
        if errors:
            return jsonify({"error": "Invalid label", "details": errors}), 422
        store.config_put(_i18n.LABELS_TABLE, {**label, "id": rid})
        _audit("update", "custom-label", label.get("key") or rid)
        return jsonify(store.config_get(_i18n.LABELS_TABLE, rid))

    @app.delete("/api/admin/labels/<rid>")
    @require_auth
    @require_admin
    def labels_delete(rid):
        old = store.config_get(_i18n.LABELS_TABLE, rid)
        if not old:
            return jsonify({"error": "Unknown label"}), 404
        for tr in store.config_all(_i18n.TRANSLATIONS_TABLE):
            if tr.get("label_key") == old.get("key"):
                store.config_delete(_i18n.TRANSLATIONS_TABLE, tr["id"])
        store.config_delete(_i18n.LABELS_TABLE, rid)
        _audit("delete", "custom-label", old.get("key") or rid)
        return jsonify({"deleted": True})

    @app.get("/api/admin/labels/<rid>/translations")
    @require_auth
    @require_admin
    def translations_list(rid):
        label = store.config_get(_i18n.LABELS_TABLE, rid)
        if not label:
            return jsonify({"error": "Unknown label"}), 404
        return jsonify([t for t in store.config_all(_i18n.TRANSLATIONS_TABLE)
                        if t.get("label_key") == label.get("key")])

    @app.post("/api/admin/labels/<rid>/translations")
    @require_auth
    @require_admin
    def translations_upsert(rid):
        label = store.config_get(_i18n.LABELS_TABLE, rid)
        if not label:
            return jsonify({"error": "Unknown label"}), 404
        body = request.json or {}
        tr = {"label_key": label["key"],
              "language": (body.get("language") or "").strip(),
              "text": body.get("text") or ""}
        keys = {l.get("key") for l in store.config_all(_i18n.LABELS_TABLE)}
        errors = _i18n.validate_translation(tr, keys)
        if errors:
            return jsonify({"error": "Invalid translation",
                            "details": errors}), 422
        for existing in store.config_all(_i18n.TRANSLATIONS_TABLE):
            if existing.get("label_key") == tr["label_key"] and \
                    existing.get("language") == tr["language"]:
                store.config_put(_i18n.TRANSLATIONS_TABLE,
                                 {**existing, "text": tr["text"]})
                _audit("update", "translation",
                       f"{tr['label_key']}/{tr['language']}")
                return jsonify(store.config_get(_i18n.TRANSLATIONS_TABLE,
                                                existing["id"]))
        new_id = store.config_put(_i18n.TRANSLATIONS_TABLE, tr)
        _audit("create", "translation",
               f"{tr['label_key']}/{tr['language']}")
        return jsonify(store.config_get(_i18n.TRANSLATIONS_TABLE, new_id)), 201

    @app.delete("/api/admin/translations/<tid>")
    @require_auth
    @require_admin
    def translations_delete(tid):
        old = store.config_get(_i18n.TRANSLATIONS_TABLE, tid)
        ok = store.config_delete(_i18n.TRANSLATIONS_TABLE, tid)
        if ok:
            _audit("delete", "translation",
                   f"{(old or {}).get('label_key')}/{(old or {}).get('language')}")
        return jsonify({"deleted": ok})

    @app.get("/api/i18n/<language>")
    def i18n_pack(language):
        """Public language pack for the sign-in screens (no auth needed)."""
        _i18n.seed_defaults(store)
        return jsonify(_i18n.get_pack(store, language))

    # ------------------------------------------------- delegated admin groups
    @app.get("/api/admin/delegated-groups")
    @require_auth
    @require_admin
    def dgroup_list():
        groups = store.config_all(_delegated.TABLE)
        users = {u["id"]: u.get("username") for u in security.list_users()}
        out = []
        for g in groups:
            g2 = dict(g)
            g2["member_usernames"] = [users.get(uid, uid)
                                      for uid in g.get("members", [])]
            out.append(g2)
        return jsonify(out)

    @app.post("/api/admin/delegated-groups")
    @require_auth
    @require_admin
    def dgroup_create():
        body = request.json or {}
        # Validate member user ids exist.
        for uid in body.get("members") or []:
            if not security.get_user(uid):
                return jsonify({"error": f"Unknown user '{uid}'"}), 422
        group = {"name": (body.get("name") or "").strip(),
                 "description": body.get("description") or "",
                 "members": body.get("members") or [],
                 "scopes": body.get("scopes") or []}
        errors = _delegated.validate_group(group, store)
        if errors:
            return jsonify({"error": "Invalid delegated group",
                            "details": errors}), 422
        rid = store.config_put(_delegated.TABLE, group)
        _audit("create", "delegated-group", group["name"])
        return jsonify(store.config_get(_delegated.TABLE, rid)), 201

    @app.put("/api/admin/delegated-groups/<rid>")
    @require_auth
    @require_admin
    def dgroup_update(rid):
        old = store.config_get(_delegated.TABLE, rid)
        if not old:
            return jsonify({"error": "Unknown delegated group"}), 404
        body = request.json or {}
        body.pop("id", None)
        if "members" in body:
            for uid in body.get("members") or []:
                if not security.get_user(uid):
                    return jsonify({"error": f"Unknown user '{uid}'"}), 422
        group = {**old, **body, "id": rid}
        errors = _delegated.validate_group(group, store)
        if errors:
            return jsonify({"error": "Invalid delegated group",
                            "details": errors}), 422
        store.config_put(_delegated.TABLE, group)
        _audit("update", "delegated-group", group.get("name") or rid)
        return jsonify(store.config_get(_delegated.TABLE, rid))

    @app.delete("/api/admin/delegated-groups/<rid>")
    @require_auth
    @require_admin
    def dgroup_delete(rid):
        old = store.config_get(_delegated.TABLE, rid)
        ok = store.config_delete(_delegated.TABLE, rid)
        if ok:
            _audit("delete", "delegated-group", (old or {}).get("name") or rid)
        return jsonify({"deleted": ok})

    @app.get("/api/admin/delegated-groups/mine")
    @require_auth
    def dgroup_mine():
        """Scopes the current user holds through delegated groups."""
        user = request.mf_user
        out = []
        for g in _delegated.groups_for_user(store, user["id"]):
            out.append({"group": g.get("name"), "scopes": g.get("scopes") or []})
        return jsonify({"admin": security.is_admin(user), "grants": out})

    # ------------------------------------------------- login branding (public)
    @app.get("/api/public/login-branding")
    def login_branding():
        """Public branding for the sign-in screens (no auth needed)."""
        s = get_settings(store, LOGIN_KEY)
        return jsonify({"logo_url": s["logo_url"], "headline": s["headline"],
                        "tagline": s["tagline"],
                        "primary_color": s["primary_color"],
                        "background": s["background"]})

    # ------------------------------------------------- post-login flows
    @app.get("/api/me/announcement")
    @require_auth
    def my_announcement():
        """Pending post-login items: announcement banner + 2FA nudge."""
        s = get_settings(store, LOGIN_KEY)
        flow = {step.get("key"): bool(step.get("enabled"))
                for step in s.get("login_flow") or []}
        out: dict = {"announcement": None, "totp_nudge": False}
        user = request.mf_user
        if s.get("announcement_enabled") and flow.get("announcement_banner", True) \
                and (s.get("announcement_title") or s.get("announcement_body")):
            dismissed = store.meta_kv_get(
                f"announcement_dismissed:{user['id']}")
            stamp = f"{s.get('announcement_title')}|{s.get('announcement_body')}"
            if dismissed != stamp:
                out["announcement"] = {
                    "title": s["announcement_title"],
                    "body": s["announcement_body"]}
        if flow.get("totp_enrollment_nudge", True) and not user.get("totp_secret"):
            if not totp_required_for_user(store, user):
                out["totp_nudge"] = True
        return jsonify(out)

    @app.post("/api/me/announcement/dismiss")
    @require_auth
    def dismiss_announcement():
        s = get_settings(store, LOGIN_KEY)
        user = request.mf_user
        stamp = f"{s.get('announcement_title')}|{s.get('announcement_body')}"
        store.meta_kv_set(f"announcement_dismissed:{user['id']}", stamp)
        return jsonify({"dismissed": True})

    # ------------------------------------------------- division record tools
    @app.get("/api/admin/divisions/usage")
    @require_auth
    @require_admin
    def divisions_usage():
        counts = {}
        for d in store.config_all(_divisions.TABLE):
            counts[d["id"]] = _divisions.count_records(store, d["id"])
        return jsonify(counts)

    @app.post("/api/admin/divisions/<rid>/move")
    @require_auth
    @require_admin
    def divisions_move(rid):
        """Move records into a division (or back to global with null)."""
        if rid != "global" and not _divisions.get_division(store, rid):
            return jsonify({"error": "Unknown division"}), 404
        body = request.json or {}
        moves = []
        for item in body.get("records") or []:
            obj_name = (item or {}).get("object")
            record_id = (item or {}).get("id")
            if not obj_name or not record_id or \
                    not registry.get_object(obj_name):
                return jsonify({"error": "Each record needs a valid "
                                         "'object' and 'id'"}), 422
            if not store.get(obj_name, record_id):
                return jsonify({"error": f"Unknown record {obj_name}/"
                                         f"{record_id}"}), 422
            moves.append((obj_name, record_id))
        target = None if rid == "global" else rid
        n = _divisions.move_records(store, target, moves)
        _audit("move", "division", rid, f"{n} record(s)")
        return jsonify({"moved": n})

    @app.put("/api/sobjects/<obj_name>/<record_id>/division")
    @require_auth
    @require_admin
    def record_set_division(obj_name, record_id):
        """Set (or clear) a single record's division. Admins only."""
        if not registry.get_object(obj_name):
            return jsonify({"error": "Unknown object"}), 404
        if not store.get(obj_name, record_id):
            return jsonify({"error": "Unknown record"}), 404
        body = request.json or {}
        div_id = (body.get("division_id") or "").strip() or None
        try:
            _divisions.set_record_division(store, obj_name, record_id, div_id)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("update", "division", obj_name, record_id)
        return jsonify({"division_id": div_id,
                        "division": _divisions.record_division_name(
                            store, obj_name, record_id)})
