"""Tests for the Forcelet Automotive pack: VehicleDefinition, Vehicle, Asset,
Order, OrderItem, Delivery — plus auto-numbering triggers, line-total
trigger, order rollup, validation rules, duplicate VIN detection, and the
order-activation / vehicle-delivered flows."""
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


def _list(client, h, obj):
    r = client.get(f"/api/sobjects/{obj}", headers=h)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _get(client, h, obj, rid):
    return client.get(f"/api/sobjects/{obj}/{rid}", headers=h).get_json()


# ------------------------------------------------------------ seed data
def test_automotive_seed_data(client, admin):
    assert len(_list(client, admin, "VehicleDefinition")) == 2
    assert len(_list(client, admin, "Vehicle")) == 2
    assert len(_list(client, admin, "Order")) == 1
    assert len(_list(client, admin, "OrderItem")) == 2
    assert len(_list(client, admin, "Delivery")) == 1
    assert len(_list(client, admin, "Asset")) == 1

    order = [o for o in _list(client, admin, "Order")
             if o.get("OrderNumber") == "ORD-000001"][0]
    assert order["TotalAmount"] == 149800.00  # 69900 + 79900 rollup
    assert order["Status"] == "Activated"


# ------------------------------------------------------------ auto-numbering
def test_order_number_generated(client, admin):
    r = client.post("/api/sobjects/Order", headers=admin,
                    json={"OrderDate": "2026-10-01"})
    assert r.status_code == 201, r.get_json()
    body = _get(client, admin, "Order", r.get_json()["Id"])
    assert body["OrderNumber"] == "ORD-000002"  # seed used ORD-000001


def test_delivery_number_generated(client, admin):
    r = client.post("/api/sobjects/Delivery", headers=admin,
                    json={"Name": "Test delivery"})
    assert r.status_code == 201, r.get_json()
    body = _get(client, admin, "Delivery", r.get_json()["Id"])
    assert body["DeliveryNumber"] == "DLV-000002"  # seed used DLV-000001


# ------------------------------------------------------------ line total + rollup
def test_order_line_total_trigger(client, admin):
    r = client.post("/api/sobjects/Order", headers=admin, json={})
    oid = r.get_json()["Id"]
    r = client.post("/api/sobjects/OrderItem", headers=admin,
                    json={"OrderId": oid, "Quantity": 3, "UnitPrice": 150.00})
    assert r.status_code == 201, r.get_json()
    item = _get(client, admin, "OrderItem", r.get_json()["Id"])
    assert item["LineTotal"] == 450.00

    order = _get(client, admin, "Order", oid)
    assert order["TotalAmount"] == 450.00


# ------------------------------------------------------------ validation rules
def test_vin_must_be_17_chars(client, admin):
    r = client.post("/api/sobjects/Vehicle", headers=admin,
                    json={"Name": "Bad VIN car", "VIN": "SHORT"})
    assert r.status_code == 422
    assert any("17 characters" in d for d in r.get_json()["details"])


def test_vin_17_chars_ok(client, admin):
    r = client.post("/api/sobjects/Vehicle", headers=admin,
                    json={"Name": "Good VIN car", "VIN": "1HGCM82633A004352"})
    assert r.status_code == 201, r.get_json()


def test_delivery_dates_sane(client, admin):
    r = client.post("/api/sobjects/Delivery", headers=admin,
                    json={"Name": "Backwards",
                          "ScheduledDate": "2026-10-10",
                          "DeliveredDate": "2026-10-01"})
    assert r.status_code == 422
    assert any("Scheduled Date" in d for d in r.get_json()["details"])


# ------------------------------------------------------------ duplicate VIN
def test_duplicate_vin_blocked(client, admin):
    r = client.post("/api/sobjects/Vehicle", headers=admin,
                    json={"Name": "Twin", "VIN": "7U4AA1C50RA101101"})
    assert r.status_code == 409
    assert r.get_json()["duplicates"]


# ------------------------------------------------------------ flows
def test_activation_creates_delivery(client, admin):
    r = client.post("/api/sobjects/Order", headers=admin,
                    json={"Status": "Draft", "OrderDate": "2026-10-02"})
    oid = r.get_json()["Id"]
    before = {d["Id"] for d in _list(client, admin, "Delivery")}

    r = client.patch(f"/api/sobjects/Order/{oid}", headers=admin,
                     json={"Status": "Activated"})
    assert r.status_code == 200, r.get_json()

    after = [d for d in _list(client, admin, "Delivery") if d["Id"] not in before]
    assert len(after) == 1
    assert after[0]["OrderId"] == oid
    assert after[0]["Status"] == "Scheduled"
    assert after[0]["DeliveryNumber"].startswith("DLV-")


def test_delivery_marks_vehicle_delivered(client, admin):
    seeded = [d for d in _list(client, admin, "Delivery")
              if d.get("DeliveryNumber") == "DLV-000001"][0]
    vid = seeded["VehicleId"]
    assert _get(client, admin, "Vehicle", vid)["Status"] != "Delivered"

    r = client.patch(f"/api/sobjects/Delivery/{seeded["Id"]}", headers=admin,
                     json={"Status": "Delivered", "DeliveredDate": "2026-10-05"})
    assert r.status_code == 200, r.get_json()
    assert _get(client, admin, "Vehicle", vid)["Status"] == "Delivered"


# ------------------------------------------------------------ paths
def test_automotive_paths_seeded(client, admin):
    for obj, field in (("Vehicle", "Status"), ("Order", "Status")):
        r = client.get(f"/api/paths/{obj}", headers=admin)
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["field"] == field
