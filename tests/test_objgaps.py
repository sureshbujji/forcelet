"""Tests for the object-gap audit items (A1-A8, B1-B9): per-object generalization
of Notes, Calendar, Macros, Path, SLA policies, stored roll-up rules,
queues, lead conversion, web-to lead/case, KB versioning, contract guards,
campaign member statuses, task recurrence, territories, forecasting, and
milestone scoping."""
import pytest

from helpers import login
from forcelet.api import create_app


@pytest.fixture()
def app(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def _rid(resp):
    body = resp.get_json()
    assert resp.status_code in (200, 201), body
    return body.get("Id") or body.get("id")


# ------------------------------------------------------------------ A6: stored roll-up rules
def test_a6_opp_amount_rule_seeded(client):
    h = login(client)
    rules = client.get("/api/admin/rollup-rules", headers=h).get_json()
    seed = [r for r in rules if r["child_object"] == "OpportunityLineItem"
            and r["parent_object"] == "Opportunity"]
    assert seed and seed[0]["parent_field"] == "Amount" and seed[0]["func"] == "sum"


def test_a6_opp_amount_rolls_up_on_line_item_writes(client):
    h = login(client)
    oid = _rid(client.post("/api/sobjects/Opportunity", headers=h,
                           json={"Name": "RU Opp", "Stage": "Prospecting",
                                 "CloseDate": "2026-12-31"}))
    lids = []
    for tp in (100.0, 250.5):
        lids.append(_rid(client.post("/api/sobjects/OpportunityLineItem", headers=h,
                                     json={"OpportunityId": oid, "Quantity": 1,
                                           "UnitPrice": tp, "TotalPrice": tp})))
    assert client.get(f"/api/sobjects/Opportunity/{oid}", headers=h).get_json()["Amount"] == 350.5
    client.patch(f"/api/sobjects/OpportunityLineItem/{lids[0]}", headers=h,
                 json={"TotalPrice": 200.0})
    assert client.get(f"/api/sobjects/Opportunity/{oid}", headers=h).get_json()["Amount"] == 450.5
    client.delete(f"/api/sobjects/OpportunityLineItem/{lids[0]}", headers=h)
    assert client.get(f"/api/sobjects/Opportunity/{oid}", headers=h).get_json()["Amount"] == 250.5


def test_a6_custom_count_rule_and_reparent(client):
    h = login(client)
    client.post("/api/admin/objects/Account/fields", headers=h,
                json={"name": "ContactCount", "label": "Contact Count", "type": "Number"})
    r = client.post("/api/admin/rollup-rules", headers=h, json={
        "name": "Contact count", "child_object": "Contact", "parent_object": "Account",
        "link_field": "AccountId", "parent_field": "ContactCount",
        "func": "count", "active": True})
    assert r.status_code == 201, r.get_json()
    aid = _rid(client.post("/api/sobjects/Account", headers=h, json={"Name": "RU Acme"}))
    bid = _rid(client.post("/api/sobjects/Account", headers=h, json={"Name": "RU Beta"}))
    for ln in ("RU-A", "RU-B"):
        _rid(client.post("/api/sobjects/Contact", headers=h,
                         json={"LastName": ln, "AccountId": aid}))
    get = lambda i: client.get(f"/api/sobjects/Account/{i}", headers=h).get_json()
    assert get(aid)["ContactCount"] == 2
    cons = client.get("/api/sobjects/Contact", headers=h).get_json()
    cid = next(x["Id"] for x in cons if x["LastName"] == "RU-A")
    client.patch(f"/api/sobjects/Contact/{cid}", headers=h, json={"AccountId": bid})
    assert get(aid)["ContactCount"] == 1
    assert get(bid)["ContactCount"] == 1


def test_a6_rollup_rule_validation(client):
    h = login(client)
    base = {"name": "X", "child_object": "Contact", "parent_object": "Account",
            "link_field": "AccountId", "parent_field": "AnnualRevenue",
            "child_field": "Nope", "func": "sum"}
    assert client.post("/api/admin/rollup-rules", headers=h,
                       json={**base, "child_object": "NoSuch"}).status_code == 422
    assert client.post("/api/admin/rollup-rules", headers=h,
                       json={**base, "func": "median"}).status_code == 422
    assert client.post("/api/admin/rollup-rules", headers=h,
                       json={**base, "parent_field": "Nope"}).status_code == 422
    # sum without a child field is rejected
    no_cf = {k: v for k, v in base.items() if k != "child_field"}
    assert client.post("/api/admin/rollup-rules", headers=h, json=no_cf).status_code == 422
    # parent field must not be a formula/roll-up field
    assert client.post("/api/admin/rollup-rules", headers=h, json={
        "name": "Y", "child_object": "Opportunity", "parent_object": "Account",
        "link_field": "AccountId", "parent_field": "OpenOpportunityCount",
        "func": "count"}).status_code == 422


def test_a6_inactive_rule_does_not_recompute(client):
    h = login(client)
    client.post("/api/admin/objects/Account/fields", headers=h,
                json={"name": "ContactCount2", "label": "CC2", "type": "Number"})
    rid = _rid(client.post("/api/admin/rollup-rules", headers=h, json={
        "name": "CC2 rule", "child_object": "Contact", "parent_object": "Account",
        "link_field": "AccountId", "parent_field": "ContactCount2",
        "func": "count", "active": True}))
    aid = _rid(client.post("/api/sobjects/Account", headers=h, json={"Name": "RU Gamma"}))
    _rid(client.post("/api/sobjects/Contact", headers=h,
                     json={"LastName": "RU-C", "AccountId": aid}))
    assert client.get(f"/api/sobjects/Account/{aid}", headers=h).get_json()["ContactCount2"] == 1
    client.patch(f"/api/admin/rollup-rules/{rid}", headers=h, json={"active": False})
    _rid(client.post("/api/sobjects/Contact", headers=h,
                     json={"LastName": "RU-D", "AccountId": aid}))
    assert client.get(f"/api/sobjects/Account/{aid}", headers=h).get_json()["ContactCount2"] == 1


# ------------------------------------------------------------------ A7: queues per-object
def test_a7_queue_for_lead_object(client):
    h = login(client)
    r = client.post("/api/case-queues", headers=h, json={
        "name": "Hot leads", "object": "Lead",
        "filters": {"==": [{"field": "Rating"}, "Hot"]}})
    assert r.status_code == 201, r.get_json()
    qid = r.get_json()["id"]
    hot = _rid(client.post("/api/sobjects/Lead", headers=h,
                           json={"LastName": "QHot", "Rating": "Hot", "Company": "X"}))
    _rid(client.post("/api/sobjects/Lead", headers=h,
                     json={"LastName": "QCold", "Rating": "Cold", "Company": "Y"}))
    rows = client.get(f"/api/case-queues/{qid}/cases", headers=h).get_json()
    last_names = [x["LastName"] for x in rows]
    assert "QHot" in last_names and "QCold" not in last_names


def test_a7_queue_object_defaults_to_case_and_validates(client):
    h = login(client)
    r = client.post("/api/case-queues", headers=h, json={"name": "Q default"})
    assert r.status_code == 201 and r.get_json()["object"] == "Case"
    qid = r.get_json()["id"]
    assert client.post("/api/case-queues", headers=h,
                       json={"name": "Q bad", "object": "NoSuch"}).status_code == 422
    assert client.put(f"/api/case-queues/{qid}", headers=h,
                      json={"object": "NoSuch"}).status_code == 422
    r = client.put(f"/api/case-queues/{qid}", headers=h, json={"object": "Lead"})
    assert r.status_code == 200 and r.get_json()["object"] == "Lead"


# ------------------------------------------------------------------ B1: lead conversion field mapping
def test_b1_default_conversion_unchanged(client):
    h = login(client)
    lid = _rid(client.post("/api/sobjects/Lead", headers=h,
                           json={"LastName": "B1", "FirstName": "Bee",
                                 "Company": "B1Co", "Email": "b1@x.com",
                                 "Phone": "555-0001"}))
    r = client.post(f"/api/sobjects/Lead/{lid}/convert", headers=h, json={})
    assert r.status_code == 201, r.get_json()
    res = r.get_json()
    acc = client.get(f"/api/sobjects/Account/{res['account_id']}", headers=h).get_json()
    con = client.get(f"/api/sobjects/Contact/{res['contact_id']}", headers=h).get_json()
    assert acc["Name"] == "B1Co"
    assert (con["FirstName"], con["LastName"], con["Email"]) == ("Bee", "B1", "b1@x.com")
    assert client.get(f"/api/sobjects/Lead/{lid}", headers=h).get_json()["Status"] == "Converted"


def test_b1_custom_mappings_applied(client):
    h = login(client)
    for m in ({"name": "B1 phone", "lead_field": "Phone",
               "target_object": "Account", "target_field": "Phone"},
              {"name": "B1 src", "lead_field": "LeadSource",
               "target_object": "Opportunity", "target_field": "Description"}):
        r = client.post("/api/admin/lead-field-mappings", headers=h, json=m)
        assert r.status_code == 201, r.get_json()
    lid = _rid(client.post("/api/sobjects/Lead", headers=h,
                           json={"LastName": "B1m", "Company": "B1mCo",
                                 "Phone": "555-0002", "LeadSource": "Web"}))
    res = client.post(f"/api/sobjects/Lead/{lid}/convert", headers=h, json={}).get_json()
    acc = client.get(f"/api/sobjects/Account/{res['account_id']}", headers=h).get_json()
    opp = client.get(f"/api/sobjects/Opportunity/{res['opportunity_id']}", headers=h).get_json()
    assert acc["Phone"] == "555-0002"
    assert opp["Description"] == "Web"


def test_b1_mapping_validation(client):
    h = login(client)
    base = {"name": "B1v", "lead_field": "Phone",
            "target_object": "Account", "target_field": "Phone"}
    assert client.post("/api/admin/lead-field-mappings", headers=h,
                       json={**base, "lead_field": "Nope"}).status_code == 422
    assert client.post("/api/admin/lead-field-mappings", headers=h,
                       json={**base, "target_object": "Nope"}).status_code == 422
    assert client.post("/api/admin/lead-field-mappings", headers=h,
                       json={**base, "target_field": "Nope"}).status_code == 422
    # formula/roll-up targets rejected
    assert client.post("/api/admin/lead-field-mappings", headers=h, json={
        "name": "B1f", "lead_field": "Phone", "target_object": "Account",
        "target_field": "OpenOpportunityCount"}).status_code == 422


# ------------------------------------------------------------------ B2: web-to field lists per-object
def test_b2_forms_seeded_and_drive_public_endpoints(client):
    h = login(client)
    forms = client.get("/api/admin/web-to-forms", headers=h).get_json()
    by_key = {f["key"]: f for f in forms}
    assert by_key["web-to-lead"]["object"] == "Lead"
    assert by_key["web-to-case"]["object"] == "Case"
    html = client.get("/api/public/web-to-lead").data.decode()
    assert 'name="Rating"' in html and 'name="Company" required' in html
    r = client.post("/api/public/web-to-lead",
                    json={"LastName": "B2", "Company": "B2Co", "Rating": "Hot"})
    assert r.status_code == 201, r.get_json()
    lead = client.get(f"/api/sobjects/Lead/{r.get_json()['id']}", headers=h).get_json()
    assert (lead["Rating"], lead["LeadSource"]) == ("Hot", "Web")


def test_b2_field_list_edit_changes_form_and_submission(client):
    h = login(client)
    forms = client.get("/api/admin/web-to-forms", headers=h).get_json()
    fid = next(f["id"] for f in forms if f["key"] == "web-to-lead")
    r = client.patch(f"/api/admin/web-to-forms/{fid}", headers=h,
                     json={"fields": ["FirstName", "LastName", "Company"]})
    assert r.status_code == 200, r.get_json()
    html = client.get("/api/public/web-to-lead").data.decode()
    assert 'name="Rating"' not in html
    r = client.post("/api/public/web-to-lead",
                    json={"LastName": "B2b", "Company": "B2bCo", "Rating": "Hot"})
    lead = client.get(f"/api/sobjects/Lead/{r.get_json()['id']}", headers=h).get_json()
    assert lead.get("Rating") is None and lead["LeadSource"] == "Web"


def test_b2_form_validation(client):
    h = login(client)
    base = {"key": "b2x", "name": "B2 X", "object": "Lead", "fields": ["LastName"]}
    assert client.post("/api/admin/web-to-forms", headers=h, json=base).status_code == 201
    dup = dict(base, name="B2 dup")
    assert client.post("/api/admin/web-to-forms", headers=h, json=dup).status_code == 422
    assert client.post("/api/admin/web-to-forms", headers=h,
                       json={**base, "key": "b2y",
                             "fields": ["Nope"]}).status_code == 422
    assert client.post("/api/admin/web-to-forms", headers=h,
                       json={**base, "key": "b2z",
                             "object": "Nope"}).status_code == 422
    assert client.post("/api/admin/web-to-forms", headers=h,
                       json={**base, "key": "b2w",
                             "required": ["Nope"]}).status_code == 422


def test_b2_web_to_case_defaults_kept(client):
    h = login(client)
    r = client.post("/api/public/web-to-case",
                    json={"LastName": "B2c", "Subject": "Help me"})
    assert r.status_code == 201, r.get_json()
    case = client.get(f"/api/sobjects/Case/{r.get_json()['id']}", headers=h).get_json()
    assert (case["Origin"], case["Priority"]) == ("Web", "Medium")


# ------------------------------------------------------------------ B3: KB versioning
def _b3_article(client, h, title="B3 Reset"):
    return _rid(client.post("/api/sobjects/KnowledgeArticle", headers=h, json={
        "Title": title, "Summary": "How to", "Body": "Step 1",
        "Status": "Draft", "Category": "How-To"}))


def test_b3_updates_snapshot_versions(client):
    h = login(client)
    aid = _b3_article(client, h)
    client.patch(f"/api/sobjects/KnowledgeArticle/{aid}", headers=h,
                 json={"Body": "Step 1, step 2"})
    client.patch(f"/api/sobjects/KnowledgeArticle/{aid}", headers=h,
                 json={"Status": "Published"})
    vs = client.get(f"/api/kb/articles/{aid}/versions", headers=h).get_json()
    assert [v["version"] for v in vs] == [2, 1]
    v1 = client.get(f"/api/kb/articles/{aid}/versions/1", headers=h).get_json()
    assert v1["body"] == "Step 1" and v1["status"] == "Draft"


def test_b3_viewcount_only_update_skips_version(client):
    h = login(client)
    aid = _b3_article(client, h)
    client.patch(f"/api/sobjects/KnowledgeArticle/{aid}", headers=h,
                 json={"Body": "changed"})
    n1 = len(client.get(f"/api/kb/articles/{aid}/versions", headers=h).get_json())
    client.patch(f"/api/sobjects/KnowledgeArticle/{aid}", headers=h,
                 json={"ViewCount": 42})
    n2 = len(client.get(f"/api/kb/articles/{aid}/versions", headers=h).get_json())
    assert (n1, n2) == (1, 1)


def test_b3_restore_reverts_and_reversions(client):
    h = login(client)
    aid = _b3_article(client, h)
    client.patch(f"/api/sobjects/KnowledgeArticle/{aid}", headers=h,
                 json={"Body": "v2 body", "Status": "Published"})
    r = client.post(f"/api/kb/articles/{aid}/versions/1/restore", headers=h)
    assert r.status_code == 200, r.get_json()
    art = client.get(f"/api/sobjects/KnowledgeArticle/{aid}", headers=h).get_json()
    assert (art["Body"], art["Status"]) == ("Step 1", "Draft")
    vs = client.get(f"/api/kb/articles/{aid}/versions", headers=h).get_json()
    assert len(vs) == 2  # pre-restore state saved as v2
    assert client.post(f"/api/kb/articles/{aid}/versions/99/restore",
                       headers=h).status_code == 404


# ------------------------------------------------------------------ B4: contract lifecycle guards
def _b4_contract(client, h, **kw):
    base = {"AccountId": _rid(client.post("/api/sobjects/Account", headers=h,
                                          json={"Name": "B4Co"})),
            "Status": "Draft"}
    base.update(kw)
    return _rid(client.post("/api/sobjects/Contract", headers=h, json=base))


def test_b4_status_transitions_guarded(client):
    h = login(client)
    cid = _b4_contract(client, h, StartDate="2026-01-01")
    r = client.patch(f"/api/sobjects/Contract/{cid}", headers=h,
                     json={"Status": "Expired"})
    assert r.status_code == 422
    assert client.patch(f"/api/sobjects/Contract/{cid}", headers=h,
                        json={"Status": "Activated"}).status_code == 200
    assert client.patch(f"/api/sobjects/Contract/{cid}", headers=h,
                        json={"Status": "Expired"}).status_code == 200
    r = client.patch(f"/api/sobjects/Contract/{cid}", headers=h,
                     json={"Status": "Draft"})
    assert r.status_code == 422  # terminal state


def test_b4_activation_requires_start_date(client):
    h = login(client)
    cid = _b4_contract(client, h)
    r = client.patch(f"/api/sobjects/Contract/{cid}", headers=h,
                     json={"Status": "Activated"})
    assert r.status_code == 422
    r = client.patch(f"/api/sobjects/Contract/{cid}", headers=h,
                     json={"Status": "Activated", "StartDate": "2026-02-01"})
    assert r.status_code == 200


def test_b4_term_fields_locked_after_draft(client):
    h = login(client)
    cid = _b4_contract(client, h, StartDate="2026-01-01", EndDate="2026-12-31")
    client.patch(f"/api/sobjects/Contract/{cid}", headers=h,
                 json={"Status": "Activated"})
    r = client.patch(f"/api/sobjects/Contract/{cid}", headers=h,
                     json={"EndDate": "2027-12-31"})
    assert r.status_code == 422
    # non-term fields stay editable
    assert client.patch(f"/api/sobjects/Contract/{cid}", headers=h,
                        json={"Description": "ok"}).status_code == 200


def test_b4_only_draft_deletable(client):
    h = login(client)
    cid = _b4_contract(client, h, StartDate="2026-01-01")
    assert client.delete(f"/api/sobjects/Contract/{cid}", headers=h).status_code == 200
    cid = _b4_contract(client, h, StartDate="2026-01-01")
    client.patch(f"/api/sobjects/Contract/{cid}", headers=h,
                 json={"Status": "Activated"})
    assert client.delete(f"/api/sobjects/Contract/{cid}", headers=h).status_code == 422


# ------------------------------------------------------------------ B5: campaign member statuses per campaign
def _b5_setup(client, h):
    cid = _rid(client.post("/api/sobjects/Campaign", headers=h,
                           json={"Name": "B5Conf"}))
    lid = _rid(client.post("/api/sobjects/Lead", headers=h,
                           json={"LastName": "B5", "Company": "B5Co"}))
    return cid, lid


def test_b5_defaults_and_validation(client):
    h = login(client)
    cid, lid = _b5_setup(client, h)
    names = [x["name"] for x in
             client.get(f"/api/campaigns/{cid}/member-statuses", headers=h).get_json()]
    assert names == ["Sent", "Responded"]
    assert client.post("/api/sobjects/CampaignMember", headers=h,
                       json={"CampaignId": cid, "LeadId": lid,
                             "Status": "Sent"}).status_code == 201
    r = client.post("/api/sobjects/CampaignMember", headers=h,
                    json={"CampaignId": cid, "LeadId": lid, "Status": "Bogus"})
    assert r.status_code == 422


def test_b5_custom_status_materializes_defaults(client):
    h = login(client)
    cid, lid = _b5_setup(client, h)
    r = client.post(f"/api/campaigns/{cid}/member-statuses", headers=h,
                    json={"name": "Registered"})
    assert r.status_code == 201 and r.get_json()["name"] == "Registered"
    names = [x["name"] for x in
             client.get(f"/api/campaigns/{cid}/member-statuses", headers=h).get_json()]
    assert names == ["Sent", "Responded", "Registered"]
    assert client.post("/api/sobjects/CampaignMember", headers=h,
                       json={"CampaignId": cid, "LeadId": lid,
                             "Status": "Registered"}).status_code == 201
    # duplicate (case-insensitive) rejected
    assert client.post(f"/api/campaigns/{cid}/member-statuses", headers=h,
                       json={"name": "sent"}).status_code == 422
    # per-campaign isolation
    cid2 = _rid(client.post("/api/sobjects/Campaign", headers=h,
                            json={"Name": "B5Other"}))
    r = client.post("/api/sobjects/CampaignMember", headers=h,
                    json={"CampaignId": cid2, "LeadId": lid, "Status": "Registered"})
    assert r.status_code == 422


def test_b5_deactivate_and_delete_guards(client):
    h = login(client)
    cid, lid = _b5_setup(client, h)
    mid = _rid(client.post("/api/sobjects/CampaignMember", headers=h,
                           json={"CampaignId": cid, "LeadId": lid, "Status": "Sent"}))
    client.post(f"/api/campaigns/{cid}/member-statuses", headers=h,
                json={"name": "Registered"})
    rows = client.get(f"/api/campaigns/{cid}/member-statuses", headers=h).get_json()
    sid = next(x["id"] for x in rows if x["name"] == "Responded")
    assert client.patch(f"/api/campaigns/{cid}/member-statuses/{sid}", headers=h,
                        json={"active": False}).status_code == 200
    r = client.post("/api/sobjects/CampaignMember", headers=h,
                    json={"CampaignId": cid, "LeadId": lid, "Status": "Responded"})
    assert r.status_code == 422
    # update to an invalid status rejected
    r = client.patch(f"/api/sobjects/CampaignMember/{mid}", headers=h,
                     json={"Status": "Bogus"})
    assert r.status_code == 422
    # in-use status cannot be deleted
    sid2 = next(x["id"] for x in rows if x["name"] == "Sent")
    r = client.delete(f"/api/campaigns/{cid}/member-statuses/{sid2}", headers=h)
    assert r.status_code == 422
    # unused status can be deleted
    sid3 = next(x["id"] for x in rows if x["name"] == "Registered")
    assert client.delete(f"/api/campaigns/{cid}/member-statuses/{sid3}",
                         headers=h).status_code == 200


# ------------------------------------------------------------------ B6: task recurrence
def _b6_tasks(client, h, subject):
    return [t for t in client.get("/api/sobjects/Task", headers=h).get_json()
            if t.get("Subject") == subject]


def test_b6_recurrence_validation(client):
    h = login(client)
    r = client.post("/api/sobjects/Task", headers=h,
                    json={"Subject": "B6v", "IsRecurring": True})
    assert r.status_code == 422
    r = client.post("/api/sobjects/Task", headers=h,
                    json={"Subject": "B6v", "IsRecurring": True,
                          "RecurrenceType": "Daily", "RecurrenceInterval": 0})
    assert r.status_code == 422


def test_b6_completion_spawns_next_occurrence(client):
    h = login(client)
    tid = _rid(client.post("/api/sobjects/Task", headers=h, json={
        "Subject": "B6w", "DueDate": "2026-09-30", "IsRecurring": True,
        "RecurrenceType": "Weekly", "RecurrenceCount": 3}))
    assert client.patch(f"/api/sobjects/Task/{tid}", headers=h,
                        json={"Status": "Completed"}).status_code == 200
    occs = sorted((t["OccurrenceNumber"], t["DueDate"], t["Status"])
                  for t in _b6_tasks(client, h, "B6w"))
    assert occs == [(1.0, "2026-09-30", "Completed"),
                    (2.0, "2026-10-07", "Not Started")]
    t2 = next(t for t in _b6_tasks(client, h, "B6w") if t["OccurrenceNumber"] == 2)
    client.patch(f"/api/sobjects/Task/{t2['Id']}", headers=h,
                 json={"Status": "Completed"})
    assert len(_b6_tasks(client, h, "B6w")) == 3
    t3 = next(t for t in _b6_tasks(client, h, "B6w") if t["OccurrenceNumber"] == 3)
    client.patch(f"/api/sobjects/Task/{t3['Id']}", headers=h,
                 json={"Status": "Completed"})
    assert len(_b6_tasks(client, h, "B6w")) == 3  # count exhausted


def test_b6_end_date_stops_series(client):
    h = login(client)
    tid = _rid(client.post("/api/sobjects/Task", headers=h, json={
        "Subject": "B6e", "DueDate": "2026-09-30", "IsRecurring": True,
        "RecurrenceType": "Daily", "RecurrenceEndDate": "2026-09-30"}))
    client.patch(f"/api/sobjects/Task/{tid}", headers=h,
                 json={"Status": "Completed"})
    assert len(_b6_tasks(client, h, "B6e")) == 1


# ------------------------------------------------------------------ B7: territories per-object
def test_b7_rule_targets_any_object(client):
    h = login(client)
    tid = client.post("/api/admin/territories", headers=h,
                      json={"name": "West"}).get_json()["id"]
    r = client.post("/api/admin/territory-rules", headers=h, json={
        "name": "B7 hot leads", "object": "Lead", "territory_id": tid,
        "criteria": {"==": [{"field": "Rating"}, "Hot"]}})
    assert r.status_code == 201 and r.get_json()["object"] == "Lead"
    # unknown object rejected
    r = client.post("/api/admin/territory-rules", headers=h, json={
        "name": "B7 bad", "object": "Nope", "territory_id": tid})
    assert r.status_code == 422
    # omitted object keeps legacy Account default
    r = client.post("/api/admin/territory-rules", headers=h, json={
        "name": "B7 legacy", "territory_id": tid, "criteria": {}})
    assert r.get_json()["object"] == "Account"
    hot = _rid(client.post("/api/sobjects/Lead", headers=h,
                           json={"LastName": "B7", "Company": "B7Co", "Rating": "Hot"}))
    cold = _rid(client.post("/api/sobjects/Lead", headers=h,
                            json={"LastName": "B7b", "Company": "B7Co", "Rating": "Cold"}))
    run = client.post("/api/admin/territory-rules/run", headers=h).get_json()
    assert run["records_assigned"] >= 1 and run["accounts_assigned"] == run["records_assigned"]
    terrs = client.get(f"/api/sobjects/Lead/{hot}/territories", headers=h).get_json()
    assert [t["name"] for t in terrs] == ["West"]
    assert client.get(f"/api/sobjects/Lead/{cold}/territories", headers=h).get_json() == []


def test_b7_territory_sharing_covers_any_object(client):
    h = login(client)
    tid = client.post("/api/admin/territories", headers=h,
                      json={"name": "West"}).get_json()["id"]
    client.post("/api/admin/territory-rules", headers=h, json={
        "name": "B7 ca leads", "object": "Lead", "territory_id": tid,
        "criteria": {"==": [{"field": "Rating"}, "Hot"]}})
    hot = _rid(client.post("/api/sobjects/Lead", headers=h,
                           json={"LastName": "B7s", "Company": "B7Co", "Rating": "Hot"}))
    cold = _rid(client.post("/api/sobjects/Lead", headers=h,
                            json={"LastName": "B7s2", "Company": "B7Co", "Rating": "Cold"}))
    client.post("/api/admin/territory-rules/run", headers=h)
    r = client.post("/api/admin/users", headers=h, json={
        "username": "b7rep", "name": "B7 Rep", "profile": "Standard User",
        "role": "Sales Rep", "password": "RepPass1!"})
    uid = r.get_json()["id"]
    client.post(f"/api/admin/territories/{tid}/users", headers=h,
                json={"user_id": uid})
    h2 = login(client, "b7rep", "RepPass1!")
    assert client.get(f"/api/sobjects/Lead/{hot}", headers=h2).status_code == 200
    assert client.get(f"/api/sobjects/Lead/{cold}", headers=h2).status_code == 404


# ------------------------------------------------------------------ B8: forecasting per-object
def _b8_type(client, h, name="B8 services"):
    r = client.post("/api/admin/forecast-types", headers=h, json={
        "name": name, "object": "WorkOrder", "amount_field": "DurationMinutes",
        "date_field": "CompletedDate", "category_field": "Status",
        "won_values": ["Completed"], "lost_values": ["Cancelled"]})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def _b8_admin_row(client, h, period, type_id):
    r = client.get(f"/api/forecasts?period={period}&type={type_id}", headers=h)
    assert r.status_code == 200
    return next(x for x in r.get_json()["rows"] if x["user_name"] == "Ava Admin")


def test_b8_builtin_forecast_unchanged(client):
    h = login(client)
    types = client.get("/api/admin/forecast-types", headers=h).get_json()
    assert types[0]["id"] == "builtin" and types[0]["object"] == "Opportunity"
    r = client.get("/api/forecasts?period=2026-09", headers=h)
    assert r.status_code == 200
    assert r.get_json()["forecast_type"]["id"] == "builtin"
    assert client.get("/api/forecasts?period=2026-09&type=nope",
                      headers=h).status_code == 422


def test_b8_custom_type_forecast_math(client):
    h = login(client)
    ftid = _b8_type(client, h)
    wo = lambda n, st, dt, mins: client.post(  # noqa: E731
        "/api/sobjects/WorkOrder", headers=h,
        json={"Name": n, "Status": st, "CompletedDate": dt, "DurationMinutes": mins})
    assert wo("WO1", "Completed", "2026-09-10", 120).status_code == 201
    assert wo("WO2", "In Progress", "2026-09-12", 60).status_code == 201
    assert wo("WO3", "Cancelled", "2026-09-14", 999).status_code == 201
    assert wo("WO4", "Completed", "2026-08-10", 500).status_code == 201
    row = _b8_admin_row(client, h, "2026-09", ftid)
    # 120 won at full + 60 open at full (no probability field); lost + other-month excluded
    assert (row["closed_amount"], row["weighted_pipeline"], row["forecast"]) == \
        (120.0, 60.0, 180.0)


def test_b8_type_validation_and_quotas_scoped(client):
    h = login(client)
    bad = {"name": "B8 bad", "object": "WorkOrder", "amount_field": "Name",
           "date_field": "CompletedDate", "category_field": "Status",
           "won_values": ["Completed"], "lost_values": ["Cancelled"]}
    assert client.post("/api/admin/forecast-types", headers=h,
                       json=bad).status_code == 422
    assert client.post("/api/admin/forecast-types", headers=h,
                       json={**bad, "amount_field": "DurationMinutes",
                             "object": "Nope"}).status_code == 422
    ftid = _b8_type(client, h, "B8 q")
    assert client.post("/api/admin/forecast-types", headers=h, json={
        "name": "B8 q", "object": "WorkOrder", "amount_field": "DurationMinutes",
        "date_field": "CompletedDate", "category_field": "Status",
        "won_values": ["Completed"], "lost_values": ["Cancelled"]}).status_code == 422
    uid = next(u["id"] for u in client.get("/api/admin/users", headers=h).get_json()
               if u["username"] == "admin")
    r = client.post("/api/admin/forecast-quotas", headers=h,
                    json={"user_id": uid, "period": "2026-09", "quota": 100,
                          "forecast_type_id": ftid})
    assert r.status_code == 201
    row = _b8_admin_row(client, h, "2026-09", ftid)
    assert (row["quota"], row["attainment"]) == (100.0, 0.0)
    # builtin forecast does not see the typed quota
    r = client.get("/api/forecasts?period=2026-09", headers=h)
    admin_row = next(x for x in r.get_json()["rows"] if x["user_name"] == "Ava Admin")
    assert admin_row["quota"] is None
    assert client.delete(f"/api/admin/forecast-types/{ftid}",
                         headers=h).status_code == 200
