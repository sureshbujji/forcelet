"""Platform core extensions REST API — Forcelet platform module.

Covers the "Implement all" platform batch: duplicate rules, forecasting
depth, workflow email alerts, and campaign influence.

WIRE-UP (for the main agent): this module exposes ``register(app)`` like
every other domain module. Add ``platform_core`` to the module list in
``forcelet/api/__init__.py`` (it must NOT be imported anywhere else yet).
The metadata fragment ``metadata/fragments/platform_core_objects.json``
must be merged into the registry first (create the five objects and add
the two Opportunity fields); until then every route returns 404
"Unknown object".

Hook call sites (also for the main agent):
  * duplicate rules -> ``forcelet/api/records.py`` ``create_record()`` /
    ``update_record()`` — see ``forcelet/duplicate_rules.py`` header.
  * email alerts -> after record create/update and after flow actions —
    see ``forcelet/email_alerts.py`` header.
"""
from __future__ import annotations

from flask import Flask, jsonify, request

from .. import duplicate_rules, email_alerts, forecasting
from .. import datamodel as _datamodel
from .. import automation
from ._shared import (
    _do_create, _do_update, _visible_records, current_user, require_auth,
    serialize,
)

ATTRIBUTION_MODELS = ("Primary Campaign Source", "First Touch",
                      "Last Touch", "Even Split")


# ------------------------------------------------- campaign influence engine
def attribute_campaign_influence(store, opportunity_id: str,
                                 model: str = "Primary Campaign Source") -> list:
    """Create CampaignInfluence records attributing ``opportunity_id``.

    Candidate campaigns come from (a) ``Opportunity.PrimaryCampaignId``
    (fragment field; absent on older DBs) and (b) ``CampaignMember``
    records for contacts on the opportunity's account.

    Models:
      * "Primary Campaign Source" — 100% to the primary campaign; nothing
        when no primary campaign is known.
      * "Even Split" — 100% divided equally across all candidate campaigns.
      * "First Touch" / "Last Touch" — 100% to the campaign whose member
        record has the earliest/latest ``created_date`` (proxy for touch
        order; CampaignMember carries no touch timestamp).

    Idempotent per (opportunity, model): existing influence records for
    the pair are replaced. Returns the created record dicts.
    """
    if model not in ATTRIBUTION_MODELS:
        raise ValueError(f"Unknown model '{model}'")
    opp = store.get("Opportunity", opportunity_id)
    if not opp:
        raise ValueError("Opportunity not found")

    primary_id = opp.get("PrimaryCampaignId")
    touches: list[tuple[str, str | None, str]] = []  # (campaign_id, contact_id, created)
    account_id = opp.get("AccountId")
    if account_id:
        contact_ids = {c["id"] for c in
                       store.query("Contact", owner_ids=None, limit=10000)
                       if c.get("AccountId") == account_id}
        for m in store.query("CampaignMember", owner_ids=None, limit=10000):
            if m.get("ContactId") in contact_ids and m.get("CampaignId"):
                touches.append((m["CampaignId"], m.get("ContactId"),
                                m.get("created_date") or ""))

    if model == "Primary Campaign Source":
        targets = [(primary_id, None)] if primary_id else []
    elif model == "Even Split":
        seen: list[tuple[str, str | None]] = []
        if primary_id:
            seen.append((primary_id, None))
        for cid, contact_id, _ in touches:
            if cid not in [s[0] for s in seen]:
                seen.append((cid, contact_id))
        targets = seen
    else:  # First Touch / Last Touch
        pool = ([(primary_id, None, "")] if primary_id else []) + touches
        pool.sort(key=lambda t: t[2] or "")
        pick = pool[0] if model == "First Touch" else pool[-1] if pool else None
        targets = [(pick[0], pick[1])] if pick else []

    # idempotent: replace existing influence for this (opportunity, model)
    for existing in store.query("CampaignInfluence", owner_ids=None, limit=10000):
        if existing.get("OpportunityId") == opportunity_id \
                and existing.get("Model") == model:
            store.delete("CampaignInfluence", existing["id"])

    created = []
    n = len(targets)
    for i, (campaign_id, contact_id) in enumerate(targets):
        pct = 100.0 if n == 1 else (
            round(100.0 / n, 2) if i < n - 1
            else round(100.0 - round(100.0 / n, 2) * (n - 1), 2))
        rid = store.insert("CampaignInfluence", {
            "Name": f"Influence {model}",
            "CampaignId": campaign_id,
            "OpportunityId": opportunity_id,
            "ContactId": contact_id,
            "InfluencePercent": pct,
            "Model": model,
        })
        created.append(store.get("CampaignInfluence", rid))
    return created


def influence_report(store, opportunity_id: str) -> dict:
    """Campaign names + percents for an opportunity; total = 100 per model."""
    rows = [r for r in store.query("CampaignInfluence", owner_ids=None, limit=10000)
            if r.get("OpportunityId") == opportunity_id]
    campaigns = {c["id"]: c.get("Name") for c in
                 store.query("Campaign", owner_ids=None, limit=10000)}
    by_model: dict[str, dict] = {}
    for r in rows:
        model = r.get("Model") or "Primary Campaign Source"
        entry = {"campaign_id": r.get("CampaignId"),
                 "campaign_name": campaigns.get(r.get("CampaignId"), ""),
                 "influence_percent": r.get("InfluencePercent") or 0,
                 "contact_id": r.get("ContactId")}
        by_model.setdefault(model, {"entries": [], "total": 0.0})
        by_model[model]["entries"].append(entry)
        by_model[model]["total"] += entry["influence_percent"]
    for m in by_model.values():
        m["total"] = round(m["total"], 2)
    return {"opportunity_id": opportunity_id, "models": by_model}


