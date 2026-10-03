"""Tests for service core: case teams, entitlement management, notes."""
from datetime import date, datetime, timedelta, timezone

import pytest

from helpers import login
from forcelet.api import create_app
from forcelet.api import service_core


def _utc_today():
    # Platform dates are UTC (service_core._today); use UTC here so the
    # suite is deterministic regardless of the container's local timezone.
    return datetime.now(timezone.utc).date()


@pytest.fixture()
def app(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def h(client):
    return login(client)


def _id(resp_json):
    return resp_json["Id"]


def _mk_team(client, h, name="Tier 2 Support"):
    r = client.post("/api/service/case-teams", headers=h, json={"Name": name})
    assert r.status_code == 201, r.get_json()
    return _id(r.get_json())


def _mk_case(client, h, subject="Broken widget"):
    r = client.post("/api/sobjects/Case", headers=h,
                    json={"Subject": subject, "Status": "New",
                          "Priority": "Medium", "Origin": "Web"})
    assert r.status_code == 201, r.get_json()
    return _id(r.get_json())


def _mk_user(client, h, username, profile="Standard User"):
    r = client.post("/api/admin/users", headers=h,
                    json={"username": username, "name": username.title(),
                          "profile": profile, "password": "forcelet"})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def _dates(delta_start, delta_end):
    today = _utc_today()
    return ((today + timedelta(days=delta_start)).isoformat(),
            (today + timedelta(days=delta_end)).isoformat())


# ------------------------------------------------------------ object registration
def test_service_core_objects_registered(app):
    names = {o["name"] for o in app.mf_registry.list_objects()}
    assert {"CaseTeamDef", "CaseTeamMemberDef", "Entitlement",
            "EntitlementProcess", "EntitlementProcessMilestone",
            "Note"} <= names
    fields = {f["name"]
              for f in app.mf_registry.get_object("Entitlement")["fields"]}
    assert {"Name", "AccountId", "AssetId", "Type", "Status", "StartDate",
            "EndDate", "CasesPerPeriod", "EntitlementProcessId"} <= fields
    note_fields = {f["name"]
                   for f in app.mf_registry.get_object("Note")["fields"]}
    assert {"Title", "Body", "ParentId", "ParentType", "OwnerId",
            "IsPrivate"} <= note_fields


# ---------------------------------------------------------------- case teams
def test_case_team_crud(client, h):
    tid = _mk_team(client, h)
    r = client.get(f"/api/service/case-teams/{tid}", headers=h)
    assert r.status_code == 200
    assert r.get_json()["team"]["Name"] == "Tier 2 Support"
    assert r.get_json()["members"] == []
    r = client.get("/api/service/case-teams", headers=h)
    assert any(t["Id"] == tid for t in r.get_json())
    r = client.put(f"/api/service/case-teams/{tid}", headers=h,
                   json={"Description": "Escalation pod"})
    assert r.status_code == 200
    # validation: name is required
    r = client.post("/api/service/case-teams", headers=h, json={})
    assert r.status_code == 422, r.get_json()
    r = client.delete(f"/api/service/case-teams/{tid}", headers=h)
    assert r.status_code == 200
    r = client.get(f"/api/service/case-teams/{tid}", headers=h)
    assert r.status_code == 404


def test_case_team_members(client, h):
    tid = _mk_team(client, h)
    uid = _mk_user(client, h, "teammate")
    r = client.post(f"/api/service/case-teams/{tid}/members", headers=h,
                    json={"UserId": uid, "TeamRole": "SME"})
    assert r.status_code == 201, r.get_json()
    mid = _id(r.get_json())
    r = client.post(f"/api/service/case-teams/{tid}/members", headers=h,
                    json={"UserId": "no-such-user"})
    assert r.status_code == 422, r.get_json()
    r = client.post(f"/api/service/case-teams/{tid}/members", headers=h,
                    json={"UserId": uid, "TeamRole": "Janitor"})
    assert r.status_code == 422, r.get_json()
    r = client.get(f"/api/service/case-teams/{tid}/members", headers=h)
    assert r.status_code == 200 and len(r.get_json()) == 1
    r = client.delete(f"/api/service/case-teams/{tid}/members/{mid}", headers=h)
    assert r.status_code == 200
    r = client.get(f"/api/service/case-teams/{tid}/members", headers=h)
    assert r.get_json() == []


def test_assign_team_to_case(client, h):
    tid = _mk_team(client, h)
    uid = _mk_user(client, h, "teammate")
    client.post(f"/api/service/case-teams/{tid}/members", headers=h,
                json={"UserId": uid, "TeamRole": "Support Agent"})
    cid = _mk_case(client, h)
    r = client.get(f"/api/service/cases/{cid}/team", headers=h)
    assert r.status_code == 404
    r = client.post(f"/api/service/cases/{cid}/assign-team", headers=h,
                    json={"team_def_id": tid})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["team"]["Name"] == "Tier 2 Support"
    assert {m["UserId"] for m in body["members"]} == {uid}
    r = client.get(f"/api/service/cases/{cid}/team", headers=h)
    assert r.status_code == 200
    assert r.get_json()["team"]["Id"] == tid
    # unknown team / unknown case
    r = client.post(f"/api/service/cases/{cid}/assign-team", headers=h,
                    json={"team_def_id": "bogus"})
    assert r.status_code == 404
    r = client.post("/api/service/cases/bogus/assign-team", headers=h,
                    json={"team_def_id": tid})
    assert r.status_code == 404
    # re-assigning another team replaces the assignment
    tid2 = _mk_team(client, h, "Tier 3 Support")
    r = client.post(f"/api/service/cases/{cid}/assign-team", headers=h,
                    json={"team_def_id": tid2})
    assert r.status_code == 200
    r = client.get(f"/api/service/cases/{cid}/team", headers=h)
    assert r.get_json()["team"]["Id"] == tid2


def test_team_member_gains_case_visibility(client, h, app):
    """A team member with no sharing access can reach the case's team endpoint
    once assigned (API-scoped visibility grant)."""
    tid = _mk_team(client, h)
    uid = _mk_user(client, h, "teammate")
    cid = _mk_case(client, h)
    h_tm = login(client, "teammate")
    # teammate cannot see admin's case at all before assignment
    r = client.get(f"/api/service/cases/{cid}/team", headers=h_tm)
    assert r.status_code == 404
    client.post(f"/api/service/case-teams/{tid}/members", headers=h,
                json={"UserId": uid})
    r = client.post(f"/api/service/cases/{cid}/assign-team", headers=h,
                    json={"team_def_id": tid})
    assert r.status_code == 200
    r = client.get(f"/api/service/cases/{cid}/team", headers=h_tm)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["team"]["Id"] == tid


# -------------------------------------------------------------- entitlements
def _mk_entitlement(client, h, name, start, end, account=None, etype="Web Support",
                    status=None, process_id=None):
    body = {"Name": name, "Type": etype, "StartDate": start, "EndDate": end}
    if account:
        body["AccountId"] = account
    if status:
        body["Status"] = status
    if process_id:
        body["EntitlementProcessId"] = process_id
    r = client.post("/api/service/entitlements", headers=h, json=body)
    assert r.status_code == 201, (name, r.get_json())
    return r.get_json()


def test_entitlement_status_auto_derives_from_dates(client, h):
    start, end = _dates(-30, 30)
    e = _mk_entitlement(client, h, "Current", start, end)
    assert e["Status"] == "Active"
    start, end = _dates(-60, -1)
    e = _mk_entitlement(client, h, "Lapsed", start, end)
    assert e["Status"] == "Expired"
    start, end = _dates(1, 60)
    e = _mk_entitlement(client, h, "Future", start, end)
    assert e["Status"] == "Draft"


def test_entitlement_date_validation(client, h):
    start, end = _dates(10, 5)  # start after end
    r = client.post("/api/service/entitlements", headers=h,
                    json={"Name": "Bad dates", "StartDate": start, "EndDate": end})
    assert r.status_code == 422, r.get_json()


def test_entitlement_no_overlapping_active_same_type(client, h, app):
    r = client.post("/api/sobjects/Account", headers=h,
                    json={"Name": "Acme"})
    assert r.status_code == 201
    aid = _id(r.get_json())
    s1, e1 = _dates(-30, 30)
    _mk_entitlement(client, h, "First", s1, e1, account=aid)
    s2, e2 = _dates(-10, 60)
    r = client.post("/api/service/entitlements", headers=h,
                    json={"Name": "Clash", "Type": "Web Support",
                          "AccountId": aid, "StartDate": s2, "EndDate": e2})
    assert r.status_code == 422, r.get_json()
    assert "conflict" in r.get_json()
    # different type is fine
    r = client.post("/api/service/entitlements", headers=h,
                    json={"Name": "Other type", "Type": "Phone Support",
                          "AccountId": aid, "StartDate": s2, "EndDate": e2})
    assert r.status_code == 201, r.get_json()
    # non-overlapping window is fine
    s3, e3 = _dates(31, 90)
    r = client.post("/api/service/entitlements", headers=h,
                    json={"Name": "Later", "Type": "Web Support",
                          "AccountId": aid, "StartDate": s3, "EndDate": e3})
    assert r.status_code == 201, r.get_json()


def test_entitlement_update_recomputes_status(client, h):
    start, end = _dates(-30, 30)
    e = _mk_entitlement(client, h, "Extend me", start, end)
    assert e["Status"] == "Active"
    past = _dates(-60, -1)[1]
    r = client.put(f"/api/service/entitlements/{e['Id']}", headers=h,
                   json={"EndDate": past})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["Status"] == "Expired"


def test_entitlement_process_crud_and_milestones(client, h):
    r = client.post("/api/service/entitlement-processes", headers=h,
                    json={"Name": "Premier SLA"})
    assert r.status_code == 201, r.get_json()
    pid = _id(r.get_json())
    for name, mins, order in [("First response", 30, 2),
                              ("Resolution", 240, 1),
                              ("Follow-up", 1440, 3)]:
        r = client.post(
            f"/api/service/entitlement-processes/{pid}/milestones", headers=h,
            json={"Name": name, "TargetMinutes": mins, "MilestoneOrder": order})
        assert r.status_code == 201, r.get_json()
    # non-positive target is rejected
    r = client.post(f"/api/service/entitlement-processes/{pid}/milestones",
                    headers=h, json={"Name": "Bad", "TargetMinutes": 0})
    assert r.status_code == 422
    r = client.get(f"/api/service/entitlement-processes/{pid}", headers=h)
    body = r.get_json()
    assert [m["Name"] for m in body["milestones"]] == \
        ["Resolution", "First response", "Follow-up"]
    r = client.get("/api/service/entitlement-processes", headers=h)
    assert any(p["Id"] == pid for p in r.get_json())


def test_apply_entitlement_stamps_milestones(client, h):
    r = client.post("/api/service/entitlement-processes", headers=h,
                    json={"Name": "Web SLA"})
    pid = _id(r.get_json())
    client.post(f"/api/service/entitlement-processes/{pid}/milestones",
                headers=h, json={"Name": "First response", "TargetMinutes": 60,
                                 "MilestoneOrder": 1})
    client.post(f"/api/service/entitlement-processes/{pid}/milestones",
                headers=h, json={"Name": "Resolution", "TargetMinutes": 480,
                                 "MilestoneOrder": 2})
    start, end = _dates(-30, 30)
    ent = _mk_entitlement(client, h, "Web Ent", start, end, process_id=pid)
    assert ent["Status"] == "Active"
    cid = _mk_case(client, h)
    r = client.post(f"/api/service/cases/{cid}/apply-entitlement", headers=h,
                    json={"entitlement_id": ent["Id"]})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["milestones_created"] == 2
    # milestones are visible through the existing SLA endpoint
    # (case creation may also stamp priority-based SLA milestones; the
    # entitlement ones carry the process policy name)
    r = client.get(f"/api/sobjects/Case/{cid}/milestones", headers=h)
    assert r.status_code == 200
    ms = [m for m in r.get_json() if m["policy"] == "Entitlement: Web SLA"]
    assert len(ms) == 2
    assert {m["name"] for m in ms} == {"First response", "Resolution"}
    # re-applying the same entitlement is idempotent
    r = client.post(f"/api/service/cases/{cid}/apply-entitlement", headers=h,
                    json={"entitlement_id": ent["Id"]})
    assert r.status_code == 200
    r = client.get(f"/api/sobjects/Case/{cid}/milestones", headers=h)
    ms = [m for m in r.get_json() if m["policy"] == "Entitlement: Web SLA"]
    assert len(ms) == 2
    # case entitlement is retrievable
    r = client.get(f"/api/service/cases/{cid}/entitlement", headers=h)
    assert r.status_code == 200
    assert r.get_json()["entitlement"]["Id"] == ent["Id"]
    # closing the case completes the stamped milestones (existing hook)
    r = client.patch(f"/api/sobjects/Case/{cid}", headers=h,
                     json={"Status": "Closed"})
    assert r.status_code == 200
    r = client.get(f"/api/sobjects/Case/{cid}/milestones", headers=h)
    assert all(m["completed_at"] for m in r.get_json())


def test_apply_entitlement_rejects_inactive(client, h):
    start, end = _dates(-60, -1)
    ent = _mk_entitlement(client, h, "Lapsed Ent", start, end)
    assert ent["Status"] == "Expired"
    cid = _mk_case(client, h)
    r = client.post(f"/api/service/cases/{cid}/apply-entitlement", headers=h,
                    json={"entitlement_id": ent["Id"]})
    assert r.status_code == 422, r.get_json()
    r = client.post(f"/api/service/cases/{cid}/apply-entitlement", headers=h,
                    json={"entitlement_id": "bogus"})
    assert r.status_code == 404


def test_account_entitlements_list(client, h):
    r = client.post("/api/sobjects/Account", headers=h, json={"Name": "Globex"})
    aid = _id(r.get_json())
    s1, e1 = _dates(-30, 30)
    ent = _mk_entitlement(client, h, "Globex Ent", s1, e1, account=aid)
    r = client.get(f"/api/service/accounts/{aid}/entitlements", headers=h)
    assert r.status_code == 200
    assert [e["Id"] for e in r.get_json()] == [ent["Id"]]
    r = client.get("/api/service/accounts/bogus/entitlements", headers=h)
    assert r.status_code == 404


# --------------------------------------------------------------------- notes
def _mk_account(client, h, name="NoteCo"):
    r = client.post("/api/sobjects/Account", headers=h, json={"Name": name})
    assert r.status_code == 201, r.get_json()
    return _id(r.get_json())


def test_note_crud_scoped_by_parent(client, h):
    aid = _mk_account(client, h)
    r = client.post("/api/service/notes", headers=h,
                    json={"Title": "Kickoff", "Body": "Met the team",
                          "ParentType": "Account", "ParentId": aid})
    assert r.status_code == 201, r.get_json()
    nid = r.get_json()["Id"]
    r = client.get(f"/api/service/notes?parent_type=Account&parent_id={aid}",
                   headers=h)
    assert r.status_code == 200 and len(r.get_json()) == 1
    r = client.get(f"/api/service/notes/{nid}", headers=h)
    assert r.status_code == 200 and r.get_json()["Title"] == "Kickoff"
    r = client.put(f"/api/service/notes/{nid}", headers=h,
                   json={"Body": "Updated body"})
    assert r.status_code == 200 and r.get_json()["Body"] == "Updated body"
    # notes on other parents do not leak in
    aid2 = _mk_account(client, h, "OtherCo")
    r = client.get(f"/api/service/notes?parent_type=Account&parent_id={aid2}",
                   headers=h)
    assert r.get_json() == []
    r = client.delete(f"/api/service/notes/{nid}", headers=h)
    assert r.status_code == 200
    r = client.get(f"/api/service/notes/{nid}", headers=h)
    assert r.status_code == 404


def test_note_parent_validation(client, h):
    aid = _mk_account(client, h)
    r = client.post("/api/service/notes", headers=h,
                    json={"Title": "x", "ParentType": "Account",
                          "ParentId": "bogus"})
    assert r.status_code == 404, r.get_json()
    r = client.post("/api/service/notes", headers=h,
                    json={"Title": "x", "ParentType": "Invoice",
                          "ParentId": aid})
    assert r.status_code == 422, r.get_json()
    r = client.post("/api/service/notes", headers=h,
                    json={"ParentType": "Account", "ParentId": aid})
    assert r.status_code == 422, r.get_json()  # Title required
    r = client.get("/api/service/notes?parent_type=Account", headers=h)
    assert r.status_code == 422


def test_private_note_visible_only_to_owner_and_admin(client, h):
    _mk_user(client, h, "sam")
    h_sam = login(client, "sam")
    # sam owns the parent so parent visibility is not the blocker
    r = client.post("/api/sobjects/Account", headers=h_sam,
                    json={"Name": "SamCo"})
    assert r.status_code == 201, r.get_json()
    aid = _id(r.get_json())
    r = client.post("/api/service/notes", headers=h,
                    json={"Title": "Private", "Body": "secret",
                          "ParentType": "Account", "ParentId": aid,
                          "IsPrivate": True})
    assert r.status_code == 201
    priv_id = r.get_json()["Id"]
    r = client.post("/api/service/notes", headers=h,
                    json={"Title": "Public", "Body": "open",
                          "ParentType": "Account", "ParentId": aid})
    assert r.status_code == 201
    # sam sees only the public note
    r = client.get(f"/api/service/notes?parent_type=Account&parent_id={aid}",
                   headers=h_sam)
    assert r.status_code == 200
    assert [n["Title"] for n in r.get_json()] == ["Public"]
    r = client.get(f"/api/service/notes/{priv_id}", headers=h_sam)
    assert r.status_code == 404
    # sam cannot edit or delete someone else's note
    r = client.put(f"/api/service/notes/{priv_id}", headers=h_sam,
                   json={"Body": "hijack"})
    assert r.status_code in (403, 404)
    r = client.delete(f"/api/service/notes/{priv_id}", headers=h_sam)
    assert r.status_code in (403, 404)
    # admin sees both
    r = client.get(f"/api/service/notes?parent_type=Account&parent_id={aid}",
                   headers=h)
    assert {n["Title"] for n in r.get_json()} == {"Private", "Public"}


def test_related_endpoint(client, h):
    aid = _mk_account(client, h)
    client.post("/api/service/notes", headers=h,
                json={"Title": "Rel", "ParentType": "Account", "ParentId": aid})
    r = client.get(f"/api/service/related?parent_type=Account&parent_id={aid}",
                   headers=h)
    assert r.status_code == 200
    body = r.get_json()
    assert [n["Title"] for n in body["notes"]] == ["Rel"]
    # case parents also surface team / entitlement / milestones
    cid = _mk_case(client, h)
    tid = _mk_team(client, h)
    client.post(f"/api/service/cases/{cid}/assign-team", headers=h,
                json={"team_def_id": tid})
    r = client.get(f"/api/service/related?parent_type=Case&parent_id={cid}",
                   headers=h)
    assert r.status_code == 200
    body = r.get_json()
    assert body["case_team"]["team"]["Id"] == tid
    assert body["entitlement"] is None
    assert isinstance(body["milestones"], list)  # seeded SLA policy may stamp some
    r = client.get("/api/service/related?parent_type=Account", headers=h)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Integration: case-team membership grants record visibility through the
# generic /api/sobjects API (not just the service-core routes).
# ---------------------------------------------------------------------------

def test_case_team_member_sees_case_via_generic_api(client, h):
    r = client.post("/api/admin/users", headers=h,
                    json={"username": "tmate1", "name": "Team Mate",
                          "profile": "Standard User", "password": "RepPass1!"})
    assert r.status_code == 201, r.get_json()
    h2 = login(client, username="tmate1", password="RepPass1!")
    u2 = client.get("/api/me", headers=h2).get_json()
    uid2 = u2.get("id") or u2.get("Id")

    r = client.post("/api/sobjects/Case", headers=h,
                    json={"Subject": "Team-visible case", "Status": "New",
                          "Origin": "Web"})
    assert r.status_code == 201, r.get_json()
    cid = r.get_json()["Id"]
    # teammate cannot see the admin-owned case via the generic API yet
    r = client.get(f"/api/sobjects/Case/{cid}", headers=h2)
    assert r.status_code in (403, 404), r.get_json()

    r = client.post("/api/service/case-teams", headers=h, json={"Name": "Tier 2"})
    assert r.status_code == 201, r.get_json()
    tid = r.get_json()["Id"]
    r = client.post(f"/api/service/case-teams/{tid}/members", headers=h,
                    json={"UserId": uid2, "Role": "Member"})
    assert r.status_code == 201, r.get_json()
    r = client.post(f"/api/service/cases/{cid}/assign-team", headers=h,
                    json={"team_def_id": tid})
    assert r.status_code in (200, 201), r.get_json()

    # now the teammate sees it through the generic record API
    r = client.get(f"/api/sobjects/Case/{cid}", headers=h2)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["Subject"] == "Team-visible case"
