"""TDM#607: a swap core does not value from terms (a CDS, a TRS) that is not valued says why in words of its own, not
«своп ждёт условий сделки»: no value in the positions report; the value there but no CBR rate of its currency; the
report's value off in the adapter (no derivative row of the report has one); a run made before 0.2.19 (the value is in
the row but was not taken). Only IRS and CCS «wait for the terms». A CDS in USD with its value in the row is valued at
the CBR rate up to the C03 data. Synthetic rows on the golden market of test_positions_v1."""

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


def test_each_reason_of_a_swap_without_terms():
    c = coverage.cause
    assert c("no_reported_value", "CDS", None, None)[0] == "report_no_value"
    assert c("no_reported_value", "TRS", None, None, report_value_on=False)[0] == "report_value_off"
    assert c("no_reported_value", "CDS", None, None, report_value=True)[0] == "run_before_report_value"
    assert c("no_fx_rate", "CDS", None, None)[0] == "report_value_no_rate"
    for kind in ("IRS", "CCS"):  # core values them from their terms: they do wait for them
        assert c("no_reported_value", kind, None, None)[0] == "swap_terms"
    assert c("no_fx_rate", "OTC", None, None)[0] == "no_fx_rate"  # another kind: as before
    why = {k: coverage.explain(f"{code}: x", "CDS", None, None, report_value=v, report_value_on=on)
           for k, code, v, on in (("none", "no_reported_value", False, True), ("off", "no_reported_value", False, False),
                                  ("old", "no_reported_value", True, True), ("rate", "no_fx_rate", True, True))}
    assert why["none"]["why"].startswith("в отчёте позиций нет стоимости (NPV)")
    assert why["off"]["fix"]["label"] == "Оценка и риск из отчёта о позициях" and "выключена" in why["off"]["why"]
    assert "до версии 0.2.19" in why["old"]["why"] and why["old"]["fix"]["label"] == "История прогонов"
    assert "курса ЦБ" in why["rate"]["why"] and why["rate"]["short"] == "нет курса"
    assert all("условий сделки (trade_terms)" not in w["why"] for w in why.values())


def test_the_run_names_the_reason_and_values_the_usd_cds(published):
    con, r = published
    u = coverage.unvalued(con, r, futures_by_ticker=False)
    g = {x["key"]: x for x in u["groups"]}
    assert g["report_no_value|CDS"]["ids"] == ["D|CDS-NONE"]
    assert g["report_value_no_rate|CDS"]["ids"] == ["D|CDS-ZWG"]
    assert g["swap_terms|IRS"]["ids"] == ["D|IRS"]
    assert not any(k.startswith("swap_terms|CDS") for k in g)
    # the book whose report gives no derivative a value: the report value looks off in the adapter for it
    assert g["report_value_off|CDS"]["ids"] == ["E|CDS-OFF"]
    assert coverage.report_value_by_book(con, r["position_set_ids"]) == {"D": True, "E": False}
    assert coverage.report_value_by_book(con, []) == {}
    # the C03 data: the USD CDS at its value x the CBR rate, the RUB one at its mv_rub, the reason of the others
    data = concepts_run.assemble(con, r)
    pos = {p["id"]: p for p in data["positions"]}
    usd = pos["D|CDS-USD"]
    assert not usd.get("notValued") and usd["extra"]["reported_ccy"]["ccy"] == "USD"
    assert usd["mvRub"] == pytest.approx(-125_000 * usd["extra"]["reported_ccy"]["rate"])
    assert pos["D|CDS-RUB"]["mvRub"] == pytest.approx(2_000)
    assert pos["D|CDS-NONE"]["extra"]["why"]["key"] == "report_no_value"
    assert pos["D|CDS-ZWG"]["extra"]["why"]["key"] == "report_value_no_rate"
    assert pos["D|IRS"]["extra"]["why"]["key"] == "swap_terms"
    assert pos["E|CDS-OFF"]["extra"]["why"]["key"] == "report_value_off"


def test_a_run_before_0_2_19_says_so(published, ctx):  # noqa: F811
    """A run of an older program left a CDS with its value in the row unvalued (``no_reported_value``): the row's value
    is there, so the reason is the old run, not the report."""
    con, r = published
    x = {"ccy": "USD", "asset_class": "otc", "x": {"x_product_group": "cds", "x_npv_ccy_value": -1.0, "x_npv_ccy": "USD"}}
    assert coverage.why_of("no_reported_value: otc CDS: no mv_rub", x, None, None, True)["key"] == \
        "run_before_report_value"
    assert fx_rate_cbr(ctx, "USD") is not None
