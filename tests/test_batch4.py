"""Tests for batch 4: Chatter feed, Kanban + Path, Lead conversion,
External IDs + upsert, Web-to-Lead + auto-response rules."""
import io
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forcelet.api import create_app
from test_forcelet import login


@pytest.fixture()
def client():
    db = tempfile.mktemp(suffix=".db")
    app = create_app(db)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    os.unlink(db)


@pytest.fixture()
def app_client():
    db = tempfile.mktemp(suffix=".db")
    app = create_app(db)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, app, db
    os.unlink(db)


@pytest.fixture()
def admin(client):
    return login(client, "admin")


@pytest.fixture()
def leo(client):
    return login(client, "leo")


def _mk_lead(c, h, **kw):
    body = {"LastName": "Prospect", "Company": "ProspectCo", "Status": "New"}
    body.update(kw)
    r = c.post("/api/sobjects/Lead", headers=h, json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["Id"]


# ------------------------------------------------------------ chatter
def test_feed_post_comment_like(client, admin):
    h = admin
    r = c_post = client.post("/api/sobjects/Account", headers=h,
                             json={"Name": "ChatterCo"})
    aid = r.get_json()["Id"]
    # post on the record feed, mentioning leo
    r = client.post("/api/feed", headers=h,
                    json={"object": "Account", "record_id": aid,
                          "body": "Big deal brewing @leo"})
    assert r.status_code == 201, r.get_json()
    pid = r.get_json()["id"]
    assert r.get_json()["user_name"] == "Ava Admin"
    # record feed shows it
    r = client.get(f"/api/feed?object=Account&record_id={aid}", headers=h)
    assert r.status_code == 200
    assert [p["id"] for p in r.get_json()] == [pid]
    # mention was recorded
    store = client.application.mf_store
    leo_id = store.meta_get("mf_users",
                            [u["id"] for u in store.meta_all("mf_users")
                             if u["username"] == "leo"][0])["id"]
    assert leo_id in store.feed_mentions_of(pid)
    # comment + like round-trip
    r = client.post(f"/api/feed/{pid}/comments", headers=h, json={"body": "nice!"})
    assert r.status_code == 201
    r = client.post(f"/api/feed/{pid}/like", headers=h)
    assert r.get_json()["liked"] is True
    r = client.get(f"/api/feed?object=Account&record_id={aid}", headers=h)
    post = r.get_json()[0]
    assert post["like_count"] == 1 and post["comment_count"] == 1
    assert post["liked_by_me"] is True
    r = client.delete(f"/api/feed/{pid}/like", headers=h)
    assert r.get_json()["liked"] is False
    # validation
    r = client.post("/api/feed", headers=h, json={"body": "  "})
    assert r.status_code == 422


def test_feed_home_follows_and_mentions(client, admin, leo):
    h, lh = admin, leo
    r = client.post("/api/sobjects/Account", headers=lh, json={"Name": "FollowCo"})
    aid = r.get_json()["Id"]
    # leo follows the account; admin posts on it
    r = client.post("/api/feed/follow", headers=lh,
                    json={"object": "Account", "record_id": aid})
    assert r.status_code == 201
    r = client.post("/api/feed", headers=h,
                    json={"object": "Account", "record_id": aid,
                          "body": "update for followers"})
    assert r.status_code == 201
    # leo's home feed includes it (he follows the record); admin's includes it
    # too because he authored it — home = followed records + mentions + own posts
    r = client.get("/api/feed", headers=lh)
    assert any(p["body"] == "update for followers" for p in r.get_json())
    r = client.get("/api/feed", headers=h)
    assert any(p["body"] == "update for followers" for p in r.get_json())
    # admin mentions leo in a global post -> appears in leo's home feed
    r = client.post("/api/feed", headers=h, json={"body": "hello @leo, see this"})
    assert r.status_code == 201
    r = client.get("/api/feed", headers=lh)
    assert any(p["body"] == "hello @leo, see this" for p in r.get_json())
    # unfollow
    r = client.delete(f"/api/feed/follow?object=Account&record_id={aid}", headers=lh)
    assert r.get_json()["following"] is False


def test_feed_security(client, admin, leo):
    h = admin
    r = client.post("/api/sobjects/Account", headers=h, json={"Name": "PrivateCo"})
    aid = r.get_json()["Id"]
    # posting on a bogus record fails
    r = client.post("/api/feed", headers=h,
                    json={"object": "Account", "record_id": "nope", "body": "x"})
    assert r.status_code == 422
    # record feed for bogus record 404s
    r = client.get("/api/feed?object=Account&record_id=nope", headers=h)
    assert r.status_code == 404


# ------------------------------------------------------------ kanban + path
def test_kanban_groups_by_stage(client, admin):
    h = admin
    r = client.get("/api/kanban/Opportunity", headers=h)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["group_by"] == "Stage"
    cols = {c["value"]: len(c["records"]) for c in body["columns"]}
    assert cols.get("Proposal") == 1 and cols.get("Qualification") == 1
    # explicit group_by works too
    r = client.get("/api/kanban/Lead?group_by=Status", headers=h)
    assert r.get_json()["group_by"] == "Status"


def test_path_seeded_for_opportunity(client, admin):
    h = admin
    r = client.get("/api/paths/Opportunity", headers=h)
    assert r.status_code == 200
    path = r.get_json()
    assert path["field"] == "Stage"
    assert "Prospecting" in path["guidance"]
    # no path configured for a custom object
    client.post("/api/admin/objects", headers=h,
                json={"name": "Widget", "label": "Widget", "plural": "Widgets"})
    r = client.get("/api/paths/Widget", headers=h)
    assert r.get_json() is None


def test_path_admin_crud(client, admin):
    h = admin
    r = client.post("/api/admin/paths", headers=h,
                    json={"object": "Lead", "field": "Status",
                          "guidance": {"New": "Work it!"}})
    assert r.status_code == 201, r.get_json()
    pid = r.get_json()["id"]
    r = client.get("/api/paths/Lead", headers=h)
    assert r.get_json()["guidance"] == {"New": "Work it!"}
    r = client.delete(f"/api/admin/paths/{pid}", headers=h)
    assert r.get_json()["deleted"] is True


# ------------------------------------------------------------ lead conversion
def test_lead_convert_creates_all_three(client, admin):
    h = admin
    lid = _mk_lead(client, h, FirstName="Connie", Email="connie@prospectco.com",
                   Phone="555-0100")
    r = client.post(f"/api/sobjects/Lead/{lid}/convert", headers=h, json={})
    assert r.status_code == 201, r.get_json()
    res = r.get_json()
    assert res["lead_id"] == lid
    # account carries the company name
    r = client.get(f"/api/sobjects/Account/{res['account_id']}", headers=h)
    assert r.get_json()["Name"] == "ProspectCo"
    # contact carries the person + link
    r = client.get(f"/api/sobjects/Contact/{res['contact_id']}", headers=h)
    contact = r.get_json()
    assert contact["LastName"] == "Prospect"
    assert contact["Email"] == "connie@prospectco.com"
    assert contact["AccountId"] == res["account_id"]
    # opportunity linked to the account with a valid stage
    r = client.get(f"/api/sobjects/Opportunity/{res['opportunity_id']}", headers=h)
    opp = r.get_json()
    assert opp["AccountId"] == res["account_id"]
    assert opp["Stage"] == "Prospecting"
    # lead is marked converted and the conversion is recorded
    r = client.get(f"/api/sobjects/Lead/{lid}", headers=h)
    assert r.get_json()["Status"] == "Converted"
    r = client.get(f"/api/sobjects/Lead/{lid}/conversion", headers=h)
    assert r.get_json()["account_id"] == res["account_id"]
    # converting again is rejected
    r = client.post(f"/api/sobjects/Lead/{lid}/convert", headers=h, json={})
    assert r.status_code == 422


def test_lead_convert_options(client, admin):
    h = admin
    lid = _mk_lead(client, h)
    r = client.post(f"/api/sobjects/Lead/{lid}/convert", headers=h, json={
        "account_name": "Custom Acct",
        "opportunity_name": "Custom Opp",
        "create_opportunity": True,
    })
    assert r.status_code == 201, r.get_json()
    res = r.get_json()
    r = client.get(f"/api/sobjects/Account/{res['account_id']}", headers=h)
    assert r.get_json()["Name"] == "Custom Acct"
    r = client.get(f"/api/sobjects/Opportunity/{res['opportunity_id']}", headers=h)
    assert r.get_json()["Name"] == "Custom Opp"
    # opt out of the opportunity
    lid2 = _mk_lead(client, h)
    r = client.post(f"/api/sobjects/Lead/{lid2}/convert", headers=h,
                    json={"create_opportunity": False})
    assert r.status_code == 201
    assert r.get_json()["opportunity_id"] is None


# ------------------------------------------------------------ external ids + upsert
def _add_external_id(c, h):
    r = c.post("/api/admin/objects/Account/fields", headers=h,
               json={"name": "ERP_Id", "label": "ERP ID", "type": "Text",
                     "external_id": True})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["external_id"] is True
    assert r.get_json()["unique"] is True


def test_upsert_create_then_update(client, admin):
    h = admin
    _add_external_id(client, h)
    r = client.put("/api/sobjects/Account/upsert/ERP_Id/ABC-1", headers=h,
                   json={"Name": "Upsert Co"})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["created"] is True
    rid = r.get_json()["Id"]
    r = client.put("/api/sobjects/Account/upsert/ERP_Id/ABC-1", headers=h,
                   json={"Name": "Upsert Co Renamed"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["created"] is False
    assert r.get_json()["Id"] == rid
    r = client.get(f"/api/sobjects/Account/{rid}", headers=h)
    assert r.get_json()["Name"] == "Upsert Co Renamed"
    assert r.get_json()["ERP_Id"] == "ABC-1"


def test_upsert_rejects_non_external_field(client, admin):
    h = admin
    r = client.put("/api/sobjects/Account/upsert/Name/whatever", headers=h,
                   json={"Name": "whatever"})
    assert r.status_code == 422


def test_external_id_validation(client, admin):
    h = admin
    # encrypted + external id is rejected
    r = client.post("/api/admin/objects/Account/fields", headers=h,
                    json={"name": "SecretExt", "label": "Secret", "type": "Text",
                          "encrypted": True, "external_id": True})
    assert r.status_code == 422
    # bad type rejected
    r = client.post("/api/admin/objects/Account/fields", headers=h,
                    json={"name": "ChkExt", "label": "Chk", "type": "Checkbox",
                          "external_id": True})
    assert r.status_code == 422


def test_csv_upsert_mode(client, admin):
    h = admin
    _add_external_id(client, h)
    r = client.put("/api/sobjects/Account/upsert/ERP_Id/CSV-1", headers=h,
                   json={"Name": "Before"})
    assert r.status_code == 201
    csv_body = "ERP_Id,Name\nCSV-1,After\nCSV-2,Brand New\n"
    data = {"file": (io.BytesIO(csv_body.encode()), "leads.csv")}
    r = client.post("/api/admin/import/Account?mode=upsert&external_id_field=ERP_Id",
                    headers=h, data=data, content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["updated"] == 1
    assert r.get_json()["created"] == 1
    r = client.put("/api/sobjects/Account/upsert/ERP_Id/CSV-1", headers=h, json={})
    assert r.get_json()["Name"] == "After"
    # upsert without a valid external id field is rejected
    data = {"file": (io.BytesIO(b"Name\nx\n"), "x.csv")}
    r = client.post("/api/admin/import/Account?mode=upsert&external_id_field=Name",
                    headers=h, data=data, content_type="multipart/form-data")
    assert r.status_code == 422


# ------------------------------------------------------------ web-to-lead + auto-responses
def test_web_to_lead_form_is_public(client):
    r = client.get("/api/public/web-to-lead")
    assert r.status_code == 200
    assert "<form" in r.get_data(as_text=True)
    assert "LastName" in r.get_data(as_text=True)


def test_web_to_lead_submit_runs_pipeline(client, admin):
    h = admin
    # round-robin assignment alternates web leads between leo and maya
    r = client.post("/api/public/web-to-lead",
                    json={"FirstName": "Webby", "LastName": "Lead",
                          "Company": "WebCo", "Email": "webby@webco.com"})
    assert r.status_code == 201, r.get_json()
    lid = r.get_json()["id"]
    r = client.get(f"/api/sobjects/Lead/{lid}", headers=h)
    lead = r.get_json()
    assert lead["LeadSource"] == "Web"
    store = client.application.mf_store
    owners = {u["username"] for u in store.meta_all("mf_users")
              if u["id"] == lead["OwnerId"]}
    assert owners <= {"leo", "maya"}
    # the seeded auto-response rule fired: welcome email logged + timeline entry
    r = client.get("/api/admin/email-log", headers=h)
    logged = [e for e in r.get_json()
              if e["record_id"] == lid and e["template"] == "New lead welcome"]
    assert logged, "expected the auto-response email in the log"
    assert logged[0]["recipient"] == "webby@webco.com"
    assert "Webby" in logged[0]["subject"]
    r = client.get(f"/api/sobjects/Lead/{lid}/activities", headers=h)
    assert any(a["activity_type"] == "email" and "Auto-response" in a["subject"]
               for a in r.get_json())
    # a duplicate web submission does not 409 (public form is idempotent-friendly)
    r = client.post("/api/public/web-to-lead",
                    json={"LastName": "Lead", "Company": "WebCo",
                          "Email": "webby@webco.com"})
    assert r.status_code == 201


def test_web_to_lead_form_post(client, admin):
    h = admin
    r = client.post("/api/public/web-to-lead",
                    data={"LastName": "Formy", "Company": "FormCo"})
    assert r.status_code == 200
    assert "Thanks" in r.get_data(as_text=True)
    leads = [r2 for r2 in client.get("/api/sobjects/Lead", headers=h).get_json()
             if r2.get("LastName") == "Formy"]
    assert leads and leads[0]["LeadSource"] == "Web"


def test_auto_response_admin_crud(client, admin):
    h = admin
    tpl = client.get("/api/email-templates", headers=h).get_json()[0]
    r = client.post("/api/admin/auto-response-rules", headers=h, json={
        "name": "Case auto-reply", "object": "Case", "order": 5,
        "criteria": {}, "template_id": tpl["id"], "active": True})
    assert r.status_code == 201, r.get_json()
    rid = r.get_json()["id"]
    r = client.get("/api/admin/auto-response-rules", headers=h)
    assert any(x["id"] == rid for x in r.get_json())
    # fires on create when criteria match
    r = client.post("/api/sobjects/Case", headers=h,
                    json={"Subject": "help", "Status": "New"})
    assert r.status_code == 201
    case_id = r.get_json()["Id"]
    r = client.get("/api/admin/email-log", headers=h)
    assert any(e["template"] == tpl["name"] and e["record_id"] == case_id
               for e in r.get_json())
    r = client.delete(f"/api/admin/auto-response-rules/{rid}", headers=h)
    assert r.get_json()["deleted"] is True


# ------------------------------------------------------------ packaging covers new config
def test_package_includes_batch4_config(client, admin):
    h = admin
    r = client.get("/api/admin/packages/export", headers=h)
    assert r.status_code == 200
    pkg = r.get_json()
    assert "auto_responses" in pkg["config"]
    assert "paths" in pkg["config"]
    assert any(x["name"] == "Web lead auto-response"
               for x in pkg["config"]["auto_responses"])
    assert any(x["object"] == "Opportunity" for x in pkg["config"]["paths"])
