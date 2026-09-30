"""Tests for the Experience Cloud customer portal: portal auth, account
isolation, case/appointment/report scoping, and profile self-service."""
import pytest

from helpers import login
from forcelet.api import create_app
from forcelet.security import hash_password


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


def _mk(client, h, obj, fields):
    r = client.post(f"/api/sobjects/{obj}", headers=h, json=fields)
    assert r.status_code in (200, 201), r.get_json()
    return r.get_json()["Id"]


_n = 0


def _portal_user(app, client, h, suffix=None):
    """Create Account + Contact + CommunityUser; return (username, password, ids)."""
    global _n
    _n += 1
    suffix = suffix or f"t{_n}"
    acct = _mk(client, h, "Account", {"Name": f"Portal Co {suffix}"})
    contact = _mk(client, h, "Contact",
                  {"FirstName": "Test", "LastName": f"User{suffix}",
                   "Email": f"user{suffix}@example.com",
                   "AccountId": acct})
    username = f"portal-{suffix}"
    password = "Secret123!"
    store = app.mf_store
    cu_id = store.insert("CommunityUser", {
        "Name": f"Test User{suffix}", "ContactId": contact,
        "Username": username, "PasswordHash": hash_password(password),
        "IsActive": True})
    return {"username": username, "password": password, "account": acct,
            "contact": contact, "cu": cu_id}


def _plogin(client, username, password):
    return client.post("/api/portal/login",
                       json={"username": username, "password": password})


def _ph(client, username, password):
    r = _plogin(client, username, password)
    assert r.status_code == 200, r.get_json()
    return {"Authorization": "Bearer " + r.get_json()["token"]}


# ------------------------------------------------------------------- login
def test_portal_login_success(app, client, h):
    u = _portal_user(app, client, h, "login-ok")
    r = _plogin(client, u["username"], u["password"])
    assert r.status_code == 200
    body = r.get_json()
    assert body["token"].startswith("mf_portal_")
    assert "Test" in body["contact_name"]


def test_portal_login_wrong_password(app, client, h):
    u = _portal_user(app, client, h, "login-bad")
    r = _plogin(client, u["username"], "WrongPass1!")
    assert r.status_code == 401


def test_portal_login_unknown_user(client):
    r = _plogin(client, "no-such-portal-user", "whatever")
    assert r.status_code == 401


def test_portal_login_inactive(app, client, h):
    u = _portal_user(app, client, h, "login-inactive")
    app.mf_store.update("CommunityUser", u["cu"], {"IsActive": False})
    r = _plogin(client, u["username"], u["password"])
    assert r.status_code == 401


def test_portal_login_case_insensitive(app, client, h):
    u = _portal_user(app, client, h, "login-case")
    r = _plogin(client, u["username"].upper(), u["password"])
    assert r.status_code == 200


def test_portal_brute_force_lockout(app, client, h):
    u = _portal_user(app, client, h, "login-lock")
    for _ in range(5):
        assert _plogin(client, u["username"], "WrongPass1!").status_code == 401
    r = _plogin(client, u["username"], "WrongPass1!")
    assert r.status_code == 429


# --------------------------------------------------------------- auth guard
def test_authed_endpoints_require_token(client):
    for path in ("/api/portal/cases", "/api/portal/appointments",
                 "/api/portal/reports", "/api/portal/profile"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={
            "Authorization": "Bearer mf_portal_bogus"}).status_code == 401


def test_internal_token_rejected_by_portal(client, h):
    assert client.get("/api/portal/cases", headers=h).status_code == 401


def test_portal_token_rejected_by_internal_api(app, client, h):
    u = _portal_user(app, client, h, "tok-scope")
    ph = _ph(client, u["username"], u["password"])
    assert client.get("/api/sobjects/Case", headers=ph).status_code == 401
    r = client.post("/api/sobjects/Case", headers=ph,
                    json={"Subject": "x"})
    assert r.status_code == 401
    assert client.get("/api/session", headers=ph).status_code == 401


def test_logout_invalidates_token(app, client, h):
    u = _portal_user(app, client, h, "logout")
    ph = _ph(client, u["username"], u["password"])
    assert client.get("/api/portal/cases", headers=ph).status_code == 200
    assert client.post("/api/portal/logout", headers=ph).status_code == 200
    assert client.get("/api/portal/cases", headers=ph).status_code == 401


# ------------------------------------------------------- knowledge base
def test_public_kb_lists_only_published(app, client, h):
    pub = _mk(client, h, "KnowledgeArticle",
              {"Title": "Public KB", "Status": "Published",
               "Summary": "s", "Body": "b"})
    draft = _mk(client, h, "KnowledgeArticle",
                {"Title": "Draft KB", "Status": "Draft",
                 "Summary": "s", "Body": "b"})
    r = client.get("/api/portal/kb")
    assert r.status_code == 200
    ids = [a["Id"] for a in r.get_json()]
    # 2 published articles are seeded by bootstrap; the draft must not appear
    assert pub in ids and len(ids) == 3
    # draft detail is not reachable
    assert client.get(f"/api/portal/kb/{pub}").status_code == 200
    assert client.get(f"/api/portal/kb/{draft}").status_code == 404
    assert client.get("/api/portal/kb/" + "0" * 16).status_code == 404


# ------------------------------------------------------- demo seed
def test_demo_portal_user_seeded(client):
    r = _plogin(client, "portal-demo", "portal123")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["contact_name"] == "Portal Demo"


