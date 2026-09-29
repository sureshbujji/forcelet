"""Tests for batch 5: Files, Products/PriceBooks/Quotes, in-app
Notifications, and AI lead scoring."""
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
def admin(client):
    return login(client, "admin")


@pytest.fixture()
def leo(client):
    return login(client, "leo")


@pytest.fixture()
def maya(client):
    return login(client, "maya")


# ------------------------------------------------------------ files
def _upload(client, h, obj, rid, name="spec.txt", body=b"hello forcelet"):
    data = {"file": (io.BytesIO(body), name)}
    return client.post(f"/api/sobjects/{obj}/{rid}/files", headers=h,
                       data=data, content_type="multipart/form-data")


def test_file_upload_list_download_delete(client, admin, leo, maya):
    aid = client.post("/api/sobjects/Account", headers=leo,
                      json={"Name": "FileCo"}).get_json()["Id"]
    r = _upload(client, leo, "Account", aid)
    assert r.status_code == 201, r.get_json()
    fid = r.get_json()["id"]
    assert r.get_json()["size"] == len(b"hello forcelet")

    r = client.get(f"/api/sobjects/Account/{aid}/files", headers=leo)
    assert len(r.get_json()) == 1

    r = client.get(f"/api/files/{fid}", headers=leo)
    assert r.status_code == 200 and r.data == b"hello forcelet"

    # maya (leo's manager) can see the record and download, but not delete
    r = client.get(f"/api/files/{fid}", headers=maya)
    assert r.status_code == 200
    r = client.delete(f"/api/files/{fid}", headers=maya)
    assert r.status_code == 403

    # uploader deletes
    r = client.delete(f"/api/files/{fid}", headers=leo)
    assert r.get_json()["deleted"] is True
    r = client.get(f"/api/sobjects/Account/{aid}/files", headers=leo)
    assert r.get_json() == []


def test_file_on_invisible_record_404(client, admin, leo):
    # ana is read-only; leo's private account is invisible to her
    ana = login(client, "ana")
    aid = client.post("/api/sobjects/Account", headers=leo,
                      json={"Name": "PrivateCo"}).get_json()["Id"]
    r = client.get(f"/api/sobjects/Account/{aid}/files", headers=ana)
    assert r.status_code == 404
    r = _upload(client, admin, "Account", aid)
    assert r.status_code == 201
    fid = r.get_json()["id"]
    r = client.get(f"/api/files/{fid}", headers=ana)
    assert r.status_code == 404


# ------------------------------------------------------------ quote-to-cash
def test_pricebook_seeded(client, admin):
    r = client.get("/api/sobjects/PriceBook", headers=admin)
    pbs = r.get_json()
    assert any(p["Name"] == "Standard Price Book" and p["IsStandard"] for p in pbs)
    r = client.get("/api/sobjects/Product", headers=admin)
    prods = r.get_json()
    assert len(prods) >= 3
    r = client.get("/api/sobjects/PriceBookEntry", headers=admin)
    entries = r.get_json()
    assert len(entries) >= 3
    assert all(e["UnitPrice"] for e in entries)


def test_pricebook_visible_org_wide(client, admin, leo):
    # seeded entries are owned by admin but visible to standard users
    entries = client.get("/api/sobjects/PriceBookEntry", headers=leo).get_json()
    assert len(entries) >= 3
    prods = client.get("/api/sobjects/Product", headers=leo).get_json()
    assert len(prods) >= 3
    # ...but standard users cannot create price book entries (404 = no access)
    r = client.post("/api/sobjects/PriceBookEntry", headers=leo, json={
        "PriceBookId": "x", "ProductId": "y", "UnitPrice": 1})
    assert r.status_code == 404


def _mk_quote(client, h):
    opp = client.post("/api/sobjects/Opportunity", headers=h,
                      json={"Name": "Q Opp", "Stage": "Proposal",
                            "CloseDate": "2026-12-01"}).get_json()
    q = client.post("/api/sobjects/Quote", headers=h,
                    json={"Name": "Q-001",
                          "OpportunityId": opp["Id"]}).get_json()
    assert q["Id"]
    return opp["Id"], q["Id"]


def test_quote_line_totals_and_grand_total(client, admin):
    h = admin
    _, qid = _mk_quote(client, h)
    entries = client.get("/api/sobjects/PriceBookEntry",
                         headers=h).get_json()
    
    e1, e2 = entries[0], entries[1]

    r = client.post("/api/sobjects/QuoteLineItem", headers=h, json={
        "QuoteId": qid, "PriceBookEntryId": e1["Id"],
        "Quantity": 2, "UnitPrice": e1["UnitPrice"], "Discount": 10})
    assert r.status_code == 201, r.get_json()
    expected1 = round(2 * e1["UnitPrice"] * 0.9, 2)
    assert r.get_json()["TotalPrice"] == expected1

    r = client.post("/api/sobjects/QuoteLineItem", headers=h, json={
        "QuoteId": qid, "PriceBookEntryId": e2["Id"],
        "Quantity": 1, "UnitPrice": e2["UnitPrice"]})
    assert r.status_code == 201, r.get_json()
    expected2 = round(e2["UnitPrice"], 2)

    # rollup: GrandTotal is computed on read
    r = client.get(f"/api/sobjects/Quote/{qid}", headers=h)
    assert r.get_json()["GrandTotal"] == round(expected1 + expected2, 2)

    # updating a line re-computes via the trigger
    lid = client.get("/api/sobjects/QuoteLineItem", headers=h).get_json()
    lid = lid[0]["Id"]
    r = client.patch(f"/api/sobjects/QuoteLineItem/{lid}", headers=h,
                     json={"Quantity": 5})
    assert r.get_json()["TotalPrice"] == round(5 * e1["UnitPrice"] * 0.9, 2)


