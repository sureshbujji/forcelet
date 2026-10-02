"""Tests for the report-engine Phase 2a fixes (2026-10-02).

Covers: matrix format (column_group_by), date-part grouping (incl. fiscal),
row-level formula columns, PARENTGROUPVAL/PREVGROUPVAL, joined reports,
custom report types, new chart types, printable view, per-widget dashboard
filters + global-filter scoping, multi-level related columns, and dashboard
refresh metadata.
"""
import pytest

from forcelet.api import create_app
from helpers import login


@pytest.fixture()
def app(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def _mk_report(client, h, name, **kw):
    body = {"name": name}
    if "report_type" not in kw:
        kw.setdefault("object", "Account")
        kw.setdefault("columns", ["Name"])
    body.update(kw)
    r = client.post("/api/admin/reports", headers=h, json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def _mk_dashboard(client, h, name, widgets, **kw):
    body = {"name": name, "widgets": widgets}
    body.update(kw)
    r = client.post("/api/dashboards", headers=h, json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def _mk_opp(client, h, aid, name, stage, close, amount):
    r = client.post("/api/sobjects/Opportunity", headers=h,
                    json={"Name": name, "AccountId": aid, "Stage": stage,
                          "CloseDate": close, "Amount": amount})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["Id"]


@pytest.fixture()
def opps(client):
    """Admin; Account + 3 Opportunities across stages/months."""
    h = login(client)
    aid = client.post("/api/sobjects/Account", headers=h,
                      json={"Name": "Acme"}).get_json()["Id"]
    _mk_opp(client, h, aid, "O1", "Prospecting", "2026-10-05", 100)
    _mk_opp(client, h, aid, "O2", "Prospecting", "2026-11-05", 200)
    _mk_opp(client, h, aid, "O3", "Closed Won", "2026-10-15", 300)
    return h


#: Isolate the fixture's Opportunities from seeded rows.
OPP_FILTER = {"in": [{"field": "Name"}, ["O1", "O2", "O3"]]}


# ------------------------------------------------------------------ matrix


def test_matrix_cells_and_totals(client, opps):
    h = opps
    rid = _mk_report(client, h, "Mx", object="Opportunity",
                     columns=["Name"], filters=OPP_FILTER,
                     group_by=["Stage"],
                     column_group_by=[{"field": "CloseDate", "part": "month"}],
                     aggregates=[{"func": "sum", "field": "Amount"}])
    data = client.get(f"/api/reports/{rid}/run", headers=h).get_json()
    m = data["matrix"]
    assert m["row_levels"] == ["Stage"]
    assert m["column_levels"] == ["CloseDate"]
    rows = {n["key"]: n for n in m["rows"]}
    assert set(rows) == {"Prospecting", "Closed Won"}
    pros = rows["Prospecting"]
    cells = {tuple(c["key"]): c for c in pros["columns"]}
    assert cells[("2026-10",)]["count"] == 1
    assert cells[("2026-10",)]["aggregates"]["sum_Amount"] == 100
    assert cells[("2026-11",)]["count"] == 1
    assert cells[("2026-11",)]["aggregates"]["sum_Amount"] == 200
    assert pros["row_total"]["count"] == 2
    assert pros["row_total"]["aggregates"]["sum_Amount"] == 300
    ct = {tuple(c["key"]): c for c in m["column_totals"]}
    assert ct[("2026-10",)]["count"] == 2
    assert ct[("2026-10",)]["aggregates"]["sum_Amount"] == 400
    assert m["grand_total"]["count"] == 3
    assert m["grand_total"]["aggregates"]["sum_Amount"] == 600


def test_matrix_rejects_three_column_levels(client, opps):
    h = opps
    r = client.post("/api/admin/reports", headers=h,
                    json={"name": "Bad", "object": "Opportunity",
                          "column_group_by": ["Stage", "CloseDate", "Name"]})
    assert r.status_code == 422
    assert "column_group_by" in r.get_json()["error"]


# ------------------------------------------------------------------ date parts


def test_date_part_grouping_month(client, opps):
    h = opps
    rid = _mk_report(client, h, "DP", object="Opportunity",
                     columns=["Name"], filters=OPP_FILTER,
                     group_by=[{"field": "CloseDate", "part": "month"}])
    data = client.get(f"/api/reports/{rid}/run", headers=h).get_json()
    keys = {g["key"] for g in data["groups"]}
    assert keys == {"2026-10", "2026-11"}


def test_date_part_fiscal_quarter(client, opps):
    h = opps
    aid = client.post("/api/sobjects/Account", headers=h,
                      json={"Name": "Old"}).get_json()["Id"]
    _mk_opp(client, h, aid, "O4", "Closed Won", "2026-08-05", 50)
    filt = {"in": [{"field": "Name"}, ["O1", "O2", "O3", "O4"]]}
    rid = _mk_report(client, h, "FQ", object="Opportunity",
                     columns=["Name"], filters=filt, fiscal_start_month=10,
                     group_by=[{"field": "CloseDate", "part": "fiscal_quarter"}])
    data = client.get(f"/api/reports/{rid}/run", headers=h).get_json()
    by_key = {g["key"]: g["count"] for g in data["groups"]}
    # Oct/Nov 2026 (fsm=10) -> Q1 FY2027; Aug 2026 -> Q4 FY2026.
    assert by_key.get("Q1 FY2027") == 3
    assert by_key.get("Q4 FY2026") == 1


def test_date_part_invalid_rejected(client, opps):
    h = opps
    r = client.post("/api/admin/reports", headers=h,
                    json={"name": "Bad", "object": "Opportunity",
                          "group_by": [{"field": "CloseDate",
                                        "part": "fortnight"}]})
    assert r.status_code == 422
    assert "part" in r.get_json()["error"]


# ------------------------------------------------------------------ row formulas


def test_row_formula_cross_object(client):
    h = login(client)
    aid = client.post("/api/sobjects/Account", headers=h,
                      json={"Name": "Acme", "AnnualRevenue": 100000}
                      ).get_json()["Id"]
    client.post("/api/sobjects/Contact", headers=h,
                json={"LastName": "FormSmith", "AccountId": aid})
    client.post("/api/sobjects/Contact", headers=h,
                json={"LastName": "FormNoParent"})
    rid = _mk_report(client, h, "RF", object="Contact",
                     columns=["LastName"],
                     row_formulas=[{"name": "DoubleRev",
                                    "formula": {"*": [{"field": "Account.AnnualRevenue"}, 2]}}])
    data = client.get(f"/api/reports/{rid}/run", headers=h).get_json()
    assert "DoubleRev" in data["columns"]
    rows = {r["LastName"]: r for r in data["rows"]}
    assert rows["FormSmith"]["DoubleRev"] == 200000
    assert rows["FormNoParent"]["DoubleRev"] is None


def test_row_formula_bad_ref_rejected(client):
    h = login(client)
    r = client.post("/api/admin/reports", headers=h,
                    json={"name": "Bad", "object": "Contact",
                          "row_formulas": [{"name": "X", "formula": {"field": "Nope.NotReal"}}]})
    assert r.status_code == 422


# ------------------------------------------------------------------ cross-group functions


def _stage_month_report(client, h):
    return _mk_report(client, h, "CG", object="Opportunity",
                      columns=["Name"], filters=OPP_FILTER,
                      group_by=[{"field": "CloseDate", "part": "month"}, "Stage"],
                      aggregates=[{"func": "sum", "field": "Amount"}],
                      summary_formulas=[
                          {"name": "vs_parent",
                           "formula": {"func": "PARENTGROUPVAL", "agg": "sum_Amount"}},
                          {"name": "vs_prev",
                           "formula": {"func": "PREVGROUPVAL", "agg": "sum_Amount"}},
                      ])


def test_parentgroupval(client, opps):
    h = opps
    rid = _stage_month_report(client, h)
    data = client.get(f"/api/reports/{rid}/run", headers=h).get_json()
    tree = {n["key"]: n for n in data["group_tree"]}
    oct_node = tree["2026-10"]
    kids = {c["key"]: c for c in oct_node["children"]}
    # Parent (month) sum for Oct = 100 + 300 = 400.
    assert kids["Prospecting"]["formulas"][0]["value"] == 400
    assert kids["Closed Won"]["formulas"][0]["value"] == 400
    # Top-level nodes have no parent -> None.
    assert oct_node["formulas"][0]["value"] is None


def test_prevgroupval(client, opps):
    h = opps
    rid = _stage_month_report(client, h)
    data = client.get(f"/api/reports/{rid}/run", headers=h).get_json()
    tree = data["group_tree"]
    # count_desc: 2026-10 (2 rows) sorts before 2026-11 (1 row).
    assert tree[0]["key"] == "2026-10"
    assert tree[1]["key"] == "2026-11"
    assert tree[0]["formulas"][1]["value"] is None
    assert tree[1]["formulas"][1]["value"] == 400


# ------------------------------------------------------------------ joined reports


def test_joined_report_two_blocks(client, opps):
    h = opps
    rid = _mk_report(client, h, "Joined",
                     blocks=[{"name": "Accts", "object": "Account",
                              "columns": ["Name"]},
                             {"name": "Opps", "object": "Opportunity",
                              "columns": ["Name", "Amount"],
                              "filters": OPP_FILTER}])
    data = client.get(f"/api/reports/{rid}/run", headers=h).get_json()
    assert data["block_count"] == 2
    names = [b["name"] for b in data["blocks"]]
    assert names == ["Accts", "Opps"]
    assert data["blocks"][0]["row_count"] >= 1
    assert data["blocks"][1]["row_count"] == 3


def test_joined_report_block_validation(client, opps):
    h = opps
    r = client.post("/api/admin/reports", headers=h,
                    json={"name": "J1", "blocks": [{"object": "Account"}]})
    assert r.status_code == 422
    r = client.post("/api/admin/reports", headers=h,
                    json={"name": "J6",
                          "blocks": [{"object": "Account"}] * 6})
    assert r.status_code == 422
    r = client.post("/api/admin/reports", headers=h,
                    json={"name": "JB",
                          "blocks": [{"object": "Account"},
                                     {"object": "Nope"}]})
    assert r.status_code == 422


# ------------------------------------------------------------------ report types


def test_custom_report_type_create_and_use(client, opps):
    h = opps
    r = client.post("/api/admin/report-types", headers=h,
                    json={"name": "opp_pipe", "label": "Opportunity Pipeline",
                          "primary_object": "Opportunity",
                          "related": [{"object": "Account",
                                       "via_field": "AccountId"}],
                          "default_columns": ["Name", "Amount"]})
    assert r.status_code == 201, r.get_json()
    tid = r.get_json()["id"]
    r = client.get("/api/report-types", headers=h)
    assert any(t["id"] == tid for t in r.get_json())
    rid = _mk_report(client, h, "Pipe", report_type="opp_pipe")
    rep = client.get("/api/reports", headers=h).get_json()
    rep = next(x for x in rep if x["id"] == rid)
    assert rep["object"] == "Opportunity"
    assert rep["columns"] == ["Name", "Amount"]
    assert rep["report_type"] == "opp_pipe"
    data = client.get(f"/api/reports/{rid}/run", headers=h).get_json()
    assert data["row_count"] >= 3


def test_custom_report_type_bad_join(client, opps):
    h = opps
    r = client.post("/api/admin/report-types", headers=h,
                    json={"name": "bad", "primary_object": "Opportunity",
                          "related": [{"object": "Account",
                                       "via_field": "Bogus"}]})
    assert r.status_code == 422
    r = client.post("/api/admin/reports", headers=h,
                    json={"name": "X", "report_type": "missing"})
    assert r.status_code == 422


# ------------------------------------------------------------------ charts


def test_new_chart_types_accepted(client, opps):
    h = opps
    for ctype in ("pie", "funnel", "scatter", "stacked_bar", "area",
                  "bar", "line", "donut"):
        rid = _mk_report(client, h, f"C-{ctype}", object="Opportunity",
                         columns=["Name"], group_by=["Stage"],
                         chart={"type": ctype})
        data = client.get(f"/api/reports/{rid}/run", headers=h).get_json()
        assert data["chart"]["type"] == ctype
    r = client.post("/api/admin/reports", headers=h,
                    json={"name": "Bad", "object": "Opportunity",
                          "chart": {"type": "radar"}})
    assert r.status_code == 422


# ------------------------------------------------------------------ printable view


def test_printable_view(client, opps):
    h = opps
    rid = _mk_report(client, h, "Prt", object="Opportunity",
                     columns=["Name", "Amount"], group_by=["Stage"],
                     chart={"type": "bar"})
    r = client.get(f"/api/reports/{rid}/print", headers=h)
    assert r.status_code == 200
    assert "text/html" in r.content_type
    html = r.get_data(as_text=True)
    assert "<table" in html and "<svg" in html
    assert "Prt" in html and "O1" in html


def test_printable_view_404(client, opps):
    h = opps
    r = client.get("/api/reports/nope/print", headers=h)
    assert r.status_code == 404


# ------------------------------------------------------------------ dashboards


def test_per_widget_filter_narrows_one_widget(client, opps):
    h = opps
    rid = _mk_report(client, h, "All", object="Opportunity",
                     columns=["Name"], filters=OPP_FILTER)
    did = _mk_dashboard(client, h, "Dash", [
        {"report_id": rid, "type": "table",
         "filters": [{"field": "Stage", "op": "==", "value": "Prospecting"}]},
        {"report_id": rid, "type": "table"},
    ])
    out = client.post(f"/api/dashboards/{did}/run", headers=h,
                      json={}).get_json()
    counts = [w["data"]["row_count"] for w in out["widgets"]]
    assert counts == [2, 3]
    assert out["last_run_at"], "run must stamp last_run_at"


def test_global_filter_foreign_field_skips_widget(client, opps):
    h = opps
    rid = _mk_report(client, h, "All2", object="Opportunity",
                     columns=["Name"], filters=OPP_FILTER)
    did = _mk_dashboard(client, h, "Dash2",
                        [{"report_id": rid, "type": "table"}],
                        filters=[{"field": "LastName", "op": "==",
                                  "value": "Nobody"}])
    out = client.post(f"/api/dashboards/{did}/run", headers=h,
                      json={}).get_json()
    # LastName is not an Opportunity field: the widget must be left
    # unfiltered, not silently zeroed.
    assert out["widgets"][0]["data"]["row_count"] == 3


def test_global_filter_valid_field_still_applies(client, opps):
    h = opps
    rid = _mk_report(client, h, "All3", object="Opportunity",
                     columns=["Name"], filters=OPP_FILTER)
    did = _mk_dashboard(client, h, "Dash3",
                        [{"report_id": rid, "type": "table"}],
                        filters=[{"field": "Stage", "op": "==",
                                  "value": "Prospecting"}])
    out = client.post(f"/api/dashboards/{did}/run", headers=h,
                      json={}).get_json()
    assert out["widgets"][0]["data"]["row_count"] == 2


def test_dashboard_refresh_schedule(client, opps):
    h = opps
    rid = _mk_report(client, h, "All4", object="Opportunity",
                     columns=["Name"], filters=OPP_FILTER)
    did = _mk_dashboard(client, h, "Dash4",
                        [{"report_id": rid, "type": "table"}],
                        refresh_schedule="daily")
    dashes = client.get("/api/dashboards", headers=h).get_json()
    dash = next(d for d in dashes if d["id"] == did)
    assert dash["refresh_schedule"] == "daily"
    assert dash.get("last_run_at") is None
    out = client.post(f"/api/dashboards/{did}/run", headers=h,
                      json={}).get_json()
    assert out["last_run_at"]
    r = client.post("/api/dashboards", headers=h,
                    json={"name": "Bad", "widgets": [],
                          "refresh_schedule": "hourly"})
    assert r.status_code == 422


# ------------------------------------------------------------------ multi-level columns


def test_multi_level_related_column(client):
    h = login(client)
    gp = client.post("/api/sobjects/Account", headers=h,
                     json={"Name": "Global"}).get_json()["Id"]
    acme = client.post("/api/sobjects/Account", headers=h,
                       json={"Name": "Acme", "ParentAccountId": gp}
                       ).get_json()["Id"]
    client.post("/api/sobjects/Contact", headers=h,
                json={"LastName": "MLSmith", "AccountId": acme})
    rid = _mk_report(client, h, "ML", object="Contact",
                     columns=["LastName", "Account.ParentAccount.Name"])
    data = client.get(f"/api/reports/{rid}/run", headers=h).get_json()
    rows = [r for r in data["rows"] if r.get("LastName") == "MLSmith"]
    assert rows
    assert rows[0]["Account.ParentAccount.Name"] == "Global"
