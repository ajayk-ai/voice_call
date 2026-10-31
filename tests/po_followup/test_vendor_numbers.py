"""Vendors on the dashboard: follow-up selection and Scenario 1 / 2 / 3 numbers (Phone_Master), single and bulk,
without a database or Google Sheet."""

import pytest
from fastapi.testclient import TestClient

from backend.po_followup.api import admin
from backend.po_followup.db import store
from backend.po_followup.services import followup as service
from backend.po_followup.services import users
from backend.po_followup.services import trace, vendor_numbers
from backend.voice_agent.app import app

client = TestClient(app, headers={"X-Session": "signed-in"})
HEADERS = ["Vendor Code", "Vendor Name", "Scenario 1 mobile No", "Scenario 2 No", "Scenario 3 No"]


def master(code, name, od, planner="9800000001", mgmt="9800000002"):
    return {"PO No": "PO" + code, "PO Item No": "10", "Vendor Code": code, "Vendor Name": name, "OD Days": str(od),
            "vendor planner number": planner, "vendor management number": mgmt}


@pytest.fixture
def data(monkeypatch):
    """Phone_Master with one vendor, Master with two, 400186 selected; records every write."""
    tabs = {
        "Master": [master("400186", "ACME", 5), master("400200", "Bolt Co", 0)],
        "Phone_Master": [{"Vendor Code": "400186", "Vendor Name": "Acme (sheet name)",
                          "Scenario 1 mobile No": "9811111111", "Scenario 2 No": "", "Scenario 3 No": "#N/A"}],
    }
    settings = {"vendor_scope": "selected", "vendor_codes": "400186"}
    writes = {"db": [], "sheet": [], "emails": []}
    emails = {"400186": "planner@acme.com"}
    monkeypatch.setattr(store, "vendor_emails", lambda: dict(emails))
    monkeypatch.setattr(store, "save_vendor_emails", lambda values: writes["emails"].append(values))
    monkeypatch.setattr(store, "source_headers", lambda title: HEADERS)
    monkeypatch.setattr(store, "upsert_source_rows", lambda title, rows, key: writes["db"].append((rows, key)))
    monkeypatch.setattr(store, "save_settings", settings.update)
    monkeypatch.setattr(service, "get_settings", lambda: dict(settings))
    monkeypatch.setattr(trace, "source_records", lambda title: tabs[title])
    monkeypatch.setattr(vendor_numbers, "_write_sheet", lambda rows, key: writes["sheet"].append((rows, key)))
    monkeypatch.setattr(admin, "_ready", True)
    monkeypatch.setattr(users, "current", lambda token: {"username": "ravi", "role": "scm"} if token else None)
    return {"writes": writes, "settings": settings}


def test_find_headers_matches_the_sheet_spellings():
    assert vendor_numbers.find_headers(HEADERS) == {
        "code": "Vendor Code", "name": "Vendor Name", 1: "Scenario 1 mobile No", 2: "Scenario 2 No", 3: "Scenario 3 No"}


@pytest.mark.parametrize("raw, ok", [
    ("", ""), ("9876543210", "9876543210"), ("+91 98765 43210", "9876543210"),
    ("9876543210, 09876543211 ; 919876543212", "9876543210, 9876543211, 9876543212"),
])
def test_valid_numbers_are_tidied(raw, ok):
    assert vendor_numbers.check_numbers(raw, 1) == ok


@pytest.mark.parametrize("raw", ["12345", "abc", "9876543210, 99", "9.19877E+11"])
def test_invalid_numbers_are_refused(raw):
    with pytest.raises(ValueError, match="Scenario 2"):
        vendor_numbers.check_numbers(raw, 2)


