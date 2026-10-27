"""R45: «Чего нет в данных» made actionable (``desk.api.coverage``): the run's unvalued positions by cause and
instrument kind with counts, amounts in RUB where the run has the rate, the report's MV, what to do and the ids for the
drill-down; closed (zero) rows counted apart; the currency of an unvalued row (OMR) in the run's rates, so that the
page does not warn of a missing rate it never needed. A positions set of the private contract v1 on the golden
fixtures (test_positions_v1's market), synthetic rows only."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from psycopg.rows import dict_row
from test_positions_v1 import DATE, ctx, run  # noqa: F401 - the module's market

from desk import store
from desk.api import concepts, coverage
from desk.publish import desk_run

ROWS = [
    {"position_id": "D|IRS1", "asset_class": "otc", "source_instrument_id": "IRS-1", "ccy": "RUB",
     "amount_ccy": "100000000", "x_product_group": "irs", "x_product": "Swap"},
    {"position_id": "D|IRS2", "asset_class": "otc", "source_instrument_id": "IRS-2", "ccy": "RUB",
     "amount_ccy": "50000000", "x_product_group": "irs", "x_product": "Swap"},
    {"position_id": "D|IRS0", "asset_class": "otc", "source_instrument_id": "IRS-0", "ccy": "RUB", "quantity": "0",
     "amount_ccy": "0", "x_product_group": "irs", "x_product": "Swap"},  # closed: not a gap
    {"position_id": "D|BF", "asset_class": "otc", "source_instrument_id": "BF-1", "ccy": "RUB", "amount_ccy": "10",
     "x_product_group": "bond_future", "x_product": "FutureBond"},
    {"position_id": "D|REPO", "asset_class": "other", "source_instrument_id": "R-1", "ccy": "RUB",
     "amount_ccy": "25000000", "x_product_group": "repo", "x_product": "RepoTerm"},  # REPO (MM), TDM#361
    {"position_id": "D|OMR", "asset_class": "otc", "source_instrument_id": "O-1", "ccy": "OMR", "amount_ccy": "1000"},
    {"position_id": "D|XS", "asset_class": "bond", "source_instrument_id": "XS999", "isin": "XS0000000999",
     "ccy": "RUB"},  # an ISIN the static does not know, no MV: not valued
    {"position_id": "D|FWD", "asset_class": "fx", "ccy": "USD", "amount_ccy": "-400", "value_date": "2027-01-15",
     "x_product_group": "fx_ndf"},  # valued without its trade terms: the notional as FX delta
]


@pytest.fixture
def published(ctx, store_url):  # noqa: F811 - the module fixture
    d = date.fromisoformat(DATE)
    with store.connect(store_url) as con:
        con.execute("INSERT INTO inbox_file (sha256, path, name, size_bytes, mtime) VALUES (%s, 'p.csv', 'p.csv', 1,"
                    " now())", ("c" * 64,))
        store.store_inbox_set(
            con, book_id="D", valid_date=d, source="positions_csv", priority=None, report_date=d, report_ts=None,
            seq=None, generated_at=None, file_mtime=datetime.now(UTC), file_name="p.csv", sha256="c" * 64,
            adapter_fingerprint="fp", unit_hash="u", rows=[{"_src": f"L{i}", "book_id": "D", **r}
                                                          for i, r in enumerate(ROWS)])
        store.load_positions(con, [("L1", "OFZ", "SU26238RMFS4", 10.0)], valid_date=d, source_name="tests",
                             source_sha256="-")
        inp = store.positions_for(con, d)
        _, positions = run(ctx, inp)
        rid, status, _ = desk_run.publish(con, ctx, positions, prev=None, inputs=inp)
        assert status == "complete"
        con.row_factory = dict_row
        r = con.execute("SELECT * FROM run WHERE run_id = %s", (rid,)).fetchone()
        yield con, r


def test_unvalued_by_cause_and_kind(published):
    con, r = published
    u = coverage.unvalued(con, r, futures_by_ticker=False)
    g = {x["key"]: x for x in u["groups"]}
    assert (u["total"], u["open"], u["closed"]) == (7, 6, 1)
    swaps = g["swap_terms|IRS"]
    assert swaps["n"] == 2 and swaps["notionalRub"] == pytest.approx(150e6) and swaps["mvRub"] is None
    assert sorted(swaps["ids"]) == ["D|IRS1", "D|IRS2"] and "trade_terms" in swaps["why"]
    assert swaps["reasons"] == {"no_reported_value": 2} and "report_risk" in swaps["todo"]
    assert g["closed|IRS"]["closed"] and g["closed|IRS"]["ids"] == ["D|IRS0"] and u["groups"][-1]["closed"]
    assert g["bond_derivative|BOND_FUT"]["n"] == 1
    assert g["repo|REPO"]["why"].startswith("РЕПО") and g["repo|REPO"]["notionalRub"] == pytest.approx(25e6)
    assert g["repo|REPO"]["kindLabel"] == "РЕПО"
    assert g["unmapped_instrument|BOND_NA"]["ids"] == ["D|XS"] and "ISIN" in g["unmapped_instrument|BOND_NA"]["todo"]
    omr = g["no_report_mv|OTC"]  # an OTC row without a product group: DER OTC (TDM#345)
    assert omr["ids"] == ["D|OMR"] and omr["noRate"] == [] and u["fxMissing"] == []  # the CBR rate is in the run now
    rate = next(x["rate"] for x in con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'fx'",
                                               (r["run_id"],)).fetchone()["data"] if x["ccy"] == "OMR")
    assert omr["notionalRub"] == pytest.approx(1000 * rate) and rate > 100
    assert coverage.cause("no_reported_value", "FX_FUT", None, False)[1].endswith("(выключена)")
    assert coverage.cause("core_x", "OFZ_PD", None, None)[0] == "core_x"


def test_closed_rows():
    assert coverage.closed(0.0, {"amount_ccy": 0, "mv_rub": None})
    assert not coverage.closed(0.0, {"amount_ccy": 5.0}) and not coverage.closed(None, {})
    assert not coverage.closed(3.0, {})


def test_approx_and_extras(published):
    con, r = published
    a = {x["key"]: x for x in coverage.approx(con, r)}
    fwd = a["assumed|FX_FWD|1"]  # an FX forward without its trade terms: PV 0, the notional as FX delta
    assert fwd["ids"] == ["D|FWD"] and "trade_terms" in fwd["text"] and not any("L1" in x["ids"] for x in a.values())
    x = concepts.extras(con, r)
    assert x["unvalued"]["open"] == 6 and isinstance(x["approx"], list)


def test_missing_rate_of_an_unvalued_row(published):
    """Without the rate in the run (a run before R45), the currency of the open rows is reported once, in
    ``fxMissing``; a closed row's currency is not."""
    con, r = published
    con.execute("ALTER TABLE run_reference DISABLE TRIGGER USER")  # a published run's rows are frozen
    con.execute("UPDATE run_reference SET data = (SELECT jsonb_agg(e) FROM jsonb_array_elements(data) e"
                " WHERE e->>'ccy' <> 'OMR') WHERE run_id = %s AND kind = 'fx'", (r["run_id"],))
    u = coverage.unvalued(con, r, futures_by_ticker=False)
    assert u["fxMissing"] == ["OMR"] and next(g for g in u["groups"] if "D|OMR" in g["ids"])["noRate"] == ["OMR"]
