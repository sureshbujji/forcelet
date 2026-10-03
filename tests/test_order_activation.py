"""Tests for order activation (quote-to-cash): create order from quote,
activate/cancel lifecycle, validation errors, and the full chain."""
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
def h(client):
    return login(client, "admin")


def _mk(client, h, obj, fields):
    r = client.post(f"/api/sobjects/{obj}", headers=h, json=fields)
    assert r.status_code == 201, (obj, r.get_json())
    return r.get_json()["Id"]


@pytest.fixture()
def quote_with_lines(client, h):
    acct = _mk(client, h, "Account", {"Name": "Q2C Acme"})
    opp = _mk(client, h, "Opportunity", {
        "Name": "Q2C Opp", "AccountId": acct, "Stage": "Proposal",
        "CloseDate": "2026-12-01"})
    quote = _mk(client, h, "Quote", {"Name": "Q-100", "OpportunityId": opp,
                                    "Status": "Approved"})
    prod = _mk(client, h, "Product", {"Name": "Widget", "ProductCode": "W-1"})
    pb = _mk(client, h, "PriceBook", {"Name": "Std"})
    pbe = _mk(client, h, "PriceBookEntry", {
        "PriceBookId": pb, "ProductId": prod, "UnitPrice": 100.0, "IsActive": True})
    _mk(client, h, "QuoteLineItem", {
        "QuoteId": quote, "PriceBookEntryId": pbe, "Quantity": 3,
        "UnitPrice": 100.0, "Discount": 10})
    return {"quote": quote, "account": acct, "product": prod}


def test_create_order_from_quote(client, h, quote_with_lines):
    r = client.post(f"/api/sales/quotes/{quote_with_lines['quote']}/create-order",
                    headers=h)
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    order = body["order"]
    assert order["Status"] == "Draft"
    assert order["AccountId"] == quote_with_lines["account"]
    assert order["OrderNumber"].startswith("ORD-")
    assert body["line_items_created"] == 1
    # discount baked into the order line's unit price: 100 * 0.9 = 90,
    # line total 3 * 90 = 270
    r = client.get("/api/sobjects/OrderItem", headers=h)
    items = [i for i in r.get_json() if i.get("OrderId") == order["Id"]]
    assert len(items) == 1
    assert items[0]["ProductId"] == quote_with_lines["product"]
    assert items[0]["UnitPrice"] == pytest.approx(90.0)
    assert items[0]["LineTotal"] == pytest.approx(270.0)


def test_create_order_quote_not_found(client, h):
    r = client.post("/api/sales/quotes/nope/create-order", headers=h)
    assert r.status_code == 404


def test_create_order_requires_line_items(client, h):
    acct = _mk(client, h, "Account", {"Name": "Empty Acme"})
    opp = _mk(client, h, "Opportunity", {
        "Name": "Empty Opp", "AccountId": acct, "Stage": "Proposal",
        "CloseDate": "2026-12-01"})
    quote = _mk(client, h, "Quote", {"Name": "Q-empty", "OpportunityId": opp,
                                    "Status": "Approved"})
    r = client.post(f"/api/sales/quotes/{quote}/create-order", headers=h)
    assert r.status_code == 422
    assert "line items" in r.get_json()["error"]


def test_activate_order(client, h, quote_with_lines):
    r = client.post(f"/api/sales/quotes/{quote_with_lines['quote']}/create-order",
                    headers=h)
    oid = r.get_json()["order"]["Id"]
    r = client.post(f"/api/sales/orders/{oid}/activate", headers=h)
    assert r.status_code == 200, r.get_json()
    order = r.get_json()
    assert order["Status"] == "Activated"
    assert order["ActivatedDate"]  # stamped
    # second activation is rejected, never a 500
    r = client.post(f"/api/sales/orders/{oid}/activate", headers=h)
    assert r.status_code == 422
    assert "already activated" in r.get_json()["error"]


def test_activate_requires_line_items(client, h):
    oid = _mk(client, h, "Order", {"OrderNumber": "ORD-T1", "Status": "Draft"})
    r = client.post(f"/api/sales/orders/{oid}/activate", headers=h)
    assert r.status_code == 422
    assert "line items" in r.get_json()["error"]


def test_activate_rejects_non_draft(client, h):
    oid = _mk(client, h, "Order", {"OrderNumber": "ORD-T2", "Status": "Draft"})
    _mk(client, h, "OrderItem", {"OrderId": oid, "Quantity": 1,
                                 "UnitPrice": 50.0, "LineTotal": 50.0})
    r = client.patch(f"/api/sobjects/Order/{oid}", headers=h,
                   json={"Status": "Submitted"})
    assert r.status_code == 200, r.get_json()
    r = client.post(f"/api/sales/orders/{oid}/activate", headers=h)
    assert r.status_code == 422
    assert "Draft" in r.get_json()["error"]


def test_activate_missing_order_is_404(client, h):
    r = client.post("/api/sales/orders/nope/activate", headers=h)
    assert r.status_code == 404


def test_cancel_order(client, h, quote_with_lines):
    r = client.post(f"/api/sales/quotes/{quote_with_lines['quote']}/create-order",
                    headers=h)
    oid = r.get_json()["order"]["Id"]
    r = client.post(f"/api/sales/orders/{oid}/cancel", headers=h)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["Status"] == "Cancelled"
    r = client.post(f"/api/sales/orders/{oid}/cancel", headers=h)
    assert r.status_code == 422
    assert "already cancelled" in r.get_json()["error"]


def test_cancel_fulfilled_rejected(client, h):
    oid = _mk(client, h, "Order", {"OrderNumber": "ORD-T3", "Status": "Draft"})
    r = client.patch(f"/api/sobjects/Order/{oid}", headers=h,
                   json={"Status": "Fulfilled"})
    assert r.status_code == 200, r.get_json()
    r = client.post(f"/api/sales/orders/{oid}/cancel", headers=h)
    assert r.status_code == 422
    assert "Fulfilled" in r.get_json()["error"]


def test_full_quote_to_cash_chain(client, h, quote_with_lines):
    r = client.post(f"/api/sales/quotes/{quote_with_lines['quote']}/create-order",
                    headers=h)
    assert r.status_code == 201, r.get_json()
    oid = r.get_json()["order"]["Id"]
    r = client.post(f"/api/sales/orders/{oid}/activate", headers=h)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["Status"] == "Activated"
    # order numbers are unique across repeated creations
    r = client.post(f"/api/sales/quotes/{quote_with_lines['quote']}/create-order",
                    headers=h)
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["order"]["OrderNumber"] != \
        client.get(f"/api/sobjects/Order/{oid}", headers=h).get_json()["OrderNumber"]