def test_list_merges_phone_master_master_and_selection(data):
    out = vendor_numbers.list_numbers()
    assert out["ready"] and out["scope"] == "selected" and [v["code"] for v in out["vendors"]] == ["400186", "400200"]
    acme, bolt = out["vendors"]
    assert acme["selected"] and not bolt["selected"]
    assert acme["name"] == "Acme (sheet name)" and acme["in_phone_master"] and acme["overdue_lines"] == 1
    assert acme["numbers"] == ["9811111111", "", ""]  # '#N/A' counts as blank
    assert acme["fallback"] == ["9800000001", "9800000002", "9800000002"]
    assert not bolt["in_phone_master"] and bolt["numbers"] == ["", "", ""] and bolt["overdue_lines"] == 0


def test_prod_save_writes_the_sheet_then_the_copy(data):
    saved = vendor_numbers.save_one(" 400186 ", None, ["9822222222", "", "9833333333"])
    row = {"Vendor Code": "400186", "Scenario 1 mobile No": "9822222222", "Scenario 3 No": "9833333333"}
    w = data["writes"]
    assert w["sheet"] == [([row], "Vendor Code")]  # only changed cells; the name typed in the sheet is left alone
    assert w["db"] == [([row], "Vendor Code")]
    assert saved["code"] == "400186"


def test_a_vendor_gets_its_name_from_master_and_a_new_code_needs_a_name(data):
    vendor_numbers.save_one("400200", None, ["9844444444", "", ""])
    assert data["writes"]["sheet"][0][0][0]["Vendor Name"] == "Bolt Co"
    vendor_numbers.save_one("999999", "New Vendor", ["9844444444", "", ""])
    assert data["writes"]["sheet"][1][0][0]["Vendor Name"] == "New Vendor"


def test_unknown_vendor_and_bad_number_change_nothing(data):
    with pytest.raises(ValueError, match="vendor name is needed"):
        vendor_numbers.save_one("999999", None, ["9844444444", "", ""])
    with pytest.raises(ValueError, match="Scenario 1"):
        vendor_numbers.save_one("400186", None, ["123", "", ""])
    assert data["writes"] == {"db": [], "sheet": [], "emails": []}


def test_selection_and_scope(data):
    out = vendor_numbers.set_selection(["400186", "400200"], "selected", None)
    assert out == {"scope": "selected", "selected": ["400186", "400200"]}
    with pytest.raises(ValueError, match="Not found"):
        vendor_numbers.set_selection(["nope"], "selected", None)
    with pytest.raises(ValueError, match="confirm = ALL"):  # every production vendor needs a typed confirmation
        vendor_numbers.set_selection(["400186"], "all", None)
    assert vendor_numbers.set_selection(["400186"], "all", "ALL")["scope"] == "all"


def test_bulk_checks_everything_first_and_only_writes_changes(data):
    rows = [
        {"line": 2, "code": "400186", "name": None, "numbers": ["9811111111", None, None], "follow": False},  # same number
        {"line": 3, "code": "400200", "name": None, "numbers": ["9855555555", "", ""], "follow": True},
    ]
    preview = vendor_numbers.bulk(rows, apply=False)
    assert preview == {"applied": False, "numbers_updated": 0, "numbers_added": 1, "emails_updated": 0,
                       "selected_added": ["400200"], "selected_removed": ["400186"], "unchanged": 0}
    assert data["writes"] == {"db": [], "sheet": [], "emails": []} and data["settings"]["vendor_codes"] == "400186"

    vendor_numbers.bulk(rows, apply=True)
    added = {"Vendor Code": "400200", "Scenario 1 mobile No": "9855555555", "Vendor Name": "Bolt Co"}
    assert data["writes"]["sheet"] == [([added], "Vendor Code")]
    assert data["settings"]["vendor_codes"] == "400200"


def test_bulk_lists_every_problem_and_saves_nothing(data):
    rows = [{"line": 2, "code": "400186", "numbers": ["1", None, None]},
            {"line": 3, "code": "400186", "numbers": [None, None, None], "follow": True},
            {"line": 4, "code": "zzz"}]
    with pytest.raises(ValueError) as e:
        vendor_numbers.bulk(rows, apply=True)
    assert "Nothing was saved" in str(e.value) and "Row 2" in str(e.value) and "Row 3" in str(e.value) and "Row 4" in str(e.value)
    assert data["writes"] == {"db": [], "sheet": [], "emails": []}


