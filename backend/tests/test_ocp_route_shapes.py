"""The run's ``/ocp`` route on the shapes a desk's real run has (0.2.21: the OCP page's drill-down «из чего
складывается ОВП» answered 500 for every book and date). The block is one response: an element the model did not know
failed it whole. Here the run holds what the synthetic runs of the other tests did not: a book of a reported entity
absent from the OCP report whose OCP is only the PV of one NDF far leg (nearly the whole line: gap
``derivative_share``, DEC18), the report's CNH line (CNY, TDM#601) and the OCP transfers; the engine's day gaps on entity-level report units, a book without reference data and cash balances
older than the base. Every gap code the engine can emit is one the model accepts, and an element it still does not
accept is left out of the response, named by its path (never a value), instead of failing the route. Every label and
number is invented."""

from __future__ import annotations

import logging
import typing
from datetime import date

import pytest
import test_ocp_run as R
from fastapi.testclient import TestClient
from test_ocp_base_plus_day import BOOKS, POLICY, B, D
from test_ocp_futures_588 import cash_row

from desk import store
from desk.api.app import OcpBlockMismatch, create_app, ocp_out
from desk.api.models import OcpGap
from desk.publish import funding_run, ocp_run
from desk.rules import ocp as O

ctx = R.ctx  # the module's market (test_ocp_run)
_fresh_process = R._fresh_process

NDF_TERMS = R.TERMS + ("NDF-1;;1;N1;ENT1;N1|NDF;fx_forward;NDF;2026-10-09;2026-12-17;USD;RUB;sell;20000000;1650000000;"
                       "82.5;1;\n")


def test_a_book_absent_from_the_report_with_one_ndf_serves_the_drill_down(store_url, ctx, tmp_path, monkeypatch):
    """N1 (entity ENT1, reported) has no row in the OCP report: base 0 + the PV of its NDF far leg, 20 million
    dollars, the whole line: gap ``derivative_share``, which the route's model did not know (500)."""
    monkeypatch.setattr(R, "TERMS", NDF_TERMS)
    books = R.BOOKS + "N1;NDF book;Rates;DERIVS;ENT1;;;1;\n"
    positions = R.POSITIONS + "N1;N1|NDF;fx;USD-NDF;;USD;;-20000000;2026-12-17;NDF-1\n"
    with store.connect(store_url) as con:
        R._scan(con, tmp_path, books=books, positions=positions, cash=R.CASH + "ENT1;D1;CNH;MAIN;20000\n",
                ocp=R.OCP + "ENT1;D1;CNH;6000000;;\n")
        rid, _ = R._run(con, ctx)
        block, = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'ocp'", (rid,)).fetchone()
    assert block["published"]["basis"] == "report_base_plus_day"
    assert "derivative_share" in {g["code"] for g in block["gaps"]}
    r = TestClient(create_app(store_url), raise_server_exceptions=False).get(f"/api/v1/runs/{rid}/ocp")
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["left_out"] == [] and "derivative_share" in {g["code"] for g in o["gaps"]}
    lines = {(x["book_id"], x["ccy"]): x for x in o["published"]["by_book"]}
    n1 = lines[("N1", "USD")]
    assert n1["base_kind"] == "book_absent" and n1["parts"]["base"] == 0
    assert [x["position_id"] for x in n1["derivative_share"]] == ["N1|NDF"]  # served, as the drill-down marks it
    assert ("D1", "CNY") in lines and not any(c == "CNH" for _, c in lines)
    assert {(t["book_id"], t["ccy"]) for t in o["transfers"]} >= {("D1", "USD"), ("X1", "USD")}


def _block(r: O.OcpResult, on: date) -> dict:
    """The run's block of an engine result as ``publish.ocp_run`` stores it (JSON-able)."""
    return funding_run._jsonable(ocp_run.empty_block(
        ocp_run.COMPUTED, "ОВП", on, policy=POLICY.lineage(),
        published={"basis": POLICY.published.value, "base_date": None,
                   "by_ccy": {c: {**x, "rate": None, "total_rub": None}
                              for c, x in O.published_by_currency(r.published).items()}, "by_book": r.published},
        gaps=r.gaps, lines=[x.as_json() for x in r.lines], reconciliation=r.comparisons,
        derivatives_check=r.derivatives_check, derivatives_pv=r.derivatives_pv, outside_report=r.outside_report,
        transfers=r.transfers, totals={}))


def report_row(pid: str, book: str, group: str, delta: dict, asset_class: str = "otc") -> dict:
    return {"position_id": pid, "book_id": book, "instr_type": "OTC", "fx_delta": delta,
            "extra": {"reported": {"asset_class": asset_class, "x": {"x_product_group": group},
                                   "value_date": "2026-12-15"}}}


SHAPES_BOOKS = {**BOOKS, "Z2": {"book_id": "Z2", "book_type": "DERIVS", "entity": "ENT2", "ocp_to": None}}
# ENT1 reported at the entity level (its rows without a book); ENT2 by book: Z1 has a row, Z2 none (base 0)
SHAPES_UNITS = [{"scope": {"entity": "ENT1"}, "valid_date": B, "set_id": 41},
                {"scope": {"entity": "ENT2", "book_id": "Z1"}, "valid_date": B, "set_id": 42}]
