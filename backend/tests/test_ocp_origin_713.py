"""TDM#713: where a figure of the published OCP derived from positions comes from, down to the first source
(``desk.api.ocp_origin``), on synthetic data.

A book with no positions in the current set that has a position set carried forward from an earlier date and a cash
balance carried forward from an older one: its report is too old, so the published figure is derived from positions
(``derived_stale_report``). The run's ``/ocp`` gives the figure's items with the date and file of the set of each, the
items sum to the figure, and the block the run stored has none of it (a run computed before the change shows it too).
Every number is invented."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_ocp_run import BOOKS, DATE, SNAPS, _csv, _run, _scan

from desk import store
from desk.api import create_app
from desk.api.ocp_origin import MAX_ITEMS
from desk.inbox import scan
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare
from desk.settings import Settings


@pytest.fixture(autouse=True)
def _fresh_process():
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    yield
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(SNAPS), DATE.isoformat())


IDLE = BOOKS + "N1;Idle book;Rates;DERIVS;ENT1;;;1;\n"
EARLY_POSITIONS = """book_id;position_id;asset_class;source_instrument_id;isin;ccy;quantity;amount_ccy;value_date;deal_id
N1;N1|FWD;fx;USD-FWD;;USD;;4000000;2026-12-17;
N1;N1|SPOT;fx;EUR;;EUR;;250000;;
"""
OLD_CASH = """entity;book_id;ccy;account_id;balance_ccy
ENT1;N1;USD;ACC-1;7000000
ENT1;N1;USD;ACC-2;-500000
ENT1;N1;RUB;ACC-1;1
"""
OLD_REPORT = """entity;book_id;ccy;ocp_ccy;ocp_rub;rate_rub_per_unit
ENT1;N1;USD;100;;
"""


def _stale_run(store_url, ctx, tmp_path):
    """N1: a report two business days old, positions of the 9th, cash of the 1st; D1, T1, X1 as the other tests."""
    with store.connect(store_url) as con:
        _scan(con, tmp_path, report=False, books=IDLE)
        inbox = tmp_path / "in"
        _csv(inbox / "positions_20261012.csv", EARLY_POSITIONS)
        _csv(inbox / "cash_balances_20261001.csv", OLD_CASH)
        _csv(inbox / "ocp_20261013.csv", OLD_REPORT)
        s = Settings(snapshot_cache=SNAPS, inbox_dirs=(inbox,), inbox_archive=tmp_path / "archive",
                     private_adapter=None, private_adapter_config=tmp_path / "cfg", inbox_poll_seconds=0,
                     inbox_lookback_days=0)
        assert scan.scan(con, s).ok
        rid, _ = _run(con, ctx)
        block = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'ocp'", (rid,)).fetchone()[0]
        sets = {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT s.book_id, s.valid_date, s.source_name FROM run r JOIN position_set s ON s.set_id = ANY(r.position_set_ids)"
            " WHERE r.run_id = %s", (rid,))}
    return rid, block, sets


def test_a_derived_line_shows_its_positions_and_balances_with_the_dates_of_their_sets(store_url, ctx, tmp_path):
    rid, block, sets = _stale_run(store_url, ctx, tmp_path)
    assert "origins" not in block  # what the run stored: nothing of the breakdown (a run before the change)
    o = TestClient(create_app(store_url)).get(f"/api/v1/runs/{rid}/ocp").json()
    line = {(x["book_id"], x["ccy"]): x for x in o["published"]["by_book"]}[("N1", "USD")]
    assert line["source"] == "derived_stale_report"
    org = {(x["book_id"], x["ccy"]): x for x in o["origins"]}[("N1", "USD")]
    # the items are the figure: the forward at its notional, the cash balance of both accounts
    assert org["amount"] == line["amount"] == pytest.approx(4_000_000 + 7_000_000 - 500_000)
    assert org["sum"] == pytest.approx(line["amount"]) and org["matches"] and org["rest"] is None
    by = {i["kind"]: i for i in org["items"]}
    assert set(by) == {"position", "cash"} and len(org["items"]) == 2
    pos, cash = by["position"], by["cash"]
    # the position: its instrument, kind and the set of the book it was carried forward from
    assert pos["position_id"] == "N1|FWD" and pos["component"] == "fx_legs" and pos["amount"] == 4_000_000
    assert pos["name"] and pos["instr_type"] == "FX"
    valid, file_name = sets["N1"]
    assert pos["set"]["valid_date"] == valid.isoformat() and pos["set"]["carried"] is True
    assert pos["set"]["file_name"] == file_name == "positions_20261012.csv"
    assert pos["set"]["age_days"] == (DATE - valid).days > 0
    # the balance: its accounts and the set of the balances, carried forward from the 1st
    assert cash["key"] == "CASH|N1|USD" and cash["component"] == "cash" and cash["basis"] == "balance"
    assert cash["amount"] == 6_500_000 and cash["restored"] is True
    assert sorted((a["account_id"], a["balance_ccy"]) for a in cash["accounts"]) == [("ACC-1", 7_000_000), ("ACC-2", -500_000)]
    assert [(s["valid_date"], s["carried"], s["file_name"]) for s in cash["sets"]] == [
        ("2026-10-01", True, "cash_balances_20261001.csv")]
    # the other currency of the book: only its own items (EUR spot: cash to be settled)
    eur = {(x["book_id"], x["ccy"]): x for x in o["origins"]}[("N1", "EUR")]
    assert [i["position_id"] for i in eur["items"]] == ["N1|SPOT"] and eur["matches"]
    # every derived line has a breakdown that sums to it; a line from the report has none
    derived = {(x["book_id"], x["ccy"]) for x in o["published"]["by_book"] if x["source"] != "report"}
    assert {(x["book_id"], x["ccy"]) for x in o["origins"]} == derived
    assert all(x["matches"] for x in o["origins"])


def test_a_figure_with_many_items_lists_the_largest_and_sums_the_rest(store_url, ctx, tmp_path):
    from desk.api import ocp_origin
    rows = [{"key": f"P{i}", "component": "cash", "book_id": "N1", "ccy": "USD", "amount": float(i + 1),
             "position_id": f"P{i}", "basis": "amount", "note": "", "pv": None, "entity": "ENT1"}
            for i in range(MAX_ITEMS + 5)]
    block = {"published": {"by_book": [{"entity": "ENT1", "book_id": "N1", "books": ["N1"], "ccy": "USD",
                                        "amount": sum(r["amount"] for r in rows), "source": "derived"}]},
             "lines": rows}
    rid, _, _ = _stale_run(store_url, ctx, tmp_path)
    with store.connect(store_url) as con:
        got, = ocp_origin.origins(con, rid, block)
    assert len(got["items"]) == MAX_ITEMS and got["rest"]["count"] == 5
    assert got["items"][0]["amount"] == MAX_ITEMS + 5  # the largest first
    assert got["rest"]["amount"] == sum(range(1, 6)) and got["matches"]


def test_a_block_without_derived_lines_or_without_the_runs_lines_has_no_breakdown(store_url, ctx, tmp_path):
    from desk.api import ocp_origin
    rid, block, _ = _stale_run(store_url, ctx, tmp_path)
    with store.connect(store_url) as con:
        assert ocp_origin.origins(con, rid, None) == []
        assert ocp_origin.origins(con, rid, {**block, "lines": []}) == []
        report_only = {**block, "published": {"by_book": [x for x in block["published"]["by_book"]
                                                          if x["source"] == "report"]}}
        assert ocp_origin.origins(con, rid, report_only) == []
