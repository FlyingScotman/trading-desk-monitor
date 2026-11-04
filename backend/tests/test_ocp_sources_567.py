"""TDM#567: one serializer for a published OCP line on every route (D10), a day of another methodology and a line
whose report still holds the derivatives are named (D11, D24); synthetic books only."""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from test_ocp_history import insert, line

from desk import store
from desk.api import create_app
from desk.api.ocp_line import book_type, method_note, ocp_line
from desk.publish.ocp_run import empty_block


def block(status, lines, basis="report_plus_outside", books=None, on="2026-11-04"):
    """A complete block (the ``/ocp`` route validates it) with the given published lines."""
    return empty_block(status, "synthetic", date.fromisoformat(on), published={"basis": basis, "base_date": None, "by_ccy": {}, "by_book": lines},
                       books=books or {})


PARTS = {"base": 1e6, "derivatives": 2e5, "bonds": 1e3, "trades": 5e4, "outside": 0.0}


def v3(book, ccy, amount, kind="without_derivatives", **kw):
    return {**line(book, ccy, amount, valid="2026-11-02", **kw), "base_date": "2026-11-02", "set_id": 7,
            "base_kind": kind, "parts": PARTS, "base_older": False}


@pytest.fixture
def three(store_url):
    """A current day on the current methodology with one line whose report still has the derivatives, an old day
    before 03.11.2026, and a book without a recorded type (with and without a bond)."""
    with store.connect(store_url) as con:
        old = insert(con, "2026-11-02", ocp=block("computed", [line("B1", "USD", 1e6)], on="2026-11-02"))
        cur = insert(con, "2026-11-04", ocp=block("computed", [
            v3("B1", "USD", 1.25e6), v3("B2", "EUR", -3e5, kind="with_derivatives"),
            {**v3(None, "CNY", 5e6, books=["B1", "B2"]), "components": ["ndf"]}],
            basis="report_base_plus_day", books={"B7": {"name": "Only OCP", "entity": "ENT1", "desk": "FX"}}),
            books=[{"book_id": "B1", "name": "One", "entity": "ENT1", "desk": "FX"},
                   {"book_id": "B2", "name": "Two", "entity": "ENT1", "desk": "FX", "book_type": "HTM"}])
    return TestClient(create_app(store_url)), old, cur


def test_the_same_line_on_three_routes(three):
    c, _old, cur = three
    page = c.get("/api/v1/concepts/run", params={"run_id": cur}).json()["OCP"]
    hist = c.get("/api/v1/history/ocp", params={"run_id": cur, "from": "2026-11-04"}).json()["days"][0]
    run = c.get(f"/api/v1/runs/{cur}/ocp").json()
    assert len(page["byBook"]) == len(hist["lines"]) == len(run["published_rows"]) == 3
    for a, b, d in zip(page["byBook"], hist["lines"], run["published_rows"], strict=True):
        assert a == b == d
    assert page["byBook"][0]["baseKind"] == "without_derivatives" and page["byBook"][2]["components"] == ["ndf"]
    assert page["methodNote"] == hist["methodNote"] == run["method_note"]


def test_with_derivatives_line_is_named_and_the_current_one_is_not(three):
    c, _old, cur = three
    note = c.get("/api/v1/history/ocp", params={"run_id": cur, "from": "2026-11-04"}).json()["days"][0]["methodNote"]
    assert note.startswith("не по текущей методике: отчёт ещё с деривативами") and "B2" in note and "B1" not in note
    assert method_note("report_base_plus_day", "2026-11-04", [ocp_line(v3("B1", "USD", 1.0))]) is None
    assert method_note(None, "2026-11-04", []) is None


def test_a_day_of_the_old_methodology_is_marked(three):
    c, old, cur = three
    days = {d["date"]: d for d in
            c.get("/api/v1/history/ocp", params={"run_id": cur, "from": "2026-11-01"}).json()["days"]}
    assert days["2026-11-02"]["basis"] == "report_plus_outside"
    note = days["2026-11-02"]["methodNote"]
    assert note.startswith("другая методика") and "03.11.2026" in note
    assert c.get(f"/api/v1/runs/{old}/ocp").json()["method_note"] == days["2026-11-02"]["methodNote"]


def test_book_type_is_derived_alike_and_each_day_has_its_own_reference(three):
    c, _old, cur = three
    d = c.get("/api/v1/history/ocp", params={"run_id": cur, "from": "2026-11-01"}).json()
    assert book_type(None, True) == "TRADING" and book_type("", False) == "DERIVS" and book_type("HTM", True) == "HTM"
    day = {x["date"]: x for x in d["days"]}["2026-11-04"]
    assert day["books"]["B2"]["type"] == "HTM" and day["books"]["B1"]["type"] == "DERIVS"  # no bond in the run
    assert day["books"]["B7"] == {"name": "Only OCP", "entity": "ENT1", "desk": "FX", "type": "—", "ocpOnly": True}
    assert d["books"]["B1"]["type"] == "DERIVS"
    # the run's page data says the same of the same book
    book = c.get("/api/v1/concepts/run", params={"run_id": cur}).json()["BOOKS"]
    assert book["B1"]["bookType"] == day["books"]["B1"]["type"] and book["B2"]["bookType"] == "HTM"
