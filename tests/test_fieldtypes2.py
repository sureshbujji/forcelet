"""Tests for the Salesforce-parity field types batch: AutoNumber, EncryptedText,
RichTextArea, and Formula as a first-class field type — plus the recTitle()
contact-title bug fix in web/index.html."""
import re
import threading

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


def _h(client):
    return login(client)


def _rid(resp):
    body = resp.get_json()
    assert resp.status_code in (200, 201), body
    return body.get("Id") or body.get("id")


def _mkobj(c, h, name="Invoice"):
    r = c.post("/api/admin/objects", headers=h,
               json={"name": name, "label": name, "plural": name + "s"})
    assert r.status_code == 201, r.get_json()


def _addfield(c, h, obj, field):
    r = c.post(f"/api/admin/objects/{obj}/fields", headers=h, json=field)
    assert r.status_code == 201, (field, r.get_json())
    return r.get_json()


def _invoice_obj(c, h):
    _mkobj(c, h)
    _addfield(c, h, "Invoice",
              {"name": "Amount", "label": "Amount", "type": "Currency"})
    _addfield(c, h, "Invoice",
              {"name": "InvNo", "label": "Invoice No", "type": "AutoNumber",
               "auto_prefix": "INV-", "auto_start": 100, "auto_width": 5})
    _addfield(c, h, "Invoice",
              {"name": "Secret", "label": "Secret", "type": "EncryptedText",
               "mask_chars": 4})
    _addfield(c, h, "Invoice",
              {"name": "Notes", "label": "Notes", "type": "RichTextArea"})
    _addfield(c, h, "Invoice",
              {"name": "Total2", "label": "Total Doubled", "type": "Formula",
               "return_type": "Number",
               "formula": {"*": [{"field": "Amount"}, 2]}})


# ------------------------------------------------------------------ AutoNumber
def test_autonumber_sequencing_and_format(client):
    h = _h(client)
    _invoice_obj(client, h)
    a = _rid(client.post("/api/sobjects/Invoice", headers=h, json={"Amount": 1}))
    b = _rid(client.post("/api/sobjects/Invoice", headers=h, json={"Amount": 2}))
    ga = client.get(f"/api/sobjects/Invoice/{a}", headers=h).get_json()
    gb = client.get(f"/api/sobjects/Invoice/{b}", headers=h).get_json()
    assert ga["InvNo"] == "INV-00100"
    assert gb["InvNo"] == "INV-00101"


def test_autonumber_custom_prefix_and_width(client):
    h = _h(client)
    _mkobj(client, h, "Ticket")
    _addfield(client, h, "Ticket",
              {"name": "TNo", "label": "Ticket No", "type": "AutoNumber",
               "auto_prefix": "T-", "auto_width": 3})
    r = client.post("/api/sobjects/Ticket", headers=h, json={})
    assert r.get_json()["TNo"] == "T-001"


def test_autonumber_readonly_on_create_and_update(client):
    h = _h(client)
    _invoice_obj(client, h)
    r = client.post("/api/sobjects/Invoice", headers=h,
                    json={"InvNo": "HAX-1", "Amount": 1})
    assert r.status_code == 422
    rid = _rid(client.post("/api/sobjects/Invoice", headers=h, json={"Amount": 1}))
    r = client.patch(f"/api/sobjects/Invoice/{rid}", headers=h,
                     json={"InvNo": "HAX-2"})
    assert r.status_code == 422


def test_autonumber_config_validation(client):
    h = _h(client)
    _mkobj(client, h)
    for bad in ({"auto_width": 99}, {"auto_width": 0}, {"auto_start": -1},
                {"auto_prefix": 5}):
        f = {"name": "A", "label": "A", "type": "AutoNumber", **bad}
        r = client.post("/api/admin/objects/Invoice/fields", headers=h, json=f)
        assert r.status_code == 422, (bad, r.get_json())


