"""Sales core: opportunity products, revenue schedules, quote syncing,
account hierarchy, opportunity contact roles, teams and splits. — Forcelet
platform module.

Exposes ``register(app)`` following the other domain modules. New objects are
defined in ``metadata/fragments/sales_core_objects.json`` and are expected to
be merged into the metadata at deploy time; nothing here depends on import
time app state.

Merge notes for the main agent:
- The fragment adds 6 new objects: OpportunityLineItem, RevenueSchedule,
  QuoteSync, OpportunityContactRole, OpportunityTeamMember, OpportunitySplit.
- ``Account`` needs a ``ParentAccountId`` self-lookup field added to
  ``metadata/standard_objects.json`` for the hierarchy endpoint (the API only
  reads/writes it; tests add it at runtime).
- ``OpportunityTeamMember.UserId`` is a Text field holding an mf_users id
  (User is not a metadata object in Forcelet); it is validated server-side.
- Quote sync state lives on the ``QuoteSync`` object instead of new fields on
  Quote/Opportunity, so no existing object definitions were touched. If real
  ``Quote.IsSyncing`` / ``Opportunity.SyncedQuoteId`` fields are added later,
  the sync endpoints can mirror them.

Behavior notes:
- OpportunityLineItem.TotalPrice is computed server-side on every save:
  Quantity * UnitPrice * (1 - Discount/100), rounded to 2 decimals.
- Opportunity.Amount is recomputed server-side after line-item create,
  update, delete, price-book add, and quote sync.
- Opportunity splits are strict: whenever any splits of a SplitType exist on
  an opportunity they must total exactly 100 (within 0.01). Use
  POST /api/sales/opportunities/<id>/splits/replace for atomic rebalancing.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import calendar
import threading
from datetime import date
from flask import Flask, jsonify, request

from ._shared import (
    _do_create, _do_update, _visible_records, current_user, require_auth,
    recompute_stored_rollups, serialize, ctx,
)

# Serializes check-then-act sequences (split total validation, quote sync)
# the same way _FS_LOCK does in field_service.py. Process-local only.
_SALES_LOCK = threading.Lock()

_SPLIT_TYPES = ("Revenue", "Credit")


# ------------------------------------------------------------------ helpers
def _ser(user, obj_name, rec):
    store, registry, _sec = ctx()
    return serialize(user, registry.get_object(obj_name), rec)


def _visible_map(user, obj_name):
    recs, _ = _visible_records(user, obj_name)
    return {r["id"]: r for r in recs}


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _total_price(qty, price, discount):
    return round(_num(qty) * _num(price) * (1.0 - _num(discount) / 100.0), 2)


def _pct_err(label, v):
    if v is None:
        return f"{label} is required"
    if not 0 <= v <= 100:
        return f"{label} must be between 0 and 100"
    return None


def _opp(user, opp_id):
    if not opp_id:
        return None
    return _visible_map(user, "Opportunity").get(opp_id)


def _recompute_amount(user, opp_id):
    """Recompute Opportunity.Amount from its visible line items."""
    total = round(sum(_num(r.get("TotalPrice"))
                      for r in _visible_map(user, "OpportunityLineItem").values()
                      if r.get("OpportunityId") == opp_id), 2)
    _do_update(user, "Opportunity", opp_id, {"Amount": total})
    return total


def _add_months(d: date, n: int) -> date:
    month = d.month - 1 + n
    y, m = d.year + month // 12, month % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _parse_date(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError, AttributeError):
        return None


def _recycle_delete(user, obj_name, rid):
    """Delete honoring the recycle bin, mirroring records.delete_record."""
    store, _reg, _sec = ctx()
    rec = store.get(obj_name, rid)
    if not rec:
        return False
    store.recycle_put(obj_name, rec, user["id"])
    store.delete(obj_name, rid)
    recompute_stored_rollups(user, obj_name, rec)
    return True


# ------------------------------------------------------------------- API
def register(app: Flask):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security

    # ------------------------------------- opportunity line items
    def _oli_payload(body, existing=None):
        base = dict(existing or {})
        for k in ("OpportunityId", "PriceBookEntryId", "ProductId",
                  "SourceQuoteId", "Quantity", "UnitPrice", "Discount",
                  "ServiceDate", "Description"):
            if k in body:
                base[k] = body[k]
        qty = _num(base.get("Quantity"), 1)
        price = _num(base.get("UnitPrice"))
        disc = _num(base.get("Discount"))
        err = _pct_err("Discount", disc)
        if err:
            return None, err
        if qty < 0:
            return None, "Quantity cannot be negative"
        if price < 0:
            return None, "UnitPrice cannot be negative"
        base["Quantity"] = qty
        base["UnitPrice"] = price
        base["Discount"] = disc
        base["TotalPrice"] = _total_price(qty, price, disc)
        return base, None

    @app.post("/api/sales/opportunity-line-items")
    @require_auth
    def oli_create():
        user = current_user()
        body = request.json or {}
        if not _opp(user, body.get("opportunity_id") or body.get("OpportunityId")):
            return jsonify({"error": "Opportunity not found"}), 404
        fields, err = _oli_payload(body)
        if err:
            return jsonify({"error": err}), 422
        fields["OpportunityId"] = body.get("opportunity_id") or body.get("OpportunityId")
        code, data = _do_create(user, "OpportunityLineItem", fields)
        if code not in (200, 201):
            return jsonify(data), code
        return jsonify(data), code

    @app.get("/api/sales/opportunity-line-items")
    @require_auth
    def oli_list():
        user = current_user()
        opp_id = request.args.get("opportunity_id")
        recs = [r for r in _visible_map(user, "OpportunityLineItem").values()
                if not opp_id or r.get("OpportunityId") == opp_id]
        recs.sort(key=lambda r: r.get("created_date") or "")
        return jsonify([_ser(user, "OpportunityLineItem", r) for r in recs])

    @app.get("/api/sales/opportunity-line-items/<rid>")
    @require_auth
    def oli_get(rid):
        user = current_user()
        rec = _visible_map(user, "OpportunityLineItem").get(rid)
        if not rec:
            return jsonify({"error": "Not found"}), 404
        out = _ser(user, "OpportunityLineItem", rec)
        out["schedules"] = [_ser(user, "RevenueSchedule", s)
                            for s in _visible_map(user, "RevenueSchedule").values()
                            if s.get("OpportunityLineItemId") == rid]
        out["schedules"].sort(key=lambda s: s.get("Period") or "")
        return jsonify(out)

    @app.put("/api/sales/opportunity-line-items/<rid>")
    @require_auth
    def oli_update(rid):
        user = current_user()
        rec = _visible_map(user, "OpportunityLineItem").get(rid)
        if not rec:
            return jsonify({"error": "Not found"}), 404
        fields, err = _oli_payload(request.json or {}, existing=rec)
        if err:
            return jsonify({"error": err}), 422
        # keep only real field names for the update
        obj = registry.get_object("OpportunityLineItem")
        allowed = {f["name"] for f in obj["fields"]}
        code, data = _do_update(user, "OpportunityLineItem", rid,
                               {k: v for k, v in fields.items() if k in allowed})
        if code not in (200, 201):
            return jsonify(data), code
        return jsonify(data), code

    @app.delete("/api/sales/opportunity-line-items/<rid>")
    @require_auth
    def oli_delete(rid):
        user = current_user()
        rec = _visible_map(user, "OpportunityLineItem").get(rid)
        if not rec:
            return jsonify({"error": "Not found"}), 404
        if not security.can(user, "delete", "OpportunityLineItem"):
            return jsonify({"error": "Not found"}), 404
        for s in list(_visible_map(user, "RevenueSchedule").values()):
            if s.get("OpportunityLineItemId") == rid:
                _recycle_delete(user, "RevenueSchedule", s["id"])
        _recycle_delete(user, "OpportunityLineItem", rid)
        return jsonify({"deleted": True})

    @app.post("/api/sales/opportunity-line-items/from-pricebook")
    @require_auth
    def oli_from_pricebook():
        user = current_user()
        body = request.json or {}
        opp_id = body.get("opportunity_id")
        if not _opp(user, opp_id):
            return jsonify({"error": "Opportunity not found"}), 404
        items = body.get("items") or []
        if not items:
            return jsonify({"error": "items required"}), 422
        pb_id = body.get("pricebook_id")
        entries = _visible_map(user, "PriceBookEntry")
        created = []
        with _SALES_LOCK:
            for it in items:
                pbe = entries.get(it.get("pricebookentry_id") or "")
                if not pbe:
                    return jsonify({"error": "PriceBookEntry not found: %s"
                                           % it.get("pricebookentry_id")}), 422
                if not pbe.get("IsActive", True):
                    return jsonify({"error": "PriceBookEntry is not active"}), 422
                if pb_id and pbe.get("PriceBookId") != pb_id:
                    return jsonify({"error": "PriceBookEntry is not in the given price book"}), 422
                qty = _num(it.get("quantity"), 1)
                price = _num(pbe.get("UnitPrice"))
                disc = _num(it.get("discount"))
                err = _pct_err("Discount", disc)
                if err:
                    return jsonify({"error": err}), 422
                if qty <= 0:
                    return jsonify({"error": "quantity must be positive"}), 422
                code, data = _do_create(user, "OpportunityLineItem", {
                    "OpportunityId": opp_id,
                    "PriceBookEntryId": pbe["id"],
                    "ProductId": pbe.get("ProductId"),
                    "Quantity": qty, "UnitPrice": price, "Discount": disc,
                    "TotalPrice": _total_price(qty, price, disc)})
                if code not in (200, 201):
                    return jsonify(data), code
                created.append(data["Id"])
            _recompute_amount(user, opp_id)
        return jsonify({"created": created}), 201

    # ------------------------------------- revenue schedules
    @app.post("/api/sales/revenue-schedules/generate")
    @require_auth
    def schedule_generate():
        user = current_user()
        body = request.json or {}
        oli = _visible_map(user, "OpportunityLineItem").get(body.get("line_item_id") or "")
        if not oli:
            return jsonify({"error": "OpportunityLineItem not found"}), 404
        try:
            n = int(body.get("installments"))
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return jsonify({"error": "installments must be a positive integer"}), 422
        start = _parse_date(body.get("start_date"))
        if not start:
            return jsonify({"error": "start_date must be YYYY-MM-DD"}), 422
        total = round(_num(oli.get("TotalPrice")), 2)
        if total <= 0:
            return jsonify({"error": "Line item total must be positive"}), 422
        created = []
        with _SALES_LOCK:
            # drop any previous schedules for a clean regenerate
            for s in list(_visible_map(user, "RevenueSchedule").values()):
                if s.get("OpportunityLineItemId") == oli["id"]:
                    _recycle_delete(user, "RevenueSchedule", s["id"])
            amounts = [round(total / n, 2)] * n
            amounts[-1] = round(total - sum(amounts[:-1]), 2)
            for i in range(n):
                period = _add_months(start, i).isoformat()
                code, data = _do_create(user, "RevenueSchedule", {
                    "OpportunityLineItemId": oli["id"], "Period": period,
                    "Amount": amounts[i], "Type": "Revenue",
                    "Description": f"Installment {i + 1} of {n}"})
                if code not in (200, 201):
                    return jsonify(data), code
                created.append(data["Id"])
        return jsonify({"created": created,
                        "schedules": [_ser(user, "RevenueSchedule",
                                           _visible_map(user, "RevenueSchedule")[i])
                                      for i in created]}), 201

    @app.get("/api/sales/revenue-schedules")
    @require_auth
    def schedule_list():
        user = current_user()
        li_id = request.args.get("line_item_id")
        recs = [r for r in _visible_map(user, "RevenueSchedule").values()
                if not li_id or r.get("OpportunityLineItemId") == li_id]
        recs.sort(key=lambda r: r.get("Period") or "")
        return jsonify([_ser(user, "RevenueSchedule", r) for r in recs])

    # ------------------------------------- quote syncing
    def _qli_key(q):
        pbe = q.get("PriceBookEntryId")
        if pbe:
            return ("pbe", pbe)
        return ("adhoc", _num(q.get("Quantity")), _num(q.get("UnitPrice")),
                _num(q.get("Discount")))

    @app.post("/api/sales/quotes/<quote_id>/sync")
    @require_auth
    def quote_sync(quote_id):
        user = current_user()
        quote = _visible_map(user, "Quote").get(quote_id)
        if not quote:
            return jsonify({"error": "Quote not found"}), 404
        opp_id = quote.get("OpportunityId")
        if not opp_id:
            return jsonify({"error": "Quote has no Opportunity"}), 422
        if not _opp(user, opp_id):
            return jsonify({"error": "Opportunity not found"}), 404
        with _SALES_LOCK:
            syncs = [s for s in _visible_map(user, "QuoteSync").values()
                     if s.get("IsActive")]
            if any(s.get("OpportunityId") == opp_id and s.get("QuoteId") != quote_id
                   for s in syncs):
                return jsonify({"error": "Another quote is already syncing "
                                         "to this opportunity"}), 422
            qlis = [q for q in _visible_map(user, "QuoteLineItem").values()
                    if q.get("QuoteId") == quote_id]
            olis = _visible_map(user, "OpportunityLineItem")
            synced = { _qli_key(o): o for o in olis.values()
                       if o.get("OpportunityId") == opp_id
                       and o.get("SourceQuoteId") == quote_id }
            manual = [o for o in olis.values()
                      if o.get("OpportunityId") == opp_id
                      and not o.get("SourceQuoteId")]
            created = updated = 0
            matched = set()
            for q in qlis:
                k = _qli_key(q)
                qty = _num(q.get("Quantity"), 1)
                price = _num(q.get("UnitPrice"))
                disc = _num(q.get("Discount"))
                fields = {"OpportunityId": opp_id, "SourceQuoteId": quote_id,
                          "PriceBookEntryId": q.get("PriceBookEntryId"),
                          "Quantity": qty, "UnitPrice": price, "Discount": disc,
                          "TotalPrice": _total_price(qty, price, disc)}
                target = synced.get(k)
                if target is None and k[0] == "pbe":
                    target = next((o for o in manual
                                   if o.get("PriceBookEntryId") == k[1]), None)
                if target:
                    code, data = _do_update(user, "OpportunityLineItem",
                                            target["id"], fields)
                    if code not in (200, 201):
                        return jsonify(data), code
                    updated += 1
                    matched.add(target["id"])
                else:
                    code, data = _do_create(user, "OpportunityLineItem", fields)
                    if code not in (200, 201):
                        return jsonify(data), code
                    created += 1
                    matched.add(data["Id"])
            deleted = 0
            for o in list(synced.values()):
                if o["id"] not in matched:
                    _recycle_delete(user, "OpportunityLineItem", o["id"])
                    deleted += 1
            mine = next((s for s in syncs if s.get("QuoteId") == quote_id), None)
            if mine:
                _do_update(user, "QuoteSync", mine["id"], {"IsActive": True})
            else:
                code, data = _do_create(user, "QuoteSync",
                                       {"QuoteId": quote_id,
                                        "OpportunityId": opp_id, "IsActive": True})
                if code not in (200, 201):
                    return jsonify(data), code
            _recompute_amount(user, opp_id)
        return jsonify({"synced": True, "opportunity_id": opp_id,
                        "created": created, "updated": updated,
                        "deleted": deleted})

    @app.post("/api/sales/quotes/<quote_id>/unsync")
    @require_auth
    def quote_unsync(quote_id):
        user = current_user()
        quote = _visible_map(user, "Quote").get(quote_id)
        if not quote:
            return jsonify({"error": "Quote not found"}), 404
        with _SALES_LOCK:
            syncs = [s for s in _visible_map(user, "QuoteSync").values()
                     if s.get("QuoteId") == quote_id and s.get("IsActive")]
            for s in syncs:
                _recycle_delete(user, "QuoteSync", s["id"])
            # line items stay, but are no longer tied to the quote
            for o in _visible_map(user, "OpportunityLineItem").values():
                if o.get("SourceQuoteId") == quote_id:
                    _do_update(user, "OpportunityLineItem", o["id"],
                               {"SourceQuoteId": None})
        return jsonify({"synced": False})

    @app.get("/api/sales/quotes/<quote_id>/sync-status")
    @require_auth
    def quote_sync_status(quote_id):
        user = current_user()
        quote = _visible_map(user, "Quote").get(quote_id)
        if not quote:
            return jsonify({"error": "Quote not found"}), 404
        active = [s for s in _visible_map(user, "QuoteSync").values()
                  if s.get("QuoteId") == quote_id and s.get("IsActive")]
        return jsonify({"quote_id": quote_id,
                        "is_syncing": bool(active),
                        "opportunity_id": quote.get("OpportunityId")})

    # ------------------------------------- account hierarchy
    @app.get("/api/sales/accounts/<account_id>/hierarchy")
    @require_auth
    def account_hierarchy(account_id):
        user = current_user()
        accts = _visible_map(user, "Account")
        root = accts.get(account_id)
        if not root:
            return jsonify({"error": "Account not found"}), 404
        children_of = {}
        for a in accts.values():
            p = a.get("ParentAccountId")
            if p:
                children_of.setdefault(p, []).append(a)
        for lst in children_of.values():
            lst.sort(key=lambda a: a.get("Name") or "")

        def node(a, depth, seen):
            kids = []
            if depth < 10:
                for c in children_of.get(a["id"], []):
                    if c["id"] in seen:
                        continue
                    kids.append(node(c, depth + 1, seen | {c["id"]}))
            return {"Id": a["id"], "Name": a.get("Name"), "children": kids}

        ancestors, seen, cur = [], {account_id}, root
        for _ in range(10):
            pid = cur.get("ParentAccountId")
            if not pid or pid in seen:
                break
            parent = accts.get(pid)
            if not parent:
                break
            ancestors.append({"Id": parent["id"], "Name": parent.get("Name")})
            seen.add(pid)
            cur = parent
        ancestors.reverse()
        return jsonify({"account": _ser(user, "Account", root),
                        "ancestors": ancestors,
                        "children": [node(c, 1, seen | {c["id"]})
                                     for c in children_of.get(account_id, [])
                                     if c["id"] not in seen]})

    # ------------------------------------- opportunity contact roles
    def _clear_other_primaries(user, opp_id, keep_id):
        for r in _visible_map(user, "OpportunityContactRole").values():
            if r.get("OpportunityId") == opp_id and r["id"] != keep_id \
                    and r.get("IsPrimary"):
                _do_update(user, "OpportunityContactRole", r["id"],
                           {"IsPrimary": False})

    @app.get("/api/sales/opportunity-contact-roles")
    @require_auth
    def ocr_list():
        user = current_user()
        opp_id = request.args.get("opportunity_id")
        recs = [r for r in _visible_map(user, "OpportunityContactRole").values()
                if not opp_id or r.get("OpportunityId") == opp_id]
        return jsonify([_ser(user, "OpportunityContactRole", r) for r in recs])

    @app.post("/api/sales/opportunity-contact-roles")
    @require_auth
    def ocr_create():
        user = current_user()
        body = request.json or {}
        opp_id = body.get("opportunity_id")
        if not _opp(user, opp_id):
            return jsonify({"error": "Opportunity not found"}), 404
        if not _visible_map(user, "Contact").get(body.get("contact_id") or ""):
            return jsonify({"error": "Contact not found"}), 404
        code, data = _do_create(user, "OpportunityContactRole", {
            "OpportunityId": opp_id, "ContactId": body.get("contact_id"),
            "Role": body.get("role") or "Other",
            "IsPrimary": bool(body.get("is_primary"))})
        if code not in (200, 201):
            return jsonify(data), code
        if body.get("is_primary"):
            _clear_other_primaries(user, opp_id, data["Id"])
        return jsonify(data), code

    @app.put("/api/sales/opportunity-contact-roles/<rid>")
    @require_auth
    def ocr_update(rid):
        user = current_user()
        rec = _visible_map(user, "OpportunityContactRole").get(rid)
        if not rec:
            return jsonify({"error": "Not found"}), 404
        body = request.json or {}
        patch = {}
        if "role" in body:
            patch["Role"] = body["role"]
        if "contact_id" in body:
            if not _visible_map(user, "Contact").get(body["contact_id"] or ""):
                return jsonify({"error": "Contact not found"}), 404
            patch["ContactId"] = body["contact_id"]
        if "is_primary" in body:
            patch["IsPrimary"] = bool(body["is_primary"])
        code, data = _do_update(user, "OpportunityContactRole", rid, patch)
        if code not in (200, 201):
            return jsonify(data), code
        if patch.get("IsPrimary"):
            _clear_other_primaries(user, rec["OpportunityId"], rid)
        return jsonify(data), code

    @app.delete("/api/sales/opportunity-contact-roles/<rid>")
    @require_auth
    def ocr_delete(rid):
        user = current_user()
        if rid not in _visible_map(user, "OpportunityContactRole"):
            return jsonify({"error": "Not found"}), 404
        if not security.can(user, "delete", "OpportunityContactRole"):
            return jsonify({"error": "Not found"}), 404
        _recycle_delete(user, "OpportunityContactRole", rid)
        return jsonify({"deleted": True})

    @app.get("/api/sales/opportunities/<opp_id>/contact-roles")
    @require_auth
    def opp_contact_roles(opp_id):
        user = current_user()
        if not _opp(user, opp_id):
            return jsonify({"error": "Opportunity not found"}), 404
        contacts = _visible_map(user, "Contact")
        out = []
        for r in _visible_map(user, "OpportunityContactRole").values():
            if r.get("OpportunityId") != opp_id:
                continue
            item = _ser(user, "OpportunityContactRole", r)
            c = contacts.get(r.get("ContactId") or "")
            item["contact"] = ({"Id": c["id"],
                                "Name": f"{c.get('FirstName') or ''} "
                                        f"{c.get('LastName') or ''}".strip(),
                                "Title": c.get("Title"),
                                "Email": c.get("Email")} if c else None)
            out.append(item)
        out.sort(key=lambda i: (not i.get("IsPrimary"), i.get("Id")))
        return jsonify(out)

    # ------------------------------------- opportunity team members
    @app.get("/api/sales/opportunity-team-members")
    @require_auth
    def otm_list():
        user = current_user()
        opp_id = request.args.get("opportunity_id")
        recs = [r for r in _visible_map(user, "OpportunityTeamMember").values()
                if not opp_id or r.get("OpportunityId") == opp_id]
        return jsonify([_ser(user, "OpportunityTeamMember", r) for r in recs])

    @app.post("/api/sales/opportunity-team-members")
    @require_auth
    def otm_create():
        user = current_user()
        body = request.json or {}
        opp_id = body.get("opportunity_id")
        if not _opp(user, opp_id):
            return jsonify({"error": "Opportunity not found"}), 404
        uid = body.get("user_id") or ""
        if not security.get_user(uid):
            return jsonify({"error": "User not found"}), 422
        code, data = _do_create(user, "OpportunityTeamMember", {
            "OpportunityId": opp_id, "UserId": uid,
            "TeamRole": body.get("team_role") or "Sales Rep",
            "AccessLevel": body.get("access_level") or "Read"})
        return jsonify(data), code

    @app.put("/api/sales/opportunity-team-members/<rid>")
    @require_auth
    def otm_update(rid):
        user = current_user()
        if rid not in _visible_map(user, "OpportunityTeamMember"):
            return jsonify({"error": "Not found"}), 404
        body = request.json or {}
        patch = {}
        if "team_role" in body:
            patch["TeamRole"] = body["team_role"]
        if "access_level" in body:
            patch["AccessLevel"] = body["access_level"]
        if "user_id" in body:
            if not security.get_user(body["user_id"] or ""):
                return jsonify({"error": "User not found"}), 422
            patch["UserId"] = body["user_id"]
        code, data = _do_update(user, "OpportunityTeamMember", rid, patch)
        return jsonify(data), code

    @app.delete("/api/sales/opportunity-team-members/<rid>")
    @require_auth
    def otm_delete(rid):
        user = current_user()
        rec = _visible_map(user, "OpportunityTeamMember").get(rid)
        if not rec:
            return jsonify({"error": "Not found"}), 404
        if not security.can(user, "delete", "OpportunityTeamMember"):
            return jsonify({"error": "Not found"}), 404
        linked = [s for s in _visible_map(user, "OpportunitySplit").values()
                  if s.get("TeamMemberId") == rid]
        if linked:
            return jsonify({"error": "Team member has splits; remove or "
                                     "reassign them first"}), 422
        _recycle_delete(user, "OpportunityTeamMember", rid)
        return jsonify({"deleted": True})

    @app.get("/api/sales/opportunities/<opp_id>/team")
    @require_auth
    def opp_team(opp_id):
        user = current_user()
        if not _opp(user, opp_id):
            return jsonify({"error": "Opportunity not found"}), 404
        members = [r for r in _visible_map(user, "OpportunityTeamMember").values()
                   if r.get("OpportunityId") == opp_id]
        splits = [r for r in _visible_map(user, "OpportunitySplit").values()
                  if r.get("OpportunityId") == opp_id]
        out_members = []
        for m in members:
            item = _ser(user, "OpportunityTeamMember", m)
            u = security.get_user(m.get("UserId") or "")
            item["username"] = u.get("username") if u else None
            item["splits"] = [_ser(user, "OpportunitySplit", s)
                              for s in splits if s.get("TeamMemberId") == m["id"]]
            out_members.append(item)
        totals = {}
        for s in splits:
            t = s.get("SplitType") or "Revenue"
            totals[t] = round(totals.get(t, 0.0) + _num(s.get("SplitPercentage")), 2)
        return jsonify({"members": out_members, "split_totals": totals})

    # ------------------------------------- opportunity splits
    def _split_totals(opp_id, split_type=None, exclude_id=None):
        totals = {}
        for s in _visible_map(current_user(), "OpportunitySplit").values():
            if s.get("OpportunityId") != opp_id:
                continue
            if exclude_id and s["id"] == exclude_id:
                continue
            t = s.get("SplitType") or "Revenue"
            if split_type and t != split_type:
                continue
            totals[t] = totals.get(t, 0.0) + _num(s.get("SplitPercentage"))
        return totals

    def _split_sum_err(opp_id, split_type, extra_pct, exclude_id=None):
        total = _split_totals(opp_id, split_type, exclude_id).get(split_type, 0.0)
        total += extra_pct
        if abs(total - 100.0) > 0.01:
            return (f"{split_type} splits must total 100% "
                    f"(would be {round(total, 2)}%)")
        return None

    @app.get("/api/sales/opportunity-splits")
    @require_auth
    def split_list():
        user = current_user()
        opp_id = request.args.get("opportunity_id")
        recs = [r for r in _visible_map(user, "OpportunitySplit").values()
                if not opp_id or r.get("OpportunityId") == opp_id]
        return jsonify([_ser(user, "OpportunitySplit", r) for r in recs])

    @app.post("/api/sales/opportunity-splits")
    @require_auth
    def split_create():
        user = current_user()
        body = request.json or {}
        opp_id = body.get("opportunity_id")
        if not _opp(user, opp_id):
            return jsonify({"error": "Opportunity not found"}), 404
        member = _visible_map(user, "OpportunityTeamMember").get(
            body.get("team_member_id") or "")
        if not member or member.get("OpportunityId") != opp_id:
            return jsonify({"error": "Team member not found on this opportunity"}), 422
        stype = body.get("split_type") or "Revenue"
        if stype not in _SPLIT_TYPES:
            return jsonify({"error": "split_type must be Revenue or Credit"}), 422
        pct = _num(body.get("split_percentage"))
        err = _pct_err("SplitPercentage", pct)
        if err:
            return jsonify({"error": err}), 422
        with _SALES_LOCK:
            err = _split_sum_err(opp_id, stype, pct)
            if err:
                return jsonify({"error": err}), 422
            code, data = _do_create(user, "OpportunitySplit", {
                "OpportunityId": opp_id, "TeamMemberId": member["id"],
                "SplitType": stype, "SplitPercentage": pct})
        return jsonify(data), code

    @app.put("/api/sales/opportunity-splits/<rid>")
    @require_auth
    def split_update(rid):
        user = current_user()
        rec = _visible_map(user, "OpportunitySplit").get(rid)
        if not rec:
            return jsonify({"error": "Not found"}), 404
        body = request.json or {}
        opp_id = rec["OpportunityId"]
        old_type = rec.get("SplitType") or "Revenue"
        new_type = body.get("split_type") or old_type
        if new_type not in _SPLIT_TYPES:
            return jsonify({"error": "split_type must be Revenue or Credit"}), 422
        new_pct = _num(body.get("split_percentage", rec.get("SplitPercentage")))
        err = _pct_err("SplitPercentage", new_pct)
        if err:
            return jsonify({"error": err}), 422
        member_id = body.get("team_member_id") or rec.get("TeamMemberId")
        member = _visible_map(user, "OpportunityTeamMember").get(member_id or "")
        if not member or member.get("OpportunityId") != opp_id:
            return jsonify({"error": "Team member not found on this opportunity"}), 422
        with _SALES_LOCK:
            if new_type == old_type:
                err = _split_sum_err(opp_id, new_type, new_pct, exclude_id=rid)
            else:
                # both the vacated type and the joined type must stay valid
                rest_old = _split_totals(opp_id, old_type, exclude_id=rid)
                if rest_old.get(old_type):
                    err = (f"{old_type} splits must total 100% after the move "
                           f"(would be {round(rest_old[old_type], 2)}%)")
                else:
                    err = _split_sum_err(opp_id, new_type, new_pct, exclude_id=rid)
            if err:
                return jsonify({"error": err}), 422
            code, data = _do_update(user, "OpportunitySplit", rid, {
                "TeamMemberId": member["id"], "SplitType": new_type,
                "SplitPercentage": new_pct})
        return jsonify(data), code

    @app.delete("/api/sales/opportunity-splits/<rid>")
    @require_auth
    def split_delete(rid):
        user = current_user()
        rec = _visible_map(user, "OpportunitySplit").get(rid)
        if not rec:
            return jsonify({"error": "Not found"}), 404
        if not security.can(user, "delete", "OpportunitySplit"):
            return jsonify({"error": "Not found"}), 404
        with _SALES_LOCK:
            stype = rec.get("SplitType") or "Revenue"
            rest = _split_totals(rec["OpportunityId"], stype, exclude_id=rid)
            if rest.get(stype) and abs(rest[stype] - 100.0) > 0.01:
                return jsonify({"error": f"Cannot delete: remaining {stype} "
                                         f"splits would total "
                                         f"{round(rest[stype], 2)}%, not 100%"}), 422
            _recycle_delete(user, "OpportunitySplit", rid)
        return jsonify({"deleted": True})

    @app.post("/api/sales/opportunities/<opp_id>/splits/replace")
    @require_auth
    def split_replace(opp_id):
        """Atomically replace all splits on an opportunity (for rebalancing)."""
        user = current_user()
        if not _opp(user, opp_id):
            return jsonify({"error": "Opportunity not found"}), 404
        body = request.json or {}
        items = body.get("splits") or []
        members = {m["id"]: m for m in
                   _visible_map(user, "OpportunityTeamMember").values()
                   if m.get("OpportunityId") == opp_id}
        totals: dict[str, float] = {}
        clean = []
        for it in items:
            m = members.get(it.get("team_member_id") or "")
            if not m:
                return jsonify({"error": "Team member not on this opportunity"}), 422
            stype = it.get("split_type") or "Revenue"
            if stype not in _SPLIT_TYPES:
                return jsonify({"error": "split_type must be Revenue or Credit"}), 422
            pct = _num(it.get("split_percentage"))
            err = _pct_err("SplitPercentage", pct)
            if err:
                return jsonify({"error": err}), 422
            totals[stype] = totals.get(stype, 0.0) + pct
            clean.append((m["id"], stype, pct))
        for stype, total in totals.items():
            if abs(total - 100.0) > 0.01:
                return jsonify({"error": f"{stype} splits must total 100% "
                                         f"(got {round(total, 2)}%)"}), 422
        with _SALES_LOCK:
            for s in list(_visible_map(user, "OpportunitySplit").values()):
                if s.get("OpportunityId") == opp_id:
                    _recycle_delete(user, "OpportunitySplit", s["id"])
            created = []
            for mid, stype, pct in clean:
                code, data = _do_create(user, "OpportunitySplit", {
                    "OpportunityId": opp_id, "TeamMemberId": mid,
                    "SplitType": stype, "SplitPercentage": pct})
                if code not in (200, 201):
                    return jsonify(data), code
                created.append(data["Id"])
        return jsonify({"replaced": True, "created": created}), 201
