"""TDM#616: FX NDFs in the OCP get their interest-rate risk (core 0.45, BACKLOG #135).

On the hub's contract 1.4 golden snapshot of 15.10.2026 (``fixtures/mdhub_v1_4``: curves for USD and CNY, none for
EUR; CBR fixings of 14 and 15 October) and synthetic trades (invented amounts):

- an X/RUB NDF settled in its foreign currency is valued by core: risk on the RUB and the foreign curve, the OCP amount
  core's FX delta (a fixed one owes a known amount of its settlement currency, not its notional);
- a cross NDF settled in its base or quote currency: risk on both foreign curves, the FX delta in both, its fixing
  from both currencies' CBR fixings; a RUB-settled cross with a fixing date is ``ndf_unsupported``, said in words;
- an NDF whose currency has no curve in the run: «нет данных: нет кривой EUR», no risk (never a zero);
- an NDF without its trade's terms: its own reason, not «оценена по отчёту, риска в отчёте нет»; the OCP as before;
- a RUB-settled X/RUB NDF is valued as before.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from rub_fixed_income import (
    FxForward,
    FxForwardLeg,
    FxForwardRiskEngine,
    FxForwardRiskRequest,
    ShockedCurveCache,
)
from test_fx_core_035 import FIX, terms

from desk.api import concepts, coverage
from desk.api.models import RiskTable
from desk.curves import fx as fxcurves
from desk.curves.rates import RUONIA_OIS_CURVE_ID
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare
from desk.publish import tdm_run
from desk.store import PositionInputs
from desk.valuation import derivatives as dv
from desk.valuation import positions as pv1

DATE = "2026-10-15"
BD = date(2026, 10, 15)

USD_IN_USD = terms("N1", "USD", "RUB", "sell", 1e6, 86e6, "2027-01-20", ndf=True, settle_ccy="USD",
                   fixing_date="2027-01-18", fixing_source="ЦБ")
USD_IN_RUB = terms("N1R", "USD", "RUB", "sell", 1e6, 86e6, "2027-01-20", ndf=True, settle_ccy="RUB",
                   fixing_date="2027-01-18", fixing_source="ЦБ")
FIXED_IN_USD = terms("N6", "USD", "RUB", "buy", 1e6, 83e6, "2026-10-16", ndf=True, settle_ccy="USD",
                     fixing_date="2026-10-14", fixing_source="CBR")
CROSS_IN_CNY = terms("N2", "USD", "CNY", "buy", 2e6, 14e6, "2026-12-17", ndf=True, settle_ccy="CNY",
                     fixing_date="2026-12-15", fixing_source="CBR")
CROSS_IN_USD = terms("N3", "USD", "CNY", "buy", 2e6, 14e6, "2026-12-17", ndf=True, settle_ccy="USD",
                     fixing_date="2026-12-15", fixing_source="CBR")
CROSS_FIXED = terms("N7", "USD", "CNY", "buy", 2e6, 14e6, "2026-10-16", ndf=True, settle_ccy="CNY",
                    fixing_date="2026-10-14", fixing_source="CBR")
CROSS_IN_RUB = terms("N4", "USD", "CNY", "buy", 2e6, 14e6, "2026-12-17", ndf=True, settle_ccy="RUB",
                     fixing_date="2026-12-15", fixing_source="CBR")
CROSS_IN_RUB_NO_FIXING = terms("N8", "USD", "CNY", "buy", 2e6, 14e6, "2026-12-17", ndf=True, settle_ccy="RUB")
THIRD_CCY = terms("N9", "USD", "CNY", "buy", 2e6, 14e6, "2026-12-17", ndf=True, settle_ccy="EUR",
                  fixing_date="2026-12-15")
EUR_NO_CURVE = terms("N5", "EUR", "RUB", "buy", 1e6, 95e6, "2027-01-20", ndf=True, settle_ccy="EUR",
                     fixing_date="2027-01-18", fixing_source="CBR")
ALL = [USD_IN_USD, USD_IN_RUB, FIXED_IN_USD, CROSS_IN_CNY, CROSS_IN_USD, CROSS_FIXED, CROSS_IN_RUB,
       CROSS_IN_RUB_NO_FIXING, THIRD_CCY, EUR_NO_CURVE]


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), DATE)


@pytest.fixture(scope="module")
def valued(ctx):
    reqs = [dv.Request(f"P-{r['trade_id']}", "FXB", {"ccy": r["base_ccy"]}, [dv.TermsLeg.of(r)]) for r in ALL]
    return dv.value(ctx, reqs, options=fxcurves.FxOptions())[0]


def cbr(ctx, ccy, on):
    return next(f.rub_per_unit for f in ctx.core_market.fx_fixings if f.currency == ccy and f.fixing_date == on)


def test_x_rub_ndf_settled_in_its_currency(ctx, valued):
    v = valued["P-N1"]
    assert v.extra["core"]["product"] == "fx_ndf"
    assert set(v.curve_buckets) == {"RUB RUONIA", "USD FX"}  # rates risk on both curves
    assert all(abs(sum(bs.values())) > 100 for bs in v.curve_buckets.values())
    assert v.extra["legs"][0]["settlement_ccy"] == "USD" and v.extra["fixings"][0]["status"] == "open"
    # the OCP amount is core's FX delta, the spot equivalent, not the notional
    assert v.extra["ocp_amount"] == {"USD": pytest.approx(v.fx_delta["USD"], rel=1e-12)}
    assert -1e6 < v.fx_delta["USD"] < -0.98e6
    # core's own engine on the same inputs, the leg settled in USD
    spot, curve = ctx.fx.spots["USD"], ctx.fx.curves["USD FX"].curve
    rub = next(e.curve for e in ctx.core_market.curves if e.curve_id == RUONIA_OIS_CURVE_ID)
    trade = FxForward("N1", "USD", 1.0, FxForwardLeg(date(2027, 1, 20), -1e6, 86.0, date(2027, 1, 20),
                                                       fixing_date=date(2027, 1, 18), fixing_source="CBR",
                                                       settlement_currency="USD"))
    engine = FxForwardRiskEngine(FxForwardRiskRequest(trade, spot.rub_per_unit, spot.value_date, rub, curve, 0.0),
                                 ShockedCurveCache())
    assert v.pv_rub == pytest.approx(engine.base_pv_rub, rel=1e-12)


def test_rub_settled_ndf_as_before(valued):
    v = valued["P-N1R"]
    assert v.extra["core"]["product"] == "fx_ndf" and set(v.curve_buckets) == {"RUB RUONIA", "USD FX"}
    assert v.extra["ocp_amount"] == {"USD": -1e6}  # the notional, as before TDM#616
    assert v.extra["legs"][0]["settlement_ccy"] == "RUB"


def test_fixed_ndf_settled_in_usd_owes_a_known_usd_amount(ctx, valued):
    v = valued["P-N6"]
    fixing = cbr(ctx, "USD", date(2026, 10, 14))
    assert v.extra["fixings"][0] == {"trade_id": "N6", "fixing_date": "2026-10-14", "source": "CBR",
                                     "status": "fixed", "fixing_rate": fixing}
    owed = 1e6 * (1 - 83.0 / fixing)  # N (1 - K / fixing) USD on the payment date
    assert v.fx_delta["USD"] == pytest.approx(owed, rel=1e-3)  # paid on the spot date: barely discounted
    assert v.extra["ocp_amount"] == {"USD": pytest.approx(v.fx_delta["USD"], rel=1e-12)}  # not the 1m notional


@pytest.mark.parametrize("pid", ["P-N2", "P-N3", "P-N8"])
def test_cross_ndf_has_risk_on_both_foreign_curves(valued, pid):
    v = valued[pid]
    assert {"fx_cross", "fx_ndf"} <= set(v.extra["core"]["product"].split("+"))
    assert {"USD FX", "CNY FX"} <= set(v.curve_buckets)
    assert v.fx_delta["USD"] > 0 > v.fx_delta["CNY"]
    assert v.extra["ocp_amount"] == {c: pytest.approx(d, rel=1e-12) for c, d in v.fx_delta.items()}


def test_cross_ndf_settlement_and_fixing(ctx, valued):
    assert valued["P-N2"].extra["legs"][0]["settlement_ccy"] == "CNY"
    assert valued["P-N3"].extra["legs"][0]["settlement_ccy"] == "USD"
    assert valued["P-N2"].extra["fixings"][0]["status"] == "open"
    assert any("without fixing_date" in a for a in valued["P-N8"].assumptions)  # RUB-settled, no fixing: projected
    v = valued["P-N7"]  # fixed from both CBR fixings of its source
    on = date(2026, 10, 14)
    assert v.extra["fixings"][0]["fixing_rate"] == pytest.approx(cbr(ctx, "USD", on) / cbr(ctx, "CNY", on), rel=1e-12)
    assert set(v.fx_delta) == {"CNY"}  # a known CNY amount


def test_rub_settled_cross_with_fixing_and_a_third_currency_are_unsupported(valued):
    for pid in ("P-N4", "P-N9"):
        assert isinstance(valued[pid], dv.NotValued) and valued[pid].reason == "ndf_unsupported"
    why, todo = coverage.CAUSES["ndf_unsupported"]
    assert "кросс-NDF" in why and "в рублях" in why and todo


def test_no_curve_is_no_data_never_a_zero(valued):
    e = valued["P-N5"]
    assert isinstance(e, dv.NotValued) and (e.reason, e.ccy) == ("no_foreign_curve", "EUR")


def _inputs(extra, trade_terms):
    return PositionInputs([(pid, "FXB", None, None) for pid in extra], [], {"FXB": BD}, datetime.now(UTC), "0" * 64,
                          extra, trade_terms=trade_terms)


def test_published_rows_and_the_risk_table(ctx):
    extra = {
        "FXB|N1": {"asset_class": "fx", "ccy": "USD", "amount_ccy": -1e6, "value_date": date(2027, 1, 20),
                   "deal_id": "N1", "x_product_group": "fx_ndf"},
        "FXB|N2": {"asset_class": "fx", "ccy": "USD", "amount_ccy": 2e6, "value_date": date(2026, 12, 17),
                   "deal_id": "N2", "x_product_group": "fx_ndf"},
        "FXB|N5": {"asset_class": "fx", "ccy": "EUR", "amount_ccy": 1e6, "value_date": date(2027, 1, 20),
                   "deal_id": "N5", "mv_rub": 5000.0, "x_product_group": "fx_ndf"},
        "FXB|NT": {"asset_class": "fx", "ccy": "USD", "amount_ccy": 3e6, "value_date": date(2027, 1, 20),
                   "mv_rub": 7000.0, "x": {"x_product_group": "fx_ndf"}},
    }
    bp = pv1.plan(ctx, _inputs(extra, [USD_IN_USD, CROSS_IN_CNY, EUR_NO_CURVE]))
    rows = {pid: tdm_run.direct_row(ctx, "FXB", pid, bp.direct[pid], x, None) for pid, x in extra.items()}
    n1, n2, n5, nt = rows.values()
    assert set(n1["risk"]) == {"RUB RUONIA", "USD FX"} and n1["extra"]["valuation"] == "computed"
    assert {"USD FX", "CNY FX"} <= set(n2["risk"]) and set(n2["fx_delta"]) == {"USD", "CNY"}
    # no EUR curve: the report's value, no risk, the reason «нет данных: нет кривой EUR»
    assert n5["risk"] == {} and n5["extra"]["not_exact_reason"] == "no_foreign_curve"
    assert n5["extra"]["risk_missing"] == {"reason": "no_curve", "ccy": "EUR", "text": "нет данных: нет кривой EUR"}
    assert n5["fx_delta"] == {"EUR": 1e6}  # the OCP keeps the amount
    # an NDF without terms: its own reason; the amount stays its FX delta (OCP) as before
    assert nt["risk"] == {} and nt["extra"]["not_exact_reason"] == pv1.NO_TERMS
    assert nt["extra"]["risk_missing"]["reason"] == "ndf_no_terms"
    assert nt["extra"]["risk_missing"]["text"].startswith("NDF без условий сделки: процентный риск не считается")
    assert nt["fx_delta"] == {"USD": 3e6} and nt["extra"]["fx_delta_basis"].startswith("notional")

    fx = {c: r[0] for c, r in ctx.fx.rates.items()}
    t = concepts.risk_table([{"position_id": pid, "fact": f} for pid, f in rows.items()], fx)
    RiskTable(run_id=1, **t)  # the API's model takes the new reasons
    usd = next(r for r in t["rows"] if (r["ccy"], r["level"], r["kind"]) == ("USD", "kind", "FX_FWD"))
    eur = next(r for r in t["rows"] if (r["ccy"], r["level"], r["kind"]) == ("EUR", "kind", "FX_FWD"))
    assert usd["dv01_by_curve_rub"].get("USD FX")  # the valued NDFs' foreign curve risk is in the table
    assert {m["reason"]: m["text"] for m in usd["risk_missing"]} == {"ndf_no_terms": pv1.RISK_MISSING["ndf_no_terms"]}
    assert eur["risk_missing"] == [{"reason": "no_curve", "text": "нет данных: нет кривой EUR", "n": 1, "ccy": "EUR"}]
    assert "reported_no_risk" not in {m["reason"] for r in t["rows"] for m in r["risk_missing"]}


def test_a_run_before_616_keeps_its_reason():
    f = {"instr_type": "FX", "ccy": "USD", "risk": {}, "fx_delta": {"USD": 1.0},
         "extra": {"quality": "reported_value", "reported": {"x": {"x_product_group": "fx_ndf"}}}}
    t = concepts.risk_table([{"position_id": "P", "fact": f}], {"USD": 80.0})
    row = next(r for r in t["rows"] if r["level"] == "kind")
    assert row["risk_missing"] == [{"reason": "reported_no_risk", "text": concepts.MISSING_TEXT["reported_no_risk"],
                                    "n": 1}]