def test_autonumber_threadsafe_increments(client, app):
    h = _h(client)
    _mkobj(client, h, "Seq")
    _addfield(client, h, "Seq",
              {"name": "SNo", "label": "S No", "type": "AutoNumber",
               "auto_prefix": "S-", "auto_width": 4})
    seen, lock = [], threading.Lock()

    def worker():
        c2 = app.test_client()
        # main-thread login already rotated the seeded password to TestPass123!
        h2 = login(c2, password="TestPass123!")
        r = c2.post("/api/sobjects/Seq", headers=h2, json={})
        with lock:
            seen.append(r.get_json().get("SNo"))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(seen) == 10
    assert len(set(seen)) == 10  # no duplicate numbers
    assert sorted(seen) == [f"S-{i:04d}" for i in range(1, 11)]


# ---------------------------------------------------------------- EncryptedText
def test_encrypted_roundtrip_masked_display(client):
    h = _h(client)
    _invoice_obj(client, h)
    rid = _rid(client.post("/api/sobjects/Invoice", headers=h,
                           json={"Secret": "s3cr3t-value-1234"}))
    got = client.get(f"/api/sobjects/Invoice/{rid}", headers=h).get_json()
    assert got["Secret"] == "•" * 13 + "1234"
    assert "s3cr3t-value" not in got["Secret"]
    # list view is masked the same way
    got2 = client.get("/api/sobjects/Invoice", headers=h).get_json()
    assert got2[0]["Secret"] == "•" * 13 + "1234"


def test_encrypted_at_rest_is_ciphertext(client, app, tmp_path):
    h = _h(client)
    _invoice_obj(client, h)
    _rid(client.post("/api/sobjects/Invoice", headers=h,
                     json={"Secret": "s3cr3t-value-1234"}))
    import sqlite3
    db = sqlite3.connect(str(tmp_path / "t.db"))
    (raw,) = db.execute('SELECT "Secret" FROM sobj_Invoice').fetchone()
    assert raw != "s3cr3t-value-1234"
    assert "s3cr3t-value-1234" not in raw


def test_encrypted_csv_export_masked(client):
    h = _h(client)
    _invoice_obj(client, h)
    _rid(client.post("/api/sobjects/Invoice", headers=h,
                     json={"Secret": "s3cr3t-value-1234"}))
    r = client.get("/api/sobjects/Invoice/export", headers=h)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "s3cr3t-value-1234" not in body
    assert "1234" in body


def test_encrypted_mask_resubmit_keeps_value(client, app, tmp_path):
    h = _h(client)
    _invoice_obj(client, h)
    rid = _rid(client.post("/api/sobjects/Invoice", headers=h,
                           json={"Secret": "s3cr3t-value-1234"}))
    import sqlite3
    db = sqlite3.connect(str(tmp_path / "t.db"))
    before = db.execute('SELECT "Secret" FROM sobj_Invoice WHERE id=?',
                        (rid,)).fetchone()[0]
    masked = client.get(f"/api/sobjects/Invoice/{rid}",
                        headers=h).get_json()["Secret"]
    r = client.patch(f"/api/sobjects/Invoice/{rid}", headers=h,
                     json={"Secret": masked})
    assert r.status_code == 200
    after = db.execute('SELECT "Secret" FROM sobj_Invoice WHERE id=?',
                       (rid,)).fetchone()[0]
    assert before == after  # ciphertext untouched
    # a genuinely new secret still overwrites
    r = client.patch(f"/api/sobjects/Invoice/{rid}", headers=h,
                     json={"Secret": "brand-new-secret-9999"})
    assert r.status_code == 200
    got = client.get(f"/api/sobjects/Invoice/{rid}", headers=h).get_json()
    assert got["Secret"] == "•" * 17 + "9999"


def test_encrypted_config_validation(client):
    h = _h(client)
    _mkobj(client, h)
    r = client.post("/api/admin/objects/Invoice/fields", headers=h, json={
        "name": "S", "label": "S", "type": "EncryptedText", "mask_chars": 99})
    assert r.status_code == 422
    _addfield(client, h, "Invoice",
              {"name": "S", "label": "S", "type": "EncryptedText",
               "mask_chars": 4, "length": 10})
    r = client.post("/api/sobjects/Invoice", headers=h,
                    json={"S": "x" * 11})
    assert r.status_code == 422