# ------------------------------------------------------- account isolation
def test_accountless_contact_sees_nothing(app, client, h):
    contact = _mk(client, h, "Contact",
                  {"FirstName": "No", "LastName": "Account",
                   "Email": "noacct@example.com"})
    app.mf_store.insert("CommunityUser", {
        "Name": "No Account", "ContactId": contact,
        "Username": "portal-noacct", "PasswordHash": hash_password("Secret123!"),
        "IsActive": True})
    ph = _ph(client, "portal-noacct", "Secret123!")
    assert client.get("/api/portal/cases", headers=ph).get_json() == []
    assert client.get("/api/portal/appointments", headers=ph).get_json() == []
    assert client.get("/api/portal/reports", headers=ph).get_json() == []
    r = client.post("/api/portal/cases", headers=ph,
                    json={"subject": "x"})
    assert r.status_code == 422


def test_case_isolation(app, client, h):
    a = _portal_user(app, client, h, "iso-a")
    b = _portal_user(app, client, h, "iso-b")
    pha, phb = _ph(client, a["username"], a["password"]), \
        _ph(client, b["username"], b["password"])
    r = client.post("/api/portal/cases", headers=pha,
                    json={"subject": "A's problem", "priority": "High"})
    assert r.status_code == 201
    ca = client.get("/api/portal/cases", headers=pha).get_json()
    cb = client.get("/api/portal/cases", headers=phb).get_json()
    assert [c["Subject"] for c in ca] == ["A's problem"]
    assert cb == []
    assert ca[0]["Priority"] == "High" and ca[0]["Status"] == "New"


def test_case_creation_ignores_spoofed_account(app, client, h):
    a = _portal_user(app, client, h, "spoof-a")
    b = _portal_user(app, client, h, "spoof-b")
    pha = _ph(client, a["username"], a["password"])
    r = client.post("/api/portal/cases", headers=pha,
                    json={"subject": "spoof attempt",
                          "AccountId": b["account"], "ContactId": b["contact"],
                          "Status": "Closed"})
    assert r.status_code == 201
    rec = app.mf_store.get("Case", r.get_json()["Id"])
    assert rec["AccountId"] == a["account"]
    assert rec["ContactId"] == a["contact"]
    assert rec["Status"] == "New"
    assert rec["Origin"] == "Portal"


def test_case_requires_subject(app, client, h):
    u = _portal_user(app, client, h, "case-subj")
    ph = _ph(client, u["username"], u["password"])
    assert client.post("/api/portal/cases", headers=ph,
                       json={"description": "no subject"}).status_code == 422


def _wo_appt(client, h, account_id, start):
    wo = _mk(client, h, "WorkOrder",
             {"Name": "WO", "Status": "New", "Subject": "Fix it",
              "AccountId": account_id})
    return _mk(client, h, "ServiceAppointment",
               {"Name": "Visit", "WorkOrderId": wo, "Status": "Scheduled",
                "ScheduledStart": start, "ScheduledEnd": start,
                "Technician": "Sam Rivera"})


def test_appointment_isolation(app, client, h):
    a = _portal_user(app, client, h, "appt-a")
    b = _portal_user(app, client, h, "appt-b")
    _wo_appt(client, h, a["account"], "2026-11-05T10:00:00")
    _wo_appt(client, h, b["account"], "2026-11-06T10:00:00")
    # past appointments are not "upcoming"
    _wo_appt(client, h, a["account"], "2020-01-01T10:00:00")
    pha, phb = _ph(client, a["username"], a["password"]), \
        _ph(client, b["username"], b["password"])
    aa = client.get("/api/portal/appointments", headers=pha).get_json()
    ab = client.get("/api/portal/appointments", headers=phb).get_json()
    assert len(aa) == 1 and aa[0]["Technician"] == "Sam Rivera"
    assert aa[0]["WorkOrderSubject"] == "Fix it"
    assert len(ab) == 1
    assert aa[0]["Id"] != ab[0]["Id"]


def test_report_isolation(app, client, h):
    a = _portal_user(app, client, h, "rep-a")
    b = _portal_user(app, client, h, "rep-b")
    appt_a = _wo_appt(client, h, a["account"], "2026-11-05T10:00:00")
    appt_b = _wo_appt(client, h, b["account"], "2026-11-06T10:00:00")
    for appt in (appt_a, appt_b):
        _mk(client, h, "ServiceReport",
            {"Name": "R", "ServiceAppointmentId": appt,
             "Summary": f"report for {appt}", "SignatureName": "Cust",
             "SignatureData": "data:image/png;base64,AAA",
             "SignedAt": "2026-11-05T12:00:00"})
    pha, phb = _ph(client, a["username"], a["password"]), \
        _ph(client, b["username"], b["password"])
    ra = client.get("/api/portal/reports", headers=pha).get_json()
    rb = client.get("/api/portal/reports", headers=phb).get_json()
    assert len(ra) == 1 and ra[0]["ServiceAppointmentId"] == appt_a
    assert ra[0]["SignatureData"] == "data:image/png;base64,AAA"
    assert len(rb) == 1 and rb[0]["ServiceAppointmentId"] == appt_b


# ---------------------------------------------------------------- profile
def test_profile_read_and_update(app, client, h):
    u = _portal_user(app, client, h, "prof")
    ph = _ph(client, u["username"], u["password"])
    p = client.get("/api/portal/profile", headers=ph).get_json()
    assert p["Email"] == "userprof@example.com"
    assert p["AccountName"] == "Portal Co prof"
    r = client.patch("/api/portal/profile", headers=ph,
                     json={"phone": "(555) 999-0000",
                           "email": "new@example.com",
                           "LastName": "Hacker",
                           "AccountId": "spoof"})
    assert r.status_code == 200
    c = app.mf_store.get("Contact", u["contact"])
    assert c["Phone"] == "(555) 999-0000" and c["Email"] == "new@example.com"
    assert c["LastName"] == "Userprof"  # non-editable fields ignored
    r = client.patch("/api/portal/profile", headers=ph,
                     json={"email": "not-an-email"})
    assert r.status_code == 422
