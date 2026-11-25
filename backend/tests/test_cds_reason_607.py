"""TDM#607 and #614: diagnostics for unvalued swaps and CDS.

A current CDS without terms is shown in coverage and C03 with ``cds_no_data`` and an empty MV, even if the report
gives NPV in the row's currency or in RUB. TRS and historic CDS runs retain the report-value diagnostics of #607.
Synthetic rows on the golden market of test_positions_v1."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from psycopg.rows import dict_row
from test_positions_v1 import DATE, ctx, run  # noqa: F401 - the module's market

from desk import store
from desk.api import concepts_run, coverage
from desk.publish import desk_run
from desk.valuation.fallback import fx_rate_cbr


def cds(pid, ccy="USD", value=None, **kw):
    x = {"x_product_group": "cds", "x_product": "CreditDefaultSwap"}
    if value is not None:
        x.update({"x_npv_ccy_value": value, "x_npv_ccy": ccy})
    return {"position_id": pid, "asset_class": "otc", "source_instrument_id": pid, "ccy": ccy,
            "amount_ccy": "10000000", **x, **kw}


ROWS = [cds("D|CDS-USD", value="-125000"), cds("D|CDS-NONE"), cds("D|CDS-ZWG", "ZWG", value="1000"),
        cds("D|CDS-RUB", "RUB", mv_rub="2000"),
        {"position_id": "D|IRS", "asset_class": "otc", "source_instrument_id": "IRS-1", "ccy": "RUB",
         "amount_ccy": "100000000", "x_product_group": "irs", "x_product": "Swap"}]
OFF = [cds("E|CDS-OFF")]  # a book whose report gives no derivative a value: the adapter's report value looks off


def _set(con, book, rows, sha):
    d = date.fromisoformat(DATE)
    con.execute("INSERT INTO inbox_file (sha256, path, name, size_bytes, mtime) VALUES (%s, 'p.csv', 'p.csv', 1,"
                " now())", (sha,))
    return store.store_inbox_set(
        con, book_id=book, valid_date=d, source="positions_csv", priority=None, report_date=d, report_ts=None,
        seq=None, generated_at=None, file_mtime=datetime.now(UTC), file_name="p.csv", sha256=sha,
        adapter_fingerprint="fp", unit_hash="u" + book, rows=[{"_src": f"L{i}", "book_id": book, **r}
                                                         for i, r in enumerate(rows)])


@pytest.fixture
def published(ctx, store_url):  # noqa: F811 - the module fixture
    with store.connect(store_url) as con:
        _set(con, "D", ROWS, "d" * 64)
        _set(con, "E", OFF, "e" * 64)
        inp = store.positions_for(con, date.fromisoformat(DATE))
        _, positions = run(ctx, inp)
        rid, status, _ = desk_run.publish(con, ctx, positions, prev=None, inputs=inp)
        assert status == "complete"
        con.row_factory = dict_row
        yield con, con.execute("SELECT * FROM run WHERE run_id = %s", (rid,)).fetchone()


def test_report_value_reasons_for_trs():
    c = coverage.cause
    assert c("no_reported_value", "TRS", None, None)[0] == "report_no_value"
    assert c("no_reported_value", "TRS", None, None, report_value_on=False)[0] == "report_value_off"
    assert c("no_reported_value", "TRS", None, None, report_value=True)[0] == "run_before_report_value"
    assert c("no_fx_rate", "TRS", None, None)[0] == "report_value_no_rate"
    for kind in ("IRS", "CCS"):  # core values them from their terms: they do wait for them
        assert c("no_reported_value", kind, None, None)[0] == "swap_terms"
    assert c("no_fx_rate", "OTC", None, None)[0] == "no_fx_rate"  # another kind: as before
    why = {k: coverage.explain(f"{code}: x", "TRS", None, None, report_value=v, report_value_on=on)
           for k, code, v, on in (("none", "no_reported_value", False, True), ("off", "no_reported_value", False, False),
                                  ("old", "no_reported_value", True, True), ("rate", "no_fx_rate", True, True))}
    assert why["none"]["why"].startswith("в отчёте позиций нет стоимости (NPV)")
    assert why["off"]["fix"]["label"] == "Оценка и риск из отчёта о позициях" and "выключена" in why["off"]["why"]
    assert "до версии 0.2.19" in why["old"]["why"] and why["old"]["fix"]["label"] == "История прогонов"
    assert "курса ЦБ" in why["rate"]["why"] and why["rate"]["short"] == "нет курса"
    assert all("условий сделки (trade_terms)" not in w["why"] for w in why.values())


def test_the_run_shows_cds_without_terms_with_empty_mv(published):
    con, r = published
    u = coverage.unvalued(con, r, futures_by_ticker=False)
    g = {x["key"]: x for x in u["groups"]}
    cds_ids = {"D|CDS-USD", "D|CDS-RUB", "D|CDS-NONE", "D|CDS-ZWG", "E|CDS-OFF"}
    assert {k for k, v in g.items() if v["kind"] == "CDS"} == {"cds_no_data|CDS"}
    assert set(g["cds_no_data|CDS"]["ids"]) == cds_ids
    assert g["swap_terms|IRS"]["ids"] == ["D|IRS"]
    assert coverage.report_value_by_book(con, r["position_set_ids"]) == {"D": True, "E": False}
    assert coverage.report_value_by_book(con, []) == {}
    data = concepts_run.assemble(con, r)
    pos = {p["id"]: p for p in data["positions"]}
    for pid in cds_ids:
        p = pos[pid]
        assert p["notValued"] and p["mvRub"] is None
        why = p["extra"]["why"]
        assert why["key"] == "cds_no_data" and why["short"] == "нет данных"
        assert "нет условий сделки" in why["detail"]
    assert "нет курса ZWG/RUB" in pos["D|CDS-ZWG"]["extra"]["why"]["detail"]
    assert pos["D|IRS"]["extra"]["why"]["key"] == "swap_terms"


def test_a_run_before_0_2_19_says_so(published, ctx):  # noqa: F811
    """A run of an older program left a CDS with its value in the row unvalued (``no_reported_value``): the row's value
    is there, so the reason is the old run, not the report."""
    _con, _r = published
    x = {"ccy": "USD", "asset_class": "otc", "x": {"x_product_group": "cds", "x_npv_ccy_value": -1.0, "x_npv_ccy": "USD"}}
    assert coverage.why_of("no_reported_value: otc CDS: no mv_rub", x, None, None, True)["key"] == \
        "run_before_report_value"
    assert fx_rate_cbr(ctx, "USD") is not None