def test_encrypted_zero_mask_shows_plaintext(client):
    h = _h(client)
    _mkobj(client, h)
    _addfield(client, h, "Invoice",
              {"name": "S", "label": "S", "type": "EncryptedText",
               "mask_chars": 0})
    rid = _rid(client.post("/api/sobjects/Invoice", headers=h,
                           json={"S": "open-secret"}))
    got = client.get(f"/api/sobjects/Invoice/{rid}", headers=h).get_json()
    assert got["S"] == "open-secret"


# ---------------------------------------------------------------- RichTextArea
def test_richtext_sanitizes_on_save(client):
    h = _h(client)
    _invoice_obj(client, h)
    nasty = ('<p>Hello <b>world</b></p><script>alert(1)</script>'
             '<iframe src="x"></iframe>'
             '<a href="javascript:evil()" onclick="steal()">x</a>'
             '<img src="y" onerror="boom()">'
             '<a href="https://example.com">ok</a>')
    rid = _rid(client.post("/api/sobjects/Invoice", headers=h,
                           json={"Notes": nasty}))
    got = client.get(f"/api/sobjects/Invoice/{rid}", headers=h).get_json()
    html = got["Notes"]
    assert "<script" not in html and "alert(1)" not in html
    assert "<iframe" not in html
    assert "javascript:" not in html
    assert "onerror" not in html and "onclick" not in html
    assert "<b>world</b>" in html
    assert '<a href="https://example.com">ok</a>' in html


def test_richtext_length_validation(client):
    h = _h(client)
    _mkobj(client, h)
    _addfield(client, h, "Invoice",
              {"name": "N", "label": "N", "type": "RichTextArea", "length": 20})
    r = client.post("/api/sobjects/Invoice", headers=h,
                    json={"N": "<p>" + "x" * 100 + "</p>"})
    assert r.status_code == 422


def test_richtext_unit_sanitizer():
    from forcelet.field_types import sanitize_html
    out = sanitize_html('<p>a</p><script>evil()</script>'
                        '<a href="javascript:x">y</a>'
                        '<b onmouseover="z">bold</b>')
    assert "<script" not in out and "evil()" not in out
    assert "javascript:" not in out and "onmouseover" not in out
    assert "<p>a</p>" in out and "<b>bold</b>" in out
    assert sanitize_html("<p>") == ""
    assert sanitize_html(None) == ""


# ---------------------------------------------------------------------- Formula
def test_formula_computed_on_read(client):
    h = _h(client)
    _invoice_obj(client, h)
    rid = _rid(client.post("/api/sobjects/Invoice", headers=h,
                           json={"Amount": 50}))
    got = client.get(f"/api/sobjects/Invoice/{rid}", headers=h).get_json()
    assert got["Total2"] == 100
    got = client.get("/api/sobjects/Invoice", headers=h).get_json()
    assert got[0]["Total2"] == 100


def test_formula_rejected_on_write(client):
    h = _h(client)
    _invoice_obj(client, h)
    r = client.post("/api/sobjects/Invoice", headers=h, json={"Total2": 5})
    assert r.status_code == 422
    rid = _rid(client.post("/api/sobjects/Invoice", headers=h,
                           json={"Amount": 1}))
    r = client.patch(f"/api/sobjects/Invoice/{rid}", headers=h,
                     json={"Total2": 9})
    assert r.status_code == 422


def test_formula_config_validation(client):
    h = _h(client)
    _mkobj(client, h)
    # missing formula definition
    r = client.post("/api/admin/objects/Invoice/fields", headers=h, json={
        "name": "F", "label": "F", "type": "Formula", "return_type": "Number"})
    assert r.status_code == 422
    # bad return_type
    r = client.post("/api/admin/objects/Invoice/fields", headers=h, json={
        "name": "F", "label": "F", "type": "Formula", "return_type": "Bogus",
        "formula": {"*": [{"field": "Amount"}, 2]}})
    assert r.status_code == 422
    # no required / default allowed on computed types
    for kw in ({"required": True}, {"default_value": "x"}):
        f = {"name": "F", "label": "F", "type": "Formula",
             "return_type": "Number",
             "formula": {"*": [{"field": "Amount"}, 2]}, **kw}
        r = client.post("/api/admin/objects/Invoice/fields", headers=h, json=f)
        assert r.status_code == 422, (kw, r.get_json())


