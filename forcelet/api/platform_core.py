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
    _audit, _do_create, _do_update, _visible_records, current_user,
    require_auth, serialize,
)

ATTRIBUTION_MODELS = ("Primary Campaign Source", "First Touch",
                      "Last Touch", "Even Split")


# ------------------------------------------------- campaign influence engine
def attribute_campaign_influence(store, opportunity_id: str,
                                 model: str = "Primary Campaign Source",
                                 user: dict | None = None,
                                 security=None) -> list:
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
    the pair are replaced (recycled when ``user`` is given, so the audit
    trail is kept). When ``user``/``security`` are given, the opportunity
    must be visible to that user. Returns the created record dicts.
    """
    if model not in ATTRIBUTION_MODELS:
        raise ValueError(f"Unknown model '{model}'")
    opp = store.get("Opportunity", opportunity_id)
    if not opp:
        raise ValueError("Opportunity not found")
    if user is not None and security is not None \
            and not security.can_see_record(user, opp, "Opportunity"):
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

    # idempotent: replace existing influence for this (opportunity, model);
    # replaced rows go through the recycle bin (with an audit entry from the
    # endpoint) instead of a silent raw delete.
    for existing in store.query("CampaignInfluence", owner_ids=None, limit=10000):
        if existing.get("OpportunityId") == opportunity_id \
                and existing.get("Model") == model:
            if user is not None:
                store.recycle_put("CampaignInfluence", existing, user["id"])
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
        """Per-owner forecast summary for one quarterly period.

        ``period`` is a quarterly label ``YYYY-QN`` (``YYYY-MM`` is also
        accepted). Only open opportunities whose ``CloseDate`` falls inside
        the period are counted; dateless opportunities are counted in every
        period. When ``period`` is absent no date filter is applied (legacy
        behavior). ``attainment_pct`` is a percent (0-100). When a manager
        set a forecast adjustment for this owner+period, the response also
        carries ``adjusted_amount`` (the manager's number) alongside the
        unadjusted originals.
        """
        user = current_user()
        owner_id = request.args.get("owner_id") or user["id"]
        period = request.args.get("period")  # None -> no date filter
        try:
            summary = forecasting.forecast_summary(store, owner_id, period)
        except Exception as exc:  # object not provisioned yet
            return jsonify({"error": str(exc)}), 404
        return jsonify(summary)

    @app.post("/api/platform/forecasts/adjust")
    @require_auth
    def forecast_adjust():
        """Set a manager forecast adjustment for an owner+period.

        Body: ``{"owner_id", "period", "adjusted_amount", "note?"}``.
        ``period`` must be a quarterly label ``YYYY-QN``. Only a manager
        may adjust: a user with at least one direct/indirect report, i.e. a
        user in a strictly lower role in the role hierarchy (same-role peers
        don't count), or an admin. Managers may only adjust owners inside
        their own visible subtree. The adjustment is stored in the
        ``mf_forecast_adjustments`` config table and surfaced by
        ``forecast_summary`` as ``adjusted_amount`` next to the unadjusted
        numbers. Upserts: posting again for the same owner+period replaces
        the previous adjustment.
        """
        user = current_user()
        body = request.json or {}
        owner_id = (body.get("owner_id") or "").strip()
        period = (body.get("period") or "").strip() \
            or forecasting.current_period()
        amount = body.get("adjusted_amount")
        if not owner_id:
            return jsonify({"error": "owner_id is required"}), 422
        if not security.get_user(owner_id):
            return jsonify({"error": "User not found"}), 422
        if not forecasting._QUARTER_RE.match(period):
            return jsonify({"error": "period must be YYYY-QN"}), 422
        try:
            amount_f = float(amount)
        except (TypeError, ValueError):
            return jsonify({"error": "adjusted_amount must be a number"}), 422
        if amount_f < 0:
            return jsonify({"error": "adjusted_amount cannot be negative"}), 422
        targets = security.visible_owner_ids(user)
        is_admin = security.is_admin(user)
        users_by_id = {u["id"]: u for u in security.list_users()}
        own_role = user.get("role")
        # Reports are visible users in a strictly lower role: same-role
        # peers are not subordinates.
        reports = [t for t in (targets or [])
                   if t != user["id"]
                   and (users_by_id.get(t) or {}).get("role") != own_role]
        if not is_admin and not reports:
            return jsonify({"error": "Only managers can adjust forecasts"}), 403
        if not is_admin and owner_id not in (targets or []):
            return jsonify({"error": "Can only adjust forecasts of your "
                                      "direct/indirect reports"}), 403
        adjustment = forecasting.set_forecast_adjustment(
            store, owner_id, period, amount_f, user["id"],
            body.get("note"))
        _audit("forecast.adjust", "forecast-adjustment", owner_id,
               f"period={period} adjusted_amount={adjustment['adjusted_amount']}")
        return jsonify(adjustment), 200

    # -- campaign influence attribution + report -------------------------------
    @app.post("/api/platform/campaign-influence/attribute")
    @require_auth
    def influence_attribute():
        """Attribute campaign influence to an opportunity.

        Requires create access on CampaignInfluence and visibility of the
        opportunity (mirrors the parent-visibility check used for notes in
        service_core). Replaced rows go through the recycle bin + audit.
        """
        user = current_user()
        body = request.json or {}
        opp_id = body.get("opportunity_id") or ""
        model = body.get("model") or "Primary Campaign Source"
        obj, err = _obj_or_404("CampaignInfluence")
        if err:
            return err
        if not security.can(user, "create", "CampaignInfluence"):
            return jsonify({"error": "Not found"}), 404
        opp = store.get("Opportunity", opp_id)
        if not opp:
            return jsonify({"error": "Opportunity not found"}), 404
        if not security.can_see_record(user, opp, "Opportunity"):
            return jsonify({"error": "Not found"}), 404
        try:
            created = attribute_campaign_influence(store, opp_id, model,
                                                   user=user, security=security)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        _audit("attribute", "campaign-influence", opp_id,
               f"model={model}; {len(created)} record(s)")
        return jsonify({"attributed": len(created),
                        "records": [serialize(user, obj, r)
                                    for r in created]})

    @app.get("/api/platform/campaign-influence/report")
    @require_auth
    def influence_report_ep():
        """Influence report for an opportunity.

        ``period`` semantics: n/a. The opportunity must be visible to the
        caller; otherwise 404.
        """
        user = current_user()
        opp_id = request.args.get("opportunity_id") or ""
        opp = store.get("Opportunity", opp_id)
        if not opp:
            return jsonify({"error": "Opportunity not found"}), 404
        if not security.can_see_record(user, opp, "Opportunity"):
            return jsonify({"error": "Not found"}), 404
        return jsonify(influence_report(store, opp_id))
