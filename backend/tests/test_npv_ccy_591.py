"""TDM#591: a derivative in a foreign currency without terms (a CDS, a TRS, a bond future) whose report gives its value
only in the row's currency (the adapter's report risk «в валюте строки»: ``x_npv_ccy_value`` / ``x_npv_ccy``, no
``mv_rub``) is valued at that value x the CBR rate of the run's date, marked; without the rate ``no_fx_rate``; a RUB
row and a row with ``mv_rub`` are as before. A non-FX future valued so gives no OCP (TDM#588). Synthetic rows on the
golden market of test_positions_v1."""

from __future__ import annotations

import pytest
from test_positions_v1 import ctx, inputs, one, run  # noqa: F401 - the module's market

from desk.publish import tdm_run
from desk.rules import ocp as O
from desk.valuation.fallback import fx_rate_cbr


def cds(ccy="USD", value=-125_000.0, **kw):
    return {"asset_class": "otc", "ccy": ccy, "source_instrument_id": "CDS-1", "deal_id": "CDS-1",
            "x": {"x_product_group": "cds", "x_product": "CreditDefaultSwap", "x_npv_ccy_value": value,
                  "x_npv_ccy": ccy}, **kw}


def test_a_usd_cds_at_the_report_value_and_the_cbr_rate(ctx):  # noqa: F811
    cbr = fx_rate_cbr(ctx, "USD")[0]
    o = one(ctx, cds())
    v = o.valuation
    assert v is not None and v.quality == "reported_value" and v.pv_rub == pytest.approx(-125_000.0 * cbr)
    assert v.extra["reported_ccy"] == {"ccy": "USD", "value": -125_000.0, "rate": cbr, "rate_source": "CBR",
                                       "text": "стоимость из отчёта в USD, по курсу ЦБ"}
    assert any("по курсу ЦБ" in a for a in v.assumptions) and v.dv01_rub == 0.0
    row = tdm_run.direct_row(ctx, "CORP", "P1", o, cds(), None)
    assert row["fx_delta"] == {"USD": -125_000.0} and row["mv_rub"] == pytest.approx(-125_000.0 * cbr)
    assert row["extra"]["reported_ccy"]["ccy"] == "USD"
    # the OCP: a CDS is an «other» derivative at its PV in the currency
    comp, amounts, _, _ = O.classify(row, (), {"CORP": {"entity": "E"}}, ctx.business_date, O.PROVISIONAL_POLICY)
    assert comp == "other" and amounts == {"USD": -125_000.0}


def test_the_value_as_a_decimal_string_too(ctx):  # noqa: F811
    o = one(ctx, cds(value="-1000.5"))
    assert o.valuation.pv_rub == pytest.approx(-1000.5 * fx_rate_cbr(ctx, "USD")[0])


def test_without_the_cbr_rate_no_fx_rate(ctx):  # noqa: F811
    o = one(ctx, cds("ZWG"))  # a currency the CBR does not quote
    assert o.valuation is None and o.reason == "no_fx_rate" and "ZWG" in o.detail


def test_a_rub_row_and_a_row_with_mv_rub_are_unchanged(ctx):  # noqa: F811
    o = one(ctx, {"asset_class": "otc", "ccy": "RUB", "source_instrument_id": "S-1", "mv_rub": 1_000.0})
    assert o.valuation.pv_rub == 1_000.0 and "reported_ccy" not in o.valuation.extra
    o = one(ctx, cds(mv_rub=2_000.0))  # mv_rub wins over the value in the currency
    assert o.valuation.pv_rub == 2_000.0 and "reported_ccy" not in o.valuation.extra
    o = one(ctx, {"asset_class": "otc", "ccy": "USD", "source_instrument_id": "S-2"})  # nothing at all: as before
    assert o.valuation is None and o.reason == "no_reported_value"


def test_a_bond_future_valued_so_gives_no_ocp(ctx):  # noqa: F811
    x = cds(value=50_000.0)
    x["x"]["x_product_group"] = "bond_future"
    o = one(ctx, x)
    assert o.valuation is not None and o.valuation.extra["reported_ccy"]["value"] == 50_000.0
    row = tdm_run.direct_row(ctx, "CORP", "P1", o, x, None)
    comp, amounts, _, note = O.classify(row, (), {"CORP": {"entity": "E"}}, ctx.business_date, O.PROVISIONAL_POLICY)
    assert comp is None and amounts == {} and "не на валюту" in note
    assert O.derivative_kind(row, ()) is None


def test_an_fx_forward_without_mv_rub_takes_the_value_in_its_currency(ctx):  # noqa: F811
    x = {"asset_class": "fx", "ccy": "USD", "amount_ccy": 1_000_000.0, "value_date": "2027-01-15",
         "x": {"x_product_group": "fx_swap", "x_npv_ccy_value": 3_000.0, "x_npv_ccy": "USD"}}
    v = one(ctx, x).valuation
    assert v.quality == "reported_value" and v.pv_rub == pytest.approx(3_000.0 * fx_rate_cbr(ctx, "USD")[0])
    assert v.extra["reported_ccy"]["ccy"] == "USD"