def test_api(data, monkeypatch):
    assert TestClient(app).get("/dashboard/api/vendor-numbers").status_code == 401
    r = client.get("/dashboard/api/vendor-numbers")
    assert r.status_code == 200 and len(r.json()["vendors"]) == 2
    r = client.put("/dashboard/api/vendor-numbers/400186", json={"numbers": ["9855555555", "", ""]})
    assert r.status_code == 200
    r = client.put("/dashboard/api/vendor-numbers/400186", json={"numbers": ["1", "", ""]})
    assert r.status_code == 400 and "Scenario 1" in r.json()["error"]
    assert client.put("/dashboard/api/vendor-numbers/400186", json={"numbers": ["", ""]}).status_code == 422
    r = client.put("/dashboard/api/vendor-selection", json={"scope": "all", "codes": []})
    assert r.status_code == 400 and "ALL" in r.json()["error"]
    r = client.post("/dashboard/api/vendor-numbers/bulk", json={"rows": [{"code": "400200", "follow": True}]})
    assert r.status_code == 200 and r.json()["selected_added"] == ["400200"] and not r.json()["applied"]

    def sheet_down(rows, key):
        raise vendor_numbers.SheetWriteFailed("Could not save to the Google Sheet: no access")
    monkeypatch.setattr(vendor_numbers, "_write_sheet", sheet_down)
    r = client.put("/dashboard/api/vendor-numbers/400186", json={"numbers": ["9855555555", "", ""]})
    assert r.status_code == 502 and "Google Sheet" in r.json()["error"]


@pytest.mark.parametrize("raw, ok", [
    ("", ""), ("A@Acme.com", "a@acme.com"), ("a@acme.com; b@acme.com, a@acme.com  c@x.in", "a@acme.com, b@acme.com, c@x.in"),
])
def test_valid_emails_are_tidied(raw, ok):
    assert vendor_numbers.check_emails(raw) == ok


@pytest.mark.parametrize("raw", ["not-an-email", "a@b", "a@acme.com, nope"])
def test_invalid_emails_are_refused(raw):
    with pytest.raises(ValueError, match="Scenario 4"):
        vendor_numbers.check_emails(raw)


def test_emails_are_saved_with_the_vendor_and_listed(data):
    row = vendor_numbers.save_one("400186", None, ["", "", ""], "  Buyer@Acme.com ")
    assert data["writes"]["emails"] == [{"400186": "buyer@acme.com"}]
    assert row["emails"] == "planner@acme.com"  # the (stubbed) database still holds the old value
    assert vendor_numbers.list_numbers()["vendors"][0]["emails"] == "planner@acme.com"


def test_bulk_emails_nothing_written_when_one_is_wrong(data):
    rows = [{"line": 2, "code": "400186", "emails": "ok@acme.com"}, {"line": 3, "code": "400200", "emails": "bad"}]
    with pytest.raises(ValueError, match="Nothing was saved.*Row 3.*Scenario 4"):
        vendor_numbers.bulk(rows, apply=True)
    assert data["writes"]["emails"] == []
    assert vendor_numbers.bulk(rows[:1], apply=True)["emails_updated"] == 1


def test_a_mistyped_indian_number_is_refused():
    from backend.po_followup.services.vendor_numbers import check_numbers

    assert check_numbers("7200081289", 1) == "7200081289"
    assert check_numbers("+91 72000 81289", 1) == "7200081289"
    assert check_numbers("07200081289", 1) == "7200081289"
    for bad in ("72000081289", "+9172000081289", "917200081", "1234567890"):
        with pytest.raises(ValueError, match="not a 10-digit Indian mobile"):
            check_numbers(bad, 1)