def test_formula_text_return_type(client):
    h = _h(client)
    _mkobj(client, h)
    _addfield(client, h, "Invoice",
              {"name": "Lbl", "label": "Lbl", "type": "Text"})
    _addfield(client, h, "Invoice",
              {"name": "Shout", "label": "Shout", "type": "Formula",
               "return_type": "Text",
               "formula": {"upper": [{"field": "Lbl"}]}})
    rid = _rid(client.post("/api/sobjects/Invoice", headers=h,
                           json={"Lbl": "hello"}))
    got = client.get(f"/api/sobjects/Invoice/{rid}", headers=h).get_json()
    assert got["Shout"] == "HELLO"


# ------------------------------------------------------------- field-types list
def test_field_types_endpoint_lists_new_types(client):
    h = _h(client)
    names = [t["name"] for t in
             client.get("/api/field-types", headers=h).get_json()]
    for t in ("AutoNumber", "EncryptedText", "RichTextArea", "Formula"):
        assert t in names


# ------------------------------------------------- recTitle contact-title fix
WEB = None


def _web_html():
    global WEB
    if WEB is None:
        import pathlib
        WEB = pathlib.Path(__file__).resolve().parent.parent.joinpath(
            "web", "index.html").read_text()
    return WEB


def test_rectitle_helper_defined_and_person_aware():
    html = _web_html()
    m = re.search(r"function recTitle\(rec\)\{(.*?)\}", html)
    assert m, "recTitle helper missing from web/index.html"
    body = m.group(1)
    assert "FirstName" in body and "LastName" in body
    assert "rec.Name" in body and "rec.Id" in body


def test_rectitle_helper_semantics():
    # Execute the extracted helper in a tiny JS harness via node if present.
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node not available")
    html = _web_html()
    m = re.search(r"function recTitle\(rec\)\{.*?\}", html)
    harness = (m.group(0) + "\n"
               "const cases=["
               "[{FirstName:'Ada',LastName:'Lovelace',Id:'1'},'Ada Lovelace'],"
               "[{FirstName:'Ada',Id:'2'},'Ada'],"
               "[{Name:'Acme',Id:'3'},'Acme'],"
               "[{Subject:'S',Id:'4'},'S'],"
               "[{Id:'5'},'5']];\n"
               "for(const [rec,want] of cases){"
               "const got=recTitle(rec);"
               "if(got!==want){console.error('FAIL',JSON.stringify(rec),got);"
               "process.exit(1);}}\n"
               "console.log('recTitle OK');")
    r = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "recTitle OK" in r.stdout


def test_no_old_title_fallback_pattern_remains():
    html = _web_html()
    # The old inline pattern must survive only inside the helper definition.
    for i, line in enumerate(html.splitlines(), 1):
        if "function recTitle" in line:
            continue
        assert "rec.Name||rec.Subject||rec.Id" not in line, f"line {i}"
        assert "rec.Name||rec.Subject||rec.Title||rec.Id" not in line, \
            f"line {i}"


def test_contact_firstname_lastname_roundtrip(client):
    # The data the fixed title helper depends on must come back from the API.
    h = _h(client)
    r = client.post("/api/sobjects/Contact", headers=h,
                    json={"FirstName": "Ada", "LastName": "Lovelace"})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["FirstName"] == "Ada" and body["LastName"] == "Lovelace"
    got = client.get(f"/api/sobjects/Contact/{body['Id']}",
                     headers=h).get_json()
    assert got["FirstName"] == "Ada" and got["LastName"] == "Lovelace"