# ------------------------------------------------------------------- API
def register(app: Flask):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security

    def _obj_or_404(obj_name):
        obj = registry.get_object(obj_name)
        if not obj:
            return None, (jsonify({"error": "Unknown object"}), 404)
        return obj, None

    # -- generic CRUD helper ------------------------------------------------
    def _list(obj_name):
        user = current_user()
        obj, err = _obj_or_404(obj_name)
        if err:
            return err
        if not security.can(user, "read", obj_name):
            return jsonify({"error": "Unknown object or no access"}), 404
        records, _ = _visible_records(user, obj_name)
        return jsonify([serialize(user, obj, r) for r in records])

    def _create(obj_name):
        user = current_user()
        code, data = _do_create(user, obj_name, request.json or {})
        return jsonify(data), code

    def _get(obj_name, rid):
        user = current_user()
        obj, err = _obj_or_404(obj_name)
        if err:
            return err
        rec = store.get(obj_name, rid)
        if not rec or not security.can(user, "read", obj_name) \
                or not security.can_see_record(user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        return jsonify(serialize(user, obj, rec))

    def _update(obj_name, rid):
        user = current_user()
        code, data = _do_update(user, obj_name, rid, request.json or {})
        return jsonify(data), code

    def _delete(obj_name, rid):
        user = request.mf_user
        obj, err = _obj_or_404(obj_name)
        if err:
            return err
        rec = store.get(obj_name, rid)
        if not rec or not security.can(user, "delete", obj_name) \
                or not security.can_see_record(user, rec, obj_name):
            return jsonify({"error": "Not found"}), 404
        try:
            _datamodel.assert_mutable(obj)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        automation.dispatch_webhooks(store, obj_name, "delete",
                                     serialize(user, obj, rec), user)
        store.delete(obj_name, rid)
        return jsonify({"deleted": True})

    def _wire(obj_name, route):
        @app.get(f"/api/platform/{route}", endpoint=f"pc_list_{route}")
        @require_auth
        def _l(obj_name=obj_name):
            return _list(obj_name)

        @app.post(f"/api/platform/{route}", endpoint=f"pc_create_{route}")
        @require_auth
        def _c(obj_name=obj_name):
            return _create(obj_name)

        @app.get(f"/api/platform/{route}/<rid>", endpoint=f"pc_get_{route}")
        @require_auth
        def _g(rid, obj_name=obj_name):
            return _get(obj_name, rid)

        @app.patch(f"/api/platform/{route}/<rid>", endpoint=f"pc_upd_{route}")
        @require_auth
        def _u(rid, obj_name=obj_name):
            return _update(obj_name, rid)

        @app.delete(f"/api/platform/{route}/<rid>", endpoint=f"pc_del_{route}")
        @require_auth
        def _d(rid, obj_name=obj_name):
            return _delete(obj_name, rid)

    _wire("MatchingRule", "matching-rules")
    _wire("DuplicateRule", "duplicate-rules")
    _wire("ForecastQuota", "forecast-quotas")
    _wire("EmailAlert", "email-alerts")
    _wire("CampaignInfluence", "campaign-influence")

    # -- duplicates dry-run ---------------------------------------------------
    @app.post("/api/platform/duplicates/check")
    @require_auth
    def duplicates_check():
        body = request.json or {}
        obj_name = body.get("object") or ""
        if not registry.get_object(obj_name):
            return jsonify({"error": "Unknown object"}), 404
        dups = duplicate_rules.find_duplicates(
            store, obj_name, body.get("values") or {},
            exclude_id=body.get("exclude_id"))
        return jsonify({"duplicates": dups})

    # -- forecasting ----------------------------------------------------------
    @app.get("/api/platform/forecasts/summary")
    @require_auth
    def forecast_summary():
        user = current_user()
        owner_id = request.args.get("owner_id") or user["id"]
        period = request.args.get("period") or forecasting.current_period()
        try:
            summary = forecasting.forecast_summary(store, owner_id, period)
        except Exception as exc:  # object not provisioned yet
            return jsonify({"error": str(exc)}), 404
        return jsonify(summary)

    # -- campaign influence attribution + report -------------------------------
    @app.post("/api/platform/campaign-influence/attribute")
    @require_auth
    def influence_attribute():
        body = request.json or {}
        opp_id = body.get("opportunity_id") or ""
        model = body.get("model") or "Primary Campaign Source"
        obj, err = _obj_or_404("CampaignInfluence")
        if err:
            return err
        try:
            created = attribute_campaign_influence(store, opp_id, model)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        return jsonify({"attributed": len(created),
                        "records": [serialize(current_user(), obj, r)
                                    for r in created]})

    @app.get("/api/platform/campaign-influence/report")
    @require_auth
    def influence_report_ep():
        opp_id = request.args.get("opportunity_id") or ""
        if not store.get("Opportunity", opp_id):
            return jsonify({"error": "Opportunity not found"}), 404
        return jsonify(influence_report(store, opp_id))