# ------------------------------------------------------------ notifications
def test_mention_notification(client, admin, leo):
    r = client.post("/api/feed", headers=admin,
                    json={"body": "hey @leo check this out"})
    assert r.status_code == 201
    r = client.get("/api/notifications/unread-count", headers=leo)
    assert r.get_json()["count"] == 1
    items = client.get("/api/notifications", headers=leo).get_json()
    assert items[0]["ntype"] == "mention"
    assert items[0]["is_read"] == 0
    r = client.post("/api/notifications/read", headers=leo, json={"all": True})
    assert r.get_json()["ok"] is True
    r = client.get("/api/notifications/unread-count", headers=leo)
    assert r.get_json()["count"] == 0


def test_approval_notification(client, leo, maya):
    r = client.post("/api/sobjects/Opportunity", headers=leo,
                    json={"Name": "Big discount", "Stage": "Proposal",
                          "CloseDate": "2026-12-01", "DiscountPercent": 25})
    assert r.status_code == 201
    # auto-submitted for approval -> leo's manager maya is notified
    items = client.get("/api/notifications?unread_only=1",
                       headers=maya).get_json()
    assert any(n["ntype"] == "approval" for n in items)


def test_assignment_notification(client, admin, leo, maya):
    # round-robin web-lead rule assigns to leo or maya, not admin
    r = client.post("/api/sobjects/Lead", headers=admin,
                    json={"LastName": "Webby", "Company": "WebCo",
                          "Status": "New", "LeadSource": "Web"})
    assert r.status_code == 201
    lid = r.get_json()["Id"]
    owner = client.get(f"/api/sobjects/Lead/{lid}",
                       headers=admin).get_json()["OwnerId"]
    assert owner in (leo_user_id(client, leo), maya_user_id(client, maya))
    h = leo if owner == leo_user_id(client, leo) else maya
    items = client.get("/api/notifications?unread_only=1",
                       headers=h).get_json()
    assert any(n["ntype"] == "assignment" for n in items)


def leo_user_id(client, leo):
    return client.get("/api/me", headers=leo).get_json()["id"]


def maya_user_id(client, maya):
    return client.get("/api/me", headers=maya).get_json()["id"]


# ------------------------------------------------------------ lead scoring
def _lead(client, h, **kw):
    body = {"LastName": "Sc", "Company": "ScCo", "Status": "New"}
    body.update(kw)
    r = client.post("/api/sobjects/Lead", headers=h, json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["Id"]


def test_train_rejects_small_history(client, admin):
    r = client.post("/api/admin/ml/train-lead-scoring", headers=admin, json={})
    assert r.status_code == 422
    assert "at least 10" in r.get_json()["error"]


def test_train_and_score(client, admin):
    h = admin
    for i in range(6):
        _lead(client, h, LastName=f"Hot{i}", Email=f"hot{i}@x.co",
              Phone="555-0100", Rating="Hot", LeadSource="Web",
              Status="Converted")
    for i in range(6):
        _lead(client, h, LastName=f"Cold{i}", Rating="Cold",
              LeadSource="Other", Status="Unqualified")
    r = client.post("/api/admin/ml/train-lead-scoring", headers=h, json={})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["samples"] == 12
    assert r.get_json()["accuracy"] >= 0.8

    hot = _lead(client, h, Email="a@b.co", Phone="555-0100", Rating="Hot",
                LeadSource="Web")
    r = client.get(f"/api/sobjects/Lead/{hot}/score", headers=h)
    assert r.status_code == 200
    s = r.get_json()
    assert 0 <= s["score"] <= 100
    assert s["grade"] == "Hot"
    assert s["factors"]

    cold = _lead(client, h, Rating="Cold", LeadSource="Other")
    r = client.get(f"/api/sobjects/Lead/{cold}/score", headers=h)
    assert r.get_json()["grade"] == "Cold"


def test_score_untrained_404(client, admin):
    lid = _lead(client, admin)
    r = client.get(f"/api/sobjects/Lead/{lid}/score", headers=admin)
    assert r.status_code == 404


def test_ml_model_in_package(client, admin):
    for i in range(6):
        _lead(client, admin, LastName=f"P{i}", Email=f"p{i}@x.co",
              Rating="Hot", Status="Converted")
        _lead(client, admin, LastName=f"N{i}", Rating="Cold",
              Status="Unqualified")
    r = client.post("/api/admin/ml/train-lead-scoring", headers=admin, json={})
    assert r.status_code == 200
    r = client.get("/api/admin/packages/export", headers=admin)
    pkg = r.get_json()
    assert "lead_scoring" in [m["id"] for m in
                              pkg["config"].get("ml_models", [])]