SHAPES_REPORT = [
    {"entity": "ENT1", "ccy": "USD", "ocp_ccy": "3000000", "derivatives_ccy": {"ndf": "-2000000"},
     "x": {"x_derivative_types": [{"type": "NDF", "kind": "ndf", "kept": False, "lines": 2, "amount_ccy": "-2000000"}]},
     "_set_id": 41, "_src": "row 1"},
    {"entity": "ENT1", "ccy": "CNY", "ocp_ccy": "1000000", "_set_id": 41, "_src": "row 2",
     "_ccy_reported": {"ccy": "CNH"}},
    {"entity": "ENT2", "book_id": "Z1", "ccy": "USD", "ocp_ccy": "500000", "derivatives_ccy": {"ndf": "0"},
     "_set_id": 42, "_src": "row 3"}]
SHAPES_ROWS = [
    report_row("D1|NDF", "D1", "ndf", {"USD": -2_000_000.0}),
    report_row("Z2|NDF", "Z2", "ndf", {"USD": -20_000_000.0}),  # the far leg of an NDF: the whole OCP of Z2
    {"position_id": "Z9|FX", "book_id": "Z9", "instr_type": "FX", "fx_delta": {"EUR": 15_000_000.0}},  # no reference
]


def z2_cash(amount: float, valid: date) -> dict:
    return {**cash_row(amount, valid), "book_id": "Z2", "entity": "ENT2"}


def test_every_shape_of_the_engine_fits_the_route_model():
    """The engine's output on the other shapes of a real run fits ``RunOcp`` whole (``left_out`` empty): an entity-level
    report unit (no book in its rows), a position in a book without reference data, an NDF that is the whole OCP of a
    book absent from the report, with cash balances older than the base, the base older than the run."""
    old = O.BaseDay(B, run_id=1, trade_rows=[], cash_rows=[z2_cash(9000, date(2026, 10, 13))])
    codes = set()
    for on, days in ((B, None), (D, {B: old})):
        r = O.compute_ocp(SHAPES_ROWS, SHAPES_BOOKS, on, report_rows=SHAPES_REPORT, report_units=SHAPES_UNITS,
                          policy=POLICY, cash=[z2_cash(12_500, on)], days=days,
                          rates={"RUB": 1.0, "USD": 80.0, "EUR": 90.0, "CNY": 11.0})
        out = ocp_out(1, _block(r, on))
        assert out.left_out == [] and len(out.published.by_book) == len(r.published)
        assert [g.code for g in out.gaps] == [g["code"] for g in r.gaps]
        assert any(x.book_id is None and x.level == "entity" for x in out.reconciliation)
        z2 = {(x.book_id, x.ccy): x for x in out.published.by_book}[("Z2", "USD")]
        assert z2.base_kind == "book_absent" and [x["position_id"] for x in z2.derivative_share] == ["Z2|NDF"]
        codes |= {g.code for g in out.gaps}
    assert {"derivative_share", "balances_older_than_base"} <= codes


def test_every_gap_code_of_the_engine_is_in_the_model():
    """A code the engine emits and the model does not list failed the whole route (0.2.21)."""
    engine = {"no_report", "stale_report", "outside_without_terms", "outside_double_count", "books_outside_report",
              "off_report_excluded", *O.DAY_GAP_TEXT}
    assert engine <= set(typing.get_args(OcpGap.model_fields["code"].annotation))


def test_an_element_the_model_does_not_accept_is_left_out_not_the_route(caplog):
    """One element of the block the model does not accept (a gap code of a newer engine, a line without its entity)
    is left out of the response and named by its path and error type; the rest is served. The log says the same and
    never a value (the figures are real positions)."""
    r = O.compute_ocp(SHAPES_ROWS, SHAPES_BOOKS, B, report_rows=SHAPES_REPORT, report_units=SHAPES_UNITS,
                      policy=POLICY, cash=[z2_cash(12_500, B)])
    block = _block(r, B)
    n_gaps, n_lines = len(block["gaps"]), len(block["published"]["by_book"])
    block["gaps"].insert(0, {"code": "a_code_of_tomorrow", "text": "…", "items": []})
    block["published"]["by_book"][0]["amount"] = None
    block["published"]["by_book"][0]["entity"] = None
    block["published"]["by_ccy"]["USD"]["total"] = "x"
    secret = block["published"]["by_book"][1]["amount"] = 987_654_321.5
    with caplog.at_level(logging.WARNING, logger="desk.api"):
        out = ocp_out(1, block)
    assert sorted(out.left_out) == ["gaps.0.code: literal_error", "published.by_book.0.amount: float_type",
                                    "published.by_book.0.entity: string_type", "published.by_ccy.USD.total: float_parsing"]
    assert len(out.gaps) == n_gaps and len(out.published.by_book) == n_lines - 1
    assert "USD" not in out.published.by_ccy and out.published.basis == "report_base_plus_day"
    assert block["gaps"][0]["code"] == "a_code_of_tomorrow"  # the stored block is left as it is
    assert "gaps.0.code" in caplog.text and "987" not in caplog.text and "a_code_of_tomorrow" not in caplog.text
    assert any(x.amount == secret for x in out.published.by_book)
    assert len(out.published_rows) == n_lines - 1  # the page rows leave the same line out
    # a required field that does not fit still fails: nothing sensible to serve, and no value in the error either
    bad = _block(r, B)
    bad["lines"] = None
    bad["text"] = 987_654_321.5
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="desk.api"), pytest.raises(OcpBlockMismatch) as e:
        ocp_out(1, bad)
    assert "987" not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__
    assert "lines" in caplog.text and "987" not in caplog.text
