"""Tests for expansion batch A: WorkOrder, ServiceAppointment, Event,
Campaign, CampaignMember, Contract, KnowledgeArticle."""
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
def test_expansion_seed_data(client, admin):
    assert len(_list(client, admin, "Campaign")) == 1
    assert len(_list(client, admin, "CampaignMember")) == 2
    assert len(_list(client, admin, "Contract")) == 1
    assert len(_list(client, admin, "KnowledgeArticle")) == 2
    assert len(_list(client, admin, "WorkOrder")) == 1
    assert len(_list(client, admin, "ServiceAppointment")) == 1
    assert len(_list(client, admin, "Event")) == 1

    camp = _list(client, admin, "Campaign")[0]
    assert camp["NumResponses"] == 1  # 1 of 2 members responded
    assert camp["Status"] == "In Progress"


# ------------------------------------------------------------ auto-numbering
def test_auto_numbers(client, admin):
    r = client.post("/api/sobjects/WorkOrder", headers=admin, json={"Name": "WO"})
    assert _get(client, admin, "WorkOrder", r.get_json()["Id"])["WorkOrderNumber"] == "W-000002"
    r = client.post("/api/sobjects/Contract", headers=admin, json={"StartDate": "2026-10-01"})
    assert _get(client, admin, "Contract", r.get_json()["Id"])["ContractNumber"] == "C-000002"
    r = client.post("/api/sobjects/KnowledgeArticle", headers=admin, json={"Title": "T"})
    assert _get(client, admin, "KnowledgeArticle", r.get_json()["Id"])["ArticleNumber"] == "KA-000003"


# ------------------------------------------------------------ validation rules
def test_contract_dates(client, admin):
    r = client.post("/api/sobjects/Contract", headers=admin,
                    json={"StartDate": "2026-10-01", "EndDate": "2026-09-01"})
    assert r.status_code == 422
    assert any("Start Date" in d for d in r.get_json()["details"])


def test_event_end_not_before_start(client, admin):
    r = client.post("/api/sobjects/Event", headers=admin,
                    json={"Subject": "Bad",
                          "StartDateTime": "2026-10-02T10:00:00",
                          "EndDateTime": "2026-10-02T09:00:00"})
    assert r.status_code == 422


# ------------------------------------------------------------ flow
def test_appointment_completion_closes_work_order(client, admin):
    appt = [a for a in _list(client, admin, "ServiceAppointment")
            if a["Status"] == "Scheduled"][0]
    wo_before = _get(client, admin, "WorkOrder", appt["WorkOrderId"])
    assert wo_before["Status"] != "Completed"

    r = client.patch(f"/api/sobjects/ServiceAppointment/{appt['Id']}",
                     headers=admin, json={"Status": "Completed"})
    assert r.status_code == 200, r.get_json()
    assert _get(client, admin, "WorkOrder", appt["WorkOrderId"])["Status"] == "Completed"


# ------------------------------------------------------------ path
def test_work_order_path_seeded(client, admin):
    r = client.get("/api/paths/WorkOrder", headers=admin)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["field"] == "Status"
