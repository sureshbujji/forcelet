"""Tests for approval email notifications: submit -> approver emails,
approve/reject -> submitter email, multi-step advance, custom templates,
and fire-and-forget behavior when addresses are missing."""
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
def app_store(client):
    # reach the store through any request context-free handle
    return client.application.mf_store


@pytest.fixture()
def admin(client):
    return login(client, "admin")


@pytest.fixture()
def leo(client):
    return login(client, "leo")


@pytest.fixture()
def maya(client):
    return login(client, "maya")


def _uid(client, h):
    return client.get("/api/me", headers=h).get_json()["id"]


def _set_email(client, admin, h, email):
    uid = _uid(client, h)
    r = client.put(f"/api/admin/users/{uid}", headers=admin, json={"email": email})
    assert r.status_code == 200, r.get_json()
    return uid


def _mk_process(client, admin, name, steps, obj="Opportunity"):
    r = client.post("/api/admin/approval-processes", headers=admin, json={
        "name": name, "object": obj, "active": True, "steps": steps})
    assert r.status_code in (200, 201), r.get_json()
    return r.get_json()


def _opp(client, admin):
    r = client.post("/api/sobjects/Opportunity", headers=admin, json={
        "Name": "EmailAppr", "Stage": "Proposal", "CloseDate": "2026-12-01"})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["Id"]


def _submit(client, admin, proc, comment=""):
    rid = _opp(client, admin)
    _mk_process(client, admin, proc[0], proc[1])
    r = client.post(f"/api/sobjects/Opportunity/{rid}/submit-approval",
                    headers=admin, json={"comment": comment} if comment else {})
    assert r.status_code in (200, 201), r.get_json()
    return r.get_json()


def _emails_to(app_store, recipient):
    return [e for e in app_store.email_log(limit=500)
            if recipient in (e.get("recipient") or "")]


def test_submit_emails_approver(client, app_store, admin, leo):
    leo_id = _set_email(client, admin, leo, "leo@example.com")
    req = _submit(client, admin,
                  ("Email one-step", [{"name": "S1", "approver": {"type": "user", "id": leo_id}}]),
                  comment="Please expedite")
    mails = _emails_to(app_store, "leo@example.com")
    assert len(mails) == 1, app_store.email_log(limit=500)
    m = mails[0]
    assert "Email one-step" in m["subject"]
    assert "Please expedite" in m["body"]
    assert req["id"] in m["body"]  # request reference included


def test_approve_emails_submitter(client, app_store, admin, leo):
    leo_id = _set_email(client, admin, leo, "leo@example.com")
    _set_email(client, admin, admin, "admin@example.com")
    req = _submit(client, admin,
                  ("Email decision", [{"name": "S1", "approver": {"type": "user", "id": leo_id}}]))
    r = client.post(f"/api/approvals/{req['id']}/approve", headers=leo,
                    json={"comment": "Looks good"})
    assert r.status_code == 200, r.get_json()
    mails = _emails_to(app_store, "admin@example.com")
    assert len(mails) == 1, app_store.email_log(limit=500)
    assert "Approved" in mails[0]["subject"]
    assert "Looks good" in mails[0]["body"]


def test_reject_emails_submitter(client, app_store, admin, leo):
    leo_id = _set_email(client, admin, leo, "leo@example.com")
    _set_email(client, admin, admin, "admin@example.com")
    req = _submit(client, admin,
                  ("Email reject", [{"name": "S1", "approver": {"type": "user", "id": leo_id}}]))
    r = client.post(f"/api/approvals/{req['id']}/reject", headers=leo,
                    json={"comment": "Missing discount info"})
    assert r.status_code == 200, r.get_json()
    mails = _emails_to(app_store, "admin@example.com")
    assert len(mails) == 1, app_store.email_log(limit=500)
    assert "Rejected" in mails[0]["subject"]
    assert "Missing discount info" in mails[0]["body"]


def test_multistep_advance_emails_next_approver(client, app_store, admin, leo, maya):
    leo_id = _set_email(client, admin, leo, "leo@example.com")
    maya_id = _set_email(client, admin, maya, "maya@example.com")
    req = _submit(client, admin, ("Email two-step", [
        {"name": "S1", "approver": {"type": "user", "id": leo_id}},
        {"name": "S2", "approver": {"type": "user", "id": maya_id}},
    ]))
    assert len(_emails_to(app_store, "leo@example.com")) == 1
    assert len(_emails_to(app_store, "maya@example.com")) == 0
    r = client.post(f"/api/approvals/{req['id']}/approve", headers=leo, json={})
    assert r.status_code == 200, r.get_json()
    # advancing to step 2 emails maya; final decision not yet made
    mails = _emails_to(app_store, "maya@example.com")
    assert len(mails) == 1, app_store.email_log(limit=500)
    assert "S2" in mails[0]["body"]


def test_custom_template_overrides_default(client, app_store, admin, leo):
    leo_id = _set_email(client, admin, leo, "leo@example.com")
    r = client.post("/api/admin/email-templates", headers=admin, json={
        "name": "Approval request notification",
        "subject": "CUSTOM: {{Record.ApprovalProcess}} needs you",
        "body": "Hi, please look at {{Record.Name}}.",
    })
    assert r.status_code in (200, 201), r.get_json()
    _submit(client, admin,
            ("Email custom tpl", [{"name": "S1", "approver": {"type": "user", "id": leo_id}}]))
    mails = _emails_to(app_store, "leo@example.com")
    assert len(mails) == 1, app_store.email_log(limit=500)
    assert mails[0]["subject"] == "CUSTOM: Email custom tpl needs you"
    assert mails[0]["body"] == "Hi, please look at EmailAppr."


def test_missing_approver_email_is_silent(client, app_store, admin, leo, maya):
    # leo has NO email address: submit must still succeed, no crash, no mail.
    leo_id = _uid(client, leo)
    maya_id = _set_email(client, admin, maya, "maya@example.com")
    req = _submit(client, admin, ("Email silent", [
        {"name": "S1", "approver": {"type": "user", "id": leo_id}},
        {"name": "S2", "approver": {"type": "user", "id": maya_id}},
    ]))
    assert req["status"] == "Pending"
    assert app_store.email_log(limit=500) == []
    # step 1 approved by leo -> maya (who has an email) gets notified
    r = client.post(f"/api/approvals/{req['id']}/approve", headers=leo, json={})
    assert r.status_code == 200, r.get_json()
    assert len(_emails_to(app_store, "maya@example.com")) == 1
