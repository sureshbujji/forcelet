"""Tests for platform core: duplicate rules engine, forecasting depth,
workflow email alerts, and campaign influence (engines + API)."""
import json
import os

import pytest

from helpers import login
from forcelet.api import create_app
from forcelet.api import platform_core
from forcelet import duplicate_rules, email_alerts, forecasting

FRAG_PATH = os.path.join(os.path.dirname(__file__), "..", "metadata",
                         "fragments", "platform_core_objects.json")
FRAG = json.load(open(FRAG_PATH))


def _seed_fragment(app):
    """Provision the fragment objects/fields (what the main agent's merge does)."""
    registry = app.mf_registry
    for obj in FRAG["objects"]:
        if registry.get_object(obj["name"]):
            continue
        registry.create_object(obj["name"], obj["label"], obj["plural"],
                               is_custom=True)
        for f in obj["fields"]:
            registry.add_field(obj["name"], dict(f))
    for obj_name, fields in FRAG["field_additions"].items():
        fmap = registry.field_map(registry.get_object(obj_name))
        for f in fields:
            if f["name"] not in fmap:
                registry.add_field(obj_name, dict(f))


@pytest.fixture()
def app(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    app.config["TESTING"] = True
    _seed_fragment(app)
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def h(client):
    return login(client)


@pytest.fixture()
def store(app):
    return app.mf_store


def _mk(client, h, obj, fields):
    r = client.post(f"/api/sobjects/{obj}", headers=h, json=fields)
    assert r.status_code in (200, 201), r.get_json()
    return r.get_json()["Id"]


def _mrule(store, **kw):
    base = {"Name": "R", "ObjectName": "Lead", "Fields": "Email",
            "MatchType": "Exact", "IsActive": True}
    base.update(kw)
    return store.insert("MatchingRule", base)


def _drule(store, mrule_id, **kw):
    base = {"Name": "D", "ObjectName": "Lead", "MatchingRuleId": mrule_id,
            "Action": "Block", "Message": "dup!", "IsActive": True,
            "AppliesOn": "Both"}
    base.update(kw)
    return store.insert("DuplicateRule", base)


def _lead(client, h, **kw):
    fields = {"LastName": "Doe", "Company": "Acme", "Email": "a@b.com"}
    fields.update(kw)
    return _mk(client, h, "Lead", fields)


# ------------------------------------------------------- duplicate engine
def test_exact_match_finds_duplicate(client, h, store):
    _lead(client, h, Email="sam@acme.com")
    _mrule(store)
    dups = duplicate_rules.find_duplicates(store, "Lead", {"Email": "sam@acme.com"})
    assert len(dups) == 1
    assert dups[0]["matched"] == {"Email": "sam@acme.com"}


def test_exact_no_match_on_different_value(client, h, store):
    _lead(client, h, Email="sam@acme.com")
    _mrule(store)
    assert duplicate_rules.find_duplicates(store, "Lead", {"Email": "other@x.com"}) == []


def test_exclude_id_skips_self(client, h, store):
    lid = _lead(client, h, Email="sam@acme.com")
    _mrule(store)
    assert duplicate_rules.find_duplicates(
        store, "Lead", {"Email": "sam@acme.com"}, exclude_id=lid) == []


def test_fuzzy_is_case_insensitive(client, h, store):
    _lead(client, h, Email="Sam@Acme.COM")
    _mrule(store, MatchType="Fuzzy")
    dups = duplicate_rules.find_duplicates(store, "Lead", {"Email": "sam@acme.com"})
    assert len(dups) == 1


def test_fuzzy_contains_match(client, h, store):
    _lead(client, h, Company="Acme Corporation")
    _mrule(store, Fields="Company", MatchType="Fuzzy")
    dups = duplicate_rules.find_duplicates(store, "Lead", {"Company": "acme"})
    assert len(dups) == 1


def test_rule_with_no_usable_values_never_matches(client, h, store):
    _lead(client, h, Email="sam@acme.com")
    _mrule(store, Fields="Phone")  # incoming values carry no Phone
    assert duplicate_rules.find_duplicates(store, "Lead", {"Email": "sam@acme.com"}) == []


def test_inactive_matching_rule_ignored(client, h, store):
    _lead(client, h, Email="sam@acme.com")
    _mrule(store, IsActive=False)
    assert duplicate_rules.find_duplicates(store, "Lead", {"Email": "sam@acme.com"}) == []


def test_evaluate_block_beats_warn(client, h, store):
    _lead(client, h, Email="sam@acme.com")
    mid = _mrule(store)
    _drule(store, mid, Action="Warn", Message="warn msg")
    _drule(store, mid, Action="Block", Message="block msg")
    action, message = duplicate_rules.evaluate_duplicate_rules(
        store, "Lead", "create", {"Email": "sam@acme.com"})
    assert (action, message) == ("block", "block msg")


def test_evaluate_warn_only(client, h, store):
    _lead(client, h, Email="sam@acme.com")
    _drule(store, _mrule(store), Action="Warn", Message="careful")
    assert duplicate_rules.evaluate_duplicate_rules(
        store, "Lead", "create", {"Email": "sam@acme.com"}) == ("warn", "careful")


def test_evaluate_no_match_returns_none_none(client, h, store):
    _drule(store, _mrule(store))
    assert duplicate_rules.evaluate_duplicate_rules(
        store, "Lead", "create", {"Email": "nobody@x.com"}) == (None, None)


def test_evaluate_applies_on_filters_event(client, h, store):
    lid = _lead(client, h, Email="sam@acme.com")
    _drule(store, _mrule(store), AppliesOn="Create")
    assert duplicate_rules.evaluate_duplicate_rules(
        store, "Lead", "create", {"Email": "sam@acme.com"})[0] == "block"
    assert duplicate_rules.evaluate_duplicate_rules(
        store, "Lead", "update", {"Email": "sam@acme.com"},
        exclude_id=lid) == (None, None)


def test_evaluate_inactive_duplicate_rule_ignored(client, h, store):
    _lead(client, h, Email="sam@acme.com")
    _drule(store, _mrule(store), IsActive=False)
    assert duplicate_rules.evaluate_duplicate_rules(
        store, "Lead", "create", {"Email": "sam@acme.com"}) == (None, None)


def test_evaluate_default_message(client, h, store):
    _lead(client, h, Email="sam@acme.com")
    _drule(store, _mrule(store), Message="")
    action, message = duplicate_rules.evaluate_duplicate_rules(
        store, "Lead", "create", {"Email": "sam@acme.com"})
    assert action == "block" and "Lead" in message


# ------------------------------------------------------- duplicate rule API
def test_matching_rule_crud_api(client, h):
    r = client.post("/api/platform/matching-rules", headers=h, json={
        "Name": "Lead email", "ObjectName": "Lead", "Fields": "Email",
        "MatchType": "Exact"})
    assert r.status_code == 201, r.get_json()
    rid = r.get_json()["Id"]
    r = client.get("/api/platform/matching-rules", headers=h)
    assert any(x["Id"] == rid for x in r.get_json())
    r = client.get(f"/api/platform/matching-rules/{rid}", headers=h)
    assert r.get_json()["Name"] == "Lead email"
    r = client.patch(f"/api/platform/matching-rules/{rid}", headers=h,
                     json={"MatchType": "Fuzzy"})
    assert r.get_json()["MatchType"] == "Fuzzy"
    r = client.delete(f"/api/platform/matching-rules/{rid}", headers=h)
    assert r.get_json()["deleted"] is True
    r = client.get(f"/api/platform/matching-rules/{rid}", headers=h)
    assert r.status_code == 404


def test_duplicate_rule_crud_api(client, h):
    mid = client.post("/api/platform/matching-rules", headers=h, json={
        "Name": "M", "ObjectName": "Lead", "Fields": "Email"}).get_json()["Id"]
    r = client.post("/api/platform/duplicate-rules", headers=h, json={
        "Name": "D", "ObjectName": "Lead", "MatchingRuleId": mid,
        "Action": "Block", "AppliesOn": "Both"})
    assert r.status_code == 201, r.get_json()
    r = client.get("/api/platform/duplicate-rules", headers=h)
    assert len(r.get_json()) == 1


def test_duplicates_check_endpoint(client, h):
    _lead(client, h, Email="sam@acme.com")
    mid = client.post("/api/platform/matching-rules", headers=h, json={
        "Name": "M", "ObjectName": "Lead", "Fields": "Email"}).get_json()["Id"]
    assert mid
    r = client.post("/api/platform/duplicates/check", headers=h, json={
        "object": "Lead", "values": {"Email": "sam@acme.com"}})
    assert r.status_code == 200
    assert len(r.get_json()["duplicates"]) == 1
    r = client.post("/api/platform/duplicates/check", headers=h, json={
        "object": "Nope", "values": {}})
    assert r.status_code == 404


# ------------------------------------------------------------- forecasting
def _opp(client, h, **kw):
    fields = {"Name": "Deal", "Stage": "Prospecting", "Amount": 1000}
    fields.update(kw)
    return _mk(client, h, "Opportunity", fields)


def _admin_id(app):
    return app.mf_security.get_user_by_username("admin")["id"]


def test_forecast_summary_grouping_and_attainment(client, h, app, store):
    uid = _admin_id(app)
    _opp(client, h, ForecastCategory="Commit", Amount=40000)
    _opp(client, h, ForecastCategory="Best Case", Amount=20000)
    _opp(client, h, Amount=10000)  # no category -> Pipeline
    store.insert("ForecastQuota", {"Name": "Q", "OwnerId": uid,
                                   "Period": "2026-Q4", "QuotaAmount": 100000,
                                   "ObjectName": "Opportunity"})
    s = forecasting.forecast_summary(store, uid, "2026-Q4")
    assert s["commit"] == 40000
    assert s["best_case"] == 20000
    assert s["pipeline"] == 10000
    assert s["by_category"]["Commit"] == 40000
    assert s["quota"] == 100000
    assert s["attainment_pct"] == 40.0
    assert s["open_count"] == 3


def test_forecast_summary_excludes_closed(client, h, app, store):
    uid = _admin_id(app)
    _opp(client, h, Stage="Closed Won", ForecastCategory="Commit", Amount=99999)
    s = forecasting.forecast_summary(store, uid, "2026-Q4")
    assert s["open_count"] == 0
    assert s["commit"] == 0


def test_forecast_summary_no_quota_attainment_none(client, h, app, store):
    uid = _admin_id(app)
    _opp(client, h, ForecastCategory="Commit", Amount=5000)
    s = forecasting.forecast_summary(store, uid, "2026-Q4")
    assert s["quota"] == 0
    assert s["attainment_pct"] is None


def test_forecast_summary_defensive_without_field(app, store):
    # works even if ForecastCategory was never added to Opportunity
    uid = _admin_id(app)
    rid = store.insert("Opportunity", {"Name": "D", "Stage": "Prospecting",
                                       "Amount": 7000, "owner_id": uid})
    assert rid
    s = forecasting.forecast_summary(store, uid, "2026-Q4")
    assert s["by_category"].get("Pipeline") == 7000


def test_forecast_quota_crud_and_summary_api(client, h, app):
    uid = _admin_id(app)
    r = client.post("/api/platform/forecast-quotas", headers=h, json={
        "Name": "Q4", "OwnerId": uid, "Period": "2026-Q4", "QuotaAmount": 50000})
    assert r.status_code == 201, r.get_json()
    qid = r.get_json()["Id"]
    assert r.get_json()["ObjectName"] == "Opportunity"  # fragment default
    _opp(client, h, ForecastCategory="Commit", Amount=25000)
    r = client.get(f"/api/platform/forecasts/summary?owner_id={uid}&period=2026-Q4",
                   headers=h)
    assert r.status_code == 200
    body = r.get_json()
    assert body["attainment_pct"] == 50.0
    r = client.delete(f"/api/platform/forecast-quotas/{qid}", headers=h)
    assert r.get_json()["deleted"] is True


# ---------------------------------------------------------- email alerts
def _template(store, **kw):
    base = {"name": "T", "subject": "Deal {{Name}}",
            "body": "Amount {{Record.Amount}}, owner {{Missing}}!"}
    base.update(kw)
    return store.config_put("mf_email_templates", base)


def test_render_template_placeholders():
    rec = {"Name": "Big", "Amount": 5000}
    assert email_alerts.render_template("Hi {{FirstName}}!", rec) == "Hi !"
    assert email_alerts.render_template("Deal {{Name}}", rec) == "Deal Big"
    assert email_alerts.render_template("{{Record.Amount}}", rec) == "5000"


def test_criteria_matches():
    rec = {"Stage": "Closed Won", "Amount": 5}
    assert email_alerts.criteria_matches(None, rec) is True
    assert email_alerts.criteria_matches({"Stage": "Closed Won"}, rec) is True
    assert email_alerts.criteria_matches({"Stage": "Lost"}, rec) is False
    assert email_alerts.criteria_matches('{"Stage": "Closed Won"}', rec) is True
    assert email_alerts.criteria_matches("not json", rec) is False


def test_fire_alerts_sends_and_logs(client, h, store):
    tid = _template(store)
    store.insert("EmailAlert", {"Name": "A", "ObjectName": "Opportunity",
                                "TriggerEvent": "Create",
                                "Recipients": "boss@example.com",
                                "EmailTemplateId": tid,
                                "Criteria": '{"Stage": "Closed Won"}',
                                "IsActive": True})
    rec = {"id": "o1", "Name": "Big", "Stage": "Closed Won", "Amount": 5000}
    fired = email_alerts.fire_email_alerts(store, "Opportunity", "Create", rec)
    assert len(fired) == 1
    assert fired[0]["to"] == "boss@example.com"
    assert fired[0]["subject"] == "Deal Big"
    log = store.email_log()
    assert any(e["recipient"] == "boss@example.com" and "5000" in e["body"]
               for e in log)


def test_fire_alerts_criteria_mismatch_skips(store):
    tid = _template(store)
    store.insert("EmailAlert", {"Name": "A", "ObjectName": "Opportunity",
                                "TriggerEvent": "Create",
                                "Recipients": "boss@example.com",
                                "EmailTemplateId": tid,
                                "Criteria": '{"Stage": "Closed Won"}',
                                "IsActive": True})
    rec = {"id": "o1", "Name": "Small", "Stage": "Prospecting"}
    assert email_alerts.fire_email_alerts(store, "Opportunity", "Create", rec) == []


def test_fire_alerts_inactive_and_missing_template_skipped(store):
    store.insert("EmailAlert", {"Name": "A", "ObjectName": "Opportunity",
                                "TriggerEvent": "Create",
                                "Recipients": "boss@example.com",
                                "EmailTemplateId": "nope",
                                "IsActive": False})
    rec = {"id": "o1", "Name": "Big", "Stage": "Closed Won"}
    assert email_alerts.fire_email_alerts(store, "Opportunity", "Create", rec) == []


def test_fire_alerts_resolves_user_id_recipient(store):
    tid = _template(store)

    class StubSecurity:
        def get_user(self, uid):
            return {"id": uid, "email": "teammate@example.com"} if uid == "u1" else None

    store.insert("EmailAlert", {"Name": "A", "ObjectName": "Opportunity",
                                "TriggerEvent": "Update", "Recipients": "u1, bad",
                                "EmailTemplateId": tid, "IsActive": True})
    fired = email_alerts.fire_email_alerts(store, "Opportunity", "Update",
                                           {"id": "o1", "Name": "Big"},
                                           security=StubSecurity())
    assert [f["to"] for f in fired] == ["teammate@example.com"]


def test_email_alert_crud_api(client, h, store):
    tid = _template(store)
    r = client.post("/api/platform/email-alerts", headers=h, json={
        "Name": "Won notice", "ObjectName": "Opportunity",
        "TriggerEvent": "Update", "Recipients": "boss@example.com",
        "EmailTemplateId": tid, "Criteria": '{"Stage":"Closed Won"}'})
    assert r.status_code == 201, r.get_json()
    aid = r.get_json()["Id"]
    r = client.get("/api/platform/email-alerts", headers=h)
    assert len(r.get_json()) == 1
    r = client.delete(f"/api/platform/email-alerts/{aid}", headers=h)
    assert r.get_json()["deleted"] is True


# ----------------------------------------------------- campaign influence
def _camp_setup(client, h, store):
    camp_a = _mk(client, h, "Campaign", {"Name": "Camp A"})
    camp_b = _mk(client, h, "Campaign", {"Name": "Camp B"})
    acct = _mk(client, h, "Account", {"Name": "Acme"})
    c1 = _mk(client, h, "Contact", {"LastName": "One", "AccountId": acct})
    c2 = _mk(client, h, "Contact", {"LastName": "Two", "AccountId": acct})
    return camp_a, camp_b, acct, c1, c2


def test_attribute_primary_campaign_source(client, h, app, store):
    camp_a, _, acct, _, _ = _camp_setup(client, h, store)
    opp = _mk(client, h, "Opportunity",
              {"Name": "D", "Stage": "Prospecting", "Amount": 1000,
               "AccountId": acct, "PrimaryCampaignId": camp_a})
    created = platform_core.attribute_campaign_influence(store, opp)
    assert len(created) == 1
    assert created[0]["CampaignId"] == camp_a
    assert created[0]["InfluencePercent"] == 100.0
    assert created[0]["Model"] == "Primary Campaign Source"


def test_attribute_primary_with_no_campaign_returns_empty(client, h, store):
    acct = _mk(client, h, "Account", {"Name": "Acme"})
    opp = _mk(client, h, "Opportunity",
              {"Name": "D", "Stage": "Prospecting", "AccountId": acct})
    assert platform_core.attribute_campaign_influence(store, opp) == []


def test_attribute_even_split(client, h, app, store):
    camp_a, camp_b, acct, c1, c2 = _camp_setup(client, h, store)
    store.insert("CampaignMember", {"CampaignId": camp_a, "ContactId": c1,
                                    "Status": "Responded"})
    store.insert("CampaignMember", {"CampaignId": camp_b, "ContactId": c2,
                                    "Status": "Responded"})
    opp = _mk(client, h, "Opportunity",
              {"Name": "D", "Stage": "Prospecting", "AccountId": acct})
    created = platform_core.attribute_campaign_influence(store, opp, "Even Split")
    assert len(created) == 2
    assert sorted(c["InfluencePercent"] for c in created) == [50.0, 50.0]
    assert {c["CampaignId"] for c in created} == {camp_a, camp_b}


def test_attribute_first_and_last_touch(client, h, app, store):
    camp_a, camp_b, acct, c1, c2 = _camp_setup(client, h, store)
    store.insert("CampaignMember", {"CampaignId": camp_a, "ContactId": c1,
                                    "created_date": "2026-01-01T00:00:00"})
    store.insert("CampaignMember", {"CampaignId": camp_b, "ContactId": c2,
                                    "created_date": "2026-06-01T00:00:00"})
    opp = _mk(client, h, "Opportunity",
              {"Name": "D", "Stage": "Prospecting", "AccountId": acct})
    first = platform_core.attribute_campaign_influence(store, opp, "First Touch")
    assert [c["CampaignId"] for c in first] == [camp_a]
    last = platform_core.attribute_campaign_influence(store, opp, "Last Touch")
    assert [c["CampaignId"] for c in last] == [camp_b]


def test_attribute_is_idempotent_per_model(client, h, app, store):
    camp_a, _, acct, _, _ = _camp_setup(client, h, store)
    opp = _mk(client, h, "Opportunity",
              {"Name": "D", "Stage": "Prospecting", "AccountId": acct,
               "PrimaryCampaignId": camp_a})
    platform_core.attribute_campaign_influence(store, opp)
    platform_core.attribute_campaign_influence(store, opp)
    rows = [r for r in store.query("CampaignInfluence", owner_ids=None, limit=10000)
            if r.get("OpportunityId") == opp]
    assert len(rows) == 1


def test_attribute_unknown_model_raises(store):
    with pytest.raises(ValueError):
        platform_core.attribute_campaign_influence(store, "nope", "Bogus")


def test_influence_report_api_totals_100(client, h, app, store):
    camp_a, camp_b, acct, c1, c2 = _camp_setup(client, h, store)
    store.insert("CampaignMember", {"CampaignId": camp_a, "ContactId": c1,
                                    "Status": "Responded"})
    store.insert("CampaignMember", {"CampaignId": camp_b, "ContactId": c2,
                                    "Status": "Responded"})
    opp = _mk(client, h, "Opportunity",
              {"Name": "D", "Stage": "Prospecting", "AccountId": acct})
    r = client.post("/api/platform/campaign-influence/attribute", headers=h,
                    json={"opportunity_id": opp, "model": "Even Split"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["attributed"] == 2
    r = client.get(f"/api/platform/campaign-influence/report?opportunity_id={opp}",
                   headers=h)
    assert r.status_code == 200
    models = r.get_json()["models"]
    assert models["Even Split"]["total"] == 100.0
    names = {e["campaign_name"] for e in models["Even Split"]["entries"]}
    assert names == {"Camp A", "Camp B"}


def test_campaign_influence_crud_api(client, h):
    camp = _mk(client, h, "Campaign", {"Name": "C"})
    acct = _mk(client, h, "Account", {"Name": "A"})
    opp = _mk(client, h, "Opportunity",
              {"Name": "D", "Stage": "Prospecting", "AccountId": acct})
    r = client.post("/api/platform/campaign-influence", headers=h, json={
        "CampaignId": camp, "OpportunityId": opp, "InfluencePercent": 100,
        "Model": "Primary Campaign Source"})
    assert r.status_code == 201, r.get_json()
    iid = r.get_json()["Id"]
    r = client.get("/api/platform/campaign-influence", headers=h)
    assert len(r.get_json()) == 1
    r = client.delete(f"/api/platform/campaign-influence/{iid}", headers=h)
    assert r.get_json()["deleted"] is True


# ---------------------------------------------------------------------------
# Integration: duplicate rules are enforced on the generic record API, and an
# explicit DuplicateRule takes precedence over the legacy mf_matching_rules
# block (so Action=Warn actually warns).
# ---------------------------------------------------------------------------

def _mk_dup_rule(client, h, action):
    r = client.post("/api/platform/matching-rules", headers=h,
                    json={"Name": "Rule " + action, "ObjectName": "Lead",
                          "Fields": "Email", "MatchType": "Exact"})
    assert r.status_code == 201, r.get_json()
    mrid = r.get_json()["Id"]
    r = client.post("/api/platform/duplicate-rules", headers=h,
                    json={"Name": "Dup " + action, "ObjectName": "Lead",
                          "MatchingRuleId": mrid, "Action": action,
                          "AppliesOn": "Create",
                          "Message": "Custom %s message" % action.lower()})
    assert r.status_code == 201, r.get_json()


def test_duplicate_rule_block_uses_custom_message(client, h):
    _mk_dup_rule(client, h, "Block")
    r = client.post("/api/sobjects/Lead", headers=h,
                    json={"LastName": "B1", "Email": "b@example.com",
                          "Company": "Acme"})
    assert r.status_code == 201, r.get_json()
    r = client.post("/api/sobjects/Lead", headers=h,
                    json={"LastName": "B2", "Email": "b@example.com",
                          "Company": "Acme"})
    assert r.status_code == 409, r.get_json()
    assert "Custom block message" in r.get_json().get("error", "")


def test_duplicate_rule_warn_allows_with_warning(client, h):
    _mk_dup_rule(client, h, "Warn")
    r = client.post("/api/sobjects/Lead", headers=h,
                    json={"LastName": "W1", "Email": "w@example.com",
                          "Company": "Acme"})
    assert r.status_code == 201, r.get_json()
    r = client.post("/api/sobjects/Lead", headers=h,
                    json={"LastName": "W2", "Email": "w@example.com",
                          "Company": "Acme"})
    # Warn lets the save through (legacy block is suppressed for this rule)
    # and surfaces the warning on the response.
    assert r.status_code == 201, r.get_json()
    assert "Custom warn message" in r.get_json().get("warning", "")


# ---------------------------------------------------------------------------
# Integration: workflow email alerts fire on the generic record API
# ---------------------------------------------------------------------------

def test_email_alert_fires_on_generic_create(client, h, store):
    r = client.post("/api/admin/email-templates", headers=h,
                    json={"name": "Lead ping", "subject": "New lead {{LastName}}",
                          "body": "Company {{Company}}"})
    assert r.status_code in (200, 201), r.get_json()
    tid = r.get_json().get("id")
    r = client.post("/api/platform/email-alerts", headers=h,
                    json={"Name": "Ping on lead", "ObjectName": "Lead",
                          "TriggerEvent": "Create",
                          "Recipients": "boss@example.com",
                          "EmailTemplateId": tid, "IsActive": True})
    assert r.status_code == 201, r.get_json()
    r = client.post("/api/sobjects/Lead", headers=h,
                    json={"LastName": "AlertMe", "Email": "alert@example.com",
                          "Company": "Acme"})
    assert r.status_code == 201, r.get_json()
    log = store.email_log()
    assert any(e["recipient"] == "boss@example.com"
               and "AlertMe" in e["subject"] for e in log), log
