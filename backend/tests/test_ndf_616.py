"""TDM#616: FX NDFs in the OCP get their interest-rate risk (core 0.45, BACKLOG #135).

On the hub's contract 1.4 golden snapshot of 15.10.2026 (``fixtures/mdhub_v1_4``: curves for USD and CNY, none for
EUR; CBR fixings of 14 and 15 October) and synthetic trades (invented amounts):

- an X/RUB NDF settled in its foreign currency is valued by core: risk on the RUB and the foreign curve, the OCP amount
  core's FX delta (a fixed one owes a known amount of its settlement currency, not its notional);
- a cross NDF settled in its base or quote currency: risk on both foreign curves, the FX delta in both, its fixing
  from both currencies' CBR fixings;
- a cross NDF settled in RUB (EUR/USD, as the exchange's cross futures; the owner, 10.11.2026): unfixed, risk on RUB and
  both foreign curves, the FX delta in both; fixed, a known RUB amount (RUB risk only); a fixing missing after its
  date falls back to the report with core's reason; due today and unpublished, pending. Settled outside its pair:
  ``ndf_unsupported``, said in words;
- an NDF without ``settle_ccy`` settles in RUB; the adapter's ``x_settle_ccy_assumed`` shows on its leg;
- an NDF whose currency has no curve in the run: «нет данных: нет кривой EUR», no risk (never a zero);
- an NDF without its trade's terms: its own reason, not «оценена по отчёту, риска в отчёте нет»; the OCP as before;
- a RUB-settled X/RUB NDF is valued as before.
"""

from __future__ import annotations

from dataclasses import replace
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
from desk.rules import ocp as O
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
    # unfixed: the nominal OCP amount is the notional, as a RUB-settled NDF's; the FX delta core's spot equivalent
    assert v.extra["ocp_amount"] == {"USD": -1e6}
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
    assert v.extra["ocp_amount"] == {"USD": pytest.approx(owed, rel=1e-12)}  # the amount owed, not the 1m notional


@pytest.mark.parametrize("pid", ["P-N2", "P-N3", "P-N8"])
def test_cross_ndf_has_risk_on_both_foreign_curves(valued, pid):
    v = valued[pid]
    assert {"fx_cross", "fx_ndf"} <= set(v.extra["core"]["product"].split("+"))
    assert {"USD FX", "CNY FX"} <= set(v.curve_buckets)
    assert v.fx_delta["USD"] > 0 > v.fx_delta["CNY"]
    assert v.extra["ocp_amount"] == {"USD": 2e6, "CNY": -14e6}  # unfixed: the notionals


def _ocp(v, basis):
    """The OCP amount of a valued row on ``basis`` (``rules.ocp.classify`` of its published shape)."""
    row = {"instr_type": "FX", "fx_delta": dict(v.fx_delta), "extra": dict(v.extra)}
    return O.classify(row, [], {}, BD, O.OcpPolicy(amount_basis=basis))[1]


def _owed(ctx, pid):
    """The known amount a fixed NDF owes in its settlement currency, from its terms and the CBR fixings of 14.10."""
    on = date(2026, 10, 14)
    usd = cbr(ctx, "USD", on)
    if pid == "P-N6":  # USD/RUB settled in USD: N (1 - K / fixing) USD
        return {"USD": 1e6 * (1 - 83.0 / usd)}
    return {"CNY": 2e6 * (usd / cbr(ctx, "CNY", on) - 7.0)}  # USD/CNY settled in CNY: N (fixing - K) CNY


@pytest.mark.parametrize(("pid", "fixed"), [("P-N1", False), ("P-N6", True), ("P-N2", False), ("P-N7", True)])
def test_ocp_on_both_bases(ctx, valued, pid, fixed):
    """Spot equivalent: core's FX delta. Nominal: the notionals while unfixed (as a RUB-settled NDF), the known amount
    owed in the settlement currency once fixed (core's settlement_amount: not the delta, which is discounted)."""
    v = valued[pid]
    assert _ocp(v, O.AmountBasis.SPOT_EQUIVALENT) == pytest.approx(v.fx_delta, rel=1e-12)
    nominal = _ocp(v, O.AmountBasis.NOMINAL)
    if fixed:
        assert nominal == pytest.approx(_owed(ctx, pid), rel=1e-9)
        assert set(nominal) == set(v.fx_delta)
    else:
        assert nominal == {"P-N1": {"USD": -1e6}, "P-N2": {"USD": 2e6, "CNY": -14e6}}[pid]


@pytest.mark.parametrize("pid", ["P-N6", "P-N7"])
def test_fixed_ndf_paid_later_owes_its_amount_not_the_delta(ctx, pid):
    """Paid after its spot date, a fixed NDF's FX delta is the amount discounted to the spot date; the nominal OCP
    amount is the amount owed (core's settlement_amount), undiscounted."""
    r = {"P-N6": FIXED_IN_USD, "P-N7": CROSS_FIXED}[pid]
    late = {**r, "value_date": "2026-11-16"}  # fixed on 14.10 for a month later
    out, _ = dv.value(ctx, [dv.Request(pid, "FXB", {"ccy": r["base_ccy"]}, [dv.TermsLeg.of(late)])],
                      options=fxcurves.FxOptions())
    v = out[pid]
    assert v.extra["fixings"][0]["status"] == "fixed"
    assert v.extra["ocp_amount"] == pytest.approx(_owed(ctx, pid), rel=1e-9)
    assert _ocp(v, O.AmountBasis.NOMINAL) == pytest.approx(_owed(ctx, pid), rel=1e-9)
    assert _ocp(v, O.AmountBasis.SPOT_EQUIVALENT) == pytest.approx(v.fx_delta, rel=1e-12)
    ccy, owed = next(iter(_owed(ctx, pid).items()))
    assert abs(v.fx_delta[ccy]) < abs(owed) and v.fx_delta[ccy] != pytest.approx(owed, rel=1e-6)  # discounted


def test_cross_ndf_fixing_due_today_is_pending(ctx):
    """A cross NDF fixing today whose fixing is not published yet: projected, its fixing ``pending``."""
    t = terms("NP", "USD", "CNY", "buy", 2e6, 14e6, "2026-10-19", ndf=True, settle_ccy="CNY", fixing_date=DATE,
              fixing_source="CBR")
    gone = tuple(f for f in ctx.core_market.fx_fixings if f.fixing_date != BD)
    c = replace(ctx, core_market=replace(ctx.core_market, fx_fixings=gone))  # fx_risk_market derives from it
    out, _ = dv.value(c, [dv.Request("P-NP", "FXB", {"ccy": "USD"}, [dv.TermsLeg.of(t)])],
                      options=fxcurves.FxOptions())
    v = out["P-NP"]
    assert not isinstance(v, dv.NotValued), v
    assert v.extra["fixings"] == [{"trade_id": "NP", "fixing_date": DATE, "source": "CBR", "status": "pending"}]
    assert "fixing_pending" in v.extra["core"]["model_flags"]
    assert {"USD FX", "CNY FX"} <= set(v.curve_buckets)
    assert v.extra["ocp_amount"] == {"USD": 2e6, "CNY": -14e6}  # not fixed yet: the notionals


def test_no_spot_and_no_curve_name_what_is_missing(ctx):
    """No spot (no settlement calendar in the hub's static) but a curve: «нет спот-курса»; neither: both named."""
    spots = {k: v for k, v in ctx.fx.spots.items() if k not in ("CNY", "USD")}
    no_usd_curve = tuple(e for e in ctx.core_market.curves if e.curve_id != "USD FX")
    c = replace(ctx, fx=replace(ctx.fx, spots=spots), core_market=replace(ctx.core_market, curves=no_usd_curve))
    cny = terms("NC", "CNY", "RUB", "buy", 1e6, 12.6e6, "2027-01-20", ndf=True, settle_ccy="CNY")
    usd = terms("NU", "USD", "RUB", "buy", 1e6, 86e6, "2027-01-20", ndf=True, settle_ccy="USD")
    out, _ = dv.value(c, [dv.Request(f"P-{r['trade_id']}", "FXB", {"ccy": r["base_ccy"]}, [dv.TermsLeg.of(r)])
                          for r in (cny, usd)], options=fxcurves.FxOptions())
    assert (out["P-NC"].reason, out["P-NC"].ccy, out["P-NC"].missing) == ("no_fx_spot", "CNY", "spot")  # CNY XCCY
    assert (out["P-NU"].reason, out["P-NU"].ccy, out["P-NU"].missing) == ("no_fx_spot", "USD", "curve_spot")
    assert pv1.risk_missing(pv1.MISSING_CODES["spot"], "CNY")["text"] == \
        "нет данных: нет спот-курса CNY (нет календаря расчётов)"
    assert pv1.risk_missing(pv1.MISSING_CODES["curve_spot"], "USD")["text"] == "нет данных: нет кривой и курса USD"
    # published and in the risk table
    extra = {"FXB|NC": {"asset_class": "fx", "ccy": "CNY", "amount_ccy": 1e6, "value_date": date(2027, 1, 20),
                        "deal_id": "NC", "mv_rub": 100.0, "x_product_group": "fx_ndf"}}
    row = tdm_run.direct_row(c, "FXB", "FXB|NC", pv1.plan(c, _inputs(extra, [cny])).direct["FXB|NC"],
                             extra["FXB|NC"], None)
    assert row["risk"] == {} and row["extra"]["risk_missing"] == {
        "reason": "no_spot", "ccy": "CNY", "text": "нет данных: нет спот-курса CNY (нет календаря расчётов)"}
    t = concepts.risk_table([{"position_id": "FXB|NC", "fact": row}], {"CNY": 12.5})
    RiskTable(run_id=1, **t)
    cell = next(r for r in t["rows"] if (r["ccy"], r["level"], r["kind"]) == ("CNY", "kind", "FX_FWD"))
    assert cell["risk_missing"] == [{"reason": "no_spot", "text": "нет данных: нет спот-курса CNY (нет календаря "
                                                                 "расчётов)", "n": 1, "ccy": "CNY"}]


def test_cross_ndf_settlement_and_fixing(ctx, valued):
    assert valued["P-N2"].extra["legs"][0]["settlement_ccy"] == "CNY"
    assert valued["P-N3"].extra["legs"][0]["settlement_ccy"] == "USD"
    assert valued["P-N2"].extra["fixings"][0]["status"] == "open"
    assert any("without fixing_date" in a for a in valued["P-N8"].assumptions)  # RUB-settled, no fixing: projected
    v = valued["P-N7"]  # fixed from both CBR fixings of its source
    on = date(2026, 10, 14)
    assert v.extra["fixings"][0]["fixing_rate"] == pytest.approx(cbr(ctx, "USD", on) / cbr(ctx, "CNY", on), rel=1e-12)
    assert set(v.fx_delta) == {"CNY"}  # a known CNY amount


def test_a_settlement_currency_outside_the_pair_is_unsupported(valued):
    assert isinstance(valued["P-N9"], dv.NotValued) and valued["P-N9"].reason == "ndf_unsupported"
    why, todo = coverage.CAUSES["ndf_unsupported"]
    assert "не из пары" in why and "кросс-NDF" not in why and todo
    assert not isinstance(valued["P-N4"], dv.NotValued)  # a RUB-settled cross with its fixing: valued since 4ce0d6e


# --- cross NDFs settled in RUB (EUR/USD) ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def eur_ctx(ctx):
    """The run with a synthetic EUR FX-implied curve (the fixture has none): USD's quotes scaled by EUR/USD spot."""
    usd = next(e for e in ctx.core_market.curves if e.curve_id == "USD FX")
    k = ctx.fx.spots["EUR"].rub_per_unit / ctx.fx.spots["USD"].rub_per_unit
    qs = tuple(replace(q, points=q.points * k) if hasattr(q, "points") else replace(q, outright=q.outright * k)
               for q in usd.curve.quotes)
    eur = replace(usd, curve_id="EUR FX", spec_id="eur_fx_implied_v1", curve=replace(usd.curve, currency="EUR",
                                                                                     quotes=qs))
    return replace(ctx, core_market=replace(ctx.core_market, curves=(*ctx.core_market.curves, eur)))


def eurusd(tid, vd, fix, **kw):
    return terms(tid, "EUR", "USD", "buy", 1e6, 1.08e6, vd, ndf=True, fixing_date=fix, fixing_source="ЦБ", **kw)


def _value(c, *rows):
    return dv.value(c, [dv.Request(f"P-{r['trade_id']}", "FXB", {"ccy": r["base_ccy"]}, [dv.TermsLeg.of(r)])
                        for r in rows], options=fxcurves.FxOptions())[0]


def test_eurusd_rub_settled_unfixed(eur_ctx):
    v = _value(eur_ctx, eurusd("E1", "2026-12-17", "2026-12-15", settle_ccy="RUB"))["P-E1"]
    assert {"fx_cross", "fx_ndf"} <= set(v.extra["core"]["product"].split("+"))
    assert set(v.curve_buckets) == {"RUB RUONIA", "EUR FX", "USD FX"}
    assert v.fx_delta["EUR"] > 0.98e6 and -1.08e6 < v.fx_delta["USD"] < -0.98 * 1.08e6
    assert v.extra["legs"][0]["settlement_ccy"] == "RUB" and v.extra["fixings"][0]["status"] == "open"
    assert _ocp(v, O.AmountBasis.SPOT_EQUIVALENT) == pytest.approx(v.fx_delta, rel=1e-12)  # both currencies
    assert _ocp(v, O.AmountBasis.NOMINAL) == {"EUR": 1e6, "USD": -1.08e6}  # the terms' notionals


def test_eurusd_rub_settled_fixed_is_a_known_rub_amount(ctx, eur_ctx):
    on = date(2026, 10, 14)
    v = _value(eur_ctx, eurusd("E2", "2026-10-16", "2026-10-14", settle_ccy="RUB"))["P-E2"]
    eur, usd = cbr(ctx, "EUR", on), cbr(ctx, "USD", on)
    assert v.extra["fixings"][0]["status"] == "fixed"
    assert v.extra["fixings"][0]["fixing_rate"] == pytest.approx(eur / usd, rel=1e-12)
    assert not v.fx_delta and set(v.curve_buckets) <= {"RUB RUONIA"}  # RUB risk only
    df = v.extra["legs"][0]["df"]
    assert v.pv_rub == pytest.approx(1e6 * (eur - 1.08 * usd) * df, rel=1e-9)  # N R_X - N K R_Y, paid in RUB
    # the OCP as a fixed RUB-settled X/RUB NDF: no FX delta (spot equivalent), the notionals on the nominal basis
    assert _ocp(v, O.AmountBasis.SPOT_EQUIVALENT) == {}
    assert _ocp(v, O.AmountBasis.NOMINAL) == {"EUR": 1e6, "USD": -1.08e6}


def test_eurusd_fixing_missing_after_its_date_falls_back(ctx, eur_ctx):
    gone = tuple(f for f in eur_ctx.core_market.fx_fixings
                 if not (f.currency == "USD" and f.fixing_date == date(2026, 10, 14)))
    c = replace(eur_ctx, core_market=replace(eur_ctx.core_market, fx_fixings=gone))
    e = _value(c, eurusd("E4", "2026-10-16", "2026-10-14", settle_ccy="RUB"))["P-E4"]
    assert isinstance(e, dv.NotValued) and e.reason == "core_missing_market_data" and "USD" in e.detail
    assert e.reason.removeprefix("core_") in coverage.CORE_CAUSES  # said in words
    extra = {"FXB|E4": {"asset_class": "fx", "ccy": "EUR", "amount_ccy": 1e6, "value_date": date(2026, 10, 16),
                        "deal_id": "E4", "mv_rub": 100.0, "x_product_group": "fx_ndf"}}
    o = pv1.plan(c, _inputs(extra, [eurusd("E4", "2026-10-16", "2026-10-14", settle_ccy="RUB")])).direct["FXB|E4"]
    assert o.reason == "core_missing_market_data" and o.valuation.quality == "reported_value"


def test_eurusd_fixing_due_today_is_pending(eur_ctx):
    gone = tuple(f for f in eur_ctx.core_market.fx_fixings if f.fixing_date != BD)
    c = replace(eur_ctx, core_market=replace(eur_ctx.core_market, fx_fixings=gone))
    v = _value(c, eurusd("E5", "2026-10-19", DATE, settle_ccy="RUB"))["P-E5"]
    assert not isinstance(v, dv.NotValued), v
    assert v.extra["fixings"][0]["status"] == "pending" and "fixing_pending" in v.extra["core"]["model_flags"]
    assert set(v.fx_delta) == {"EUR", "USD"}


def test_ndf_without_a_settlement_currency_settles_in_rub(eur_ctx):
    """No ``settle_ccy``: RUB, as a RUB-settled one; the leg says the currency was taken (the adapter's
    ``x_settle_ccy_assumed``, or none given at all)."""
    out = _value(eur_ctx, eurusd("E6", "2026-12-17", "2026-12-15"),
                 eurusd("E7", "2026-12-17", "2026-12-15", settle_ccy="RUB", x_settle_ccy_assumed=True),
                 eurusd("E8", "2026-12-17", "2026-12-15", settle_ccy="RUB"))
    assert out["P-E6"].pv_rub == pytest.approx(out["P-E8"].pv_rub, rel=1e-12)
    assert [out[k].extra["legs"][0].get("settlement_ccy_assumed") for k in ("P-E6", "P-E7", "P-E8")] == [
        True, True, None]
    assert out["P-E7"].extra["legs"][0]["settlement_ccy"] == "RUB"


def test_no_curve_is_no_data_never_a_zero(valued):
    e = valued["P-N5"]
    assert isinstance(e, dv.NotValued) and (e.reason, e.ccy, e.missing) == ("no_foreign_curve", "EUR", "curve")


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
        "FXB|N9": {"asset_class": "fx", "ccy": "USD", "amount_ccy": 2e6, "value_date": date(2026, 12, 17),
                   "deal_id": "N9", "mv_rub": 9000.0, "x_product_group": "fx_ndf"},
    }
    bp = pv1.plan(ctx, _inputs(extra, [USD_IN_USD, CROSS_IN_CNY, EUR_NO_CURVE, THIRD_CCY]))
    rows = {pid: tdm_run.direct_row(ctx, "FXB", pid, bp.direct[pid], x, None) for pid, x in extra.items()}
    n1, n2, n5, nt, n9 = rows.values()
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
    # an NDF settled outside its pair: core does not value it, the card and the table say so in words
    assert n9["risk"] == {} and n9["extra"]["not_exact_reason"] == "ndf_unsupported"
    assert n9["extra"]["risk_missing"] == {"reason": "ndf_unsupported", "text": pv1.RISK_MISSING["ndf_unsupported"]}
    assert n9["fx_delta"] == {"USD": 2e6}

    fx = {c: r[0] for c, r in ctx.fx.rates.items()}
    t = concepts.risk_table([{"position_id": pid, "fact": f} for pid, f in rows.items()], fx)
    RiskTable(run_id=1, **t)  # the API's model takes the new reasons
    usd = next(r for r in t["rows"] if (r["ccy"], r["level"], r["kind"]) == ("USD", "kind", "FX_FWD"))
    eur = next(r for r in t["rows"] if (r["ccy"], r["level"], r["kind"]) == ("EUR", "kind", "FX_FWD"))
    assert usd["dv01_by_curve_rub"].get("USD FX")  # the valued NDFs' foreign curve risk is in the table
    assert {m["reason"]: m["text"] for m in usd["risk_missing"]} == {
        "ndf_no_terms": pv1.RISK_MISSING["ndf_no_terms"], "ndf_unsupported": pv1.RISK_MISSING["ndf_unsupported"]}
    assert eur["risk_missing"] == [{"reason": "no_curve", "text": "нет данных: нет кривой EUR", "n": 1, "ccy": "EUR"}]
    assert "reported_no_risk" not in {m["reason"] for r in t["rows"] for m in r["risk_missing"]}


def test_otc_rows(ctx):
    """The OTC path: an NDF without terms and without the report's risk says why; a row with the report's DV01 linked to
    terms in a currency without a curve keeps that risk and gets no «нет данных» (it would contradict it)."""
    extra = {
        "FXB|OTC-NT": {"asset_class": "otc", "ccy": "USD", "source_instrument_id": "OTC-NT", "mv_rub": 100.0,
                       "x": {"x_product_group": "fx_ndf"}},
        "FXB|OTC-N5": {"asset_class": "otc", "ccy": "EUR", "source_instrument_id": "N5", "deal_id": "N5",
                       "mv_rub": 200.0, "risk_dv01_rub": -50.0, "maturity_date": date(2027, 1, 20),
                       "x_product_group": "fx_ndf"},
        "FXB|OTC-N5B": {"asset_class": "otc", "ccy": "EUR", "source_instrument_id": "N5B", "deal_id": "N5B",
                        "mv_rub": 200.0, "x_product_group": "fx_ndf"},
    }
    bp = pv1.plan(ctx, _inputs(extra, [EUR_NO_CURVE, {**EUR_NO_CURVE, "trade_id": "N5B"}]))
    nt, n5, n5b = (bp.direct[k].valuation for k in extra)
    assert nt.extra["risk_missing"]["reason"] == "ndf_no_terms"
    assert n5.curve_buckets and "risk_missing" not in n5.extra  # the report's DV01 stays, not contradicted
    assert n5b.extra["risk_missing"] == {"reason": "no_curve", "ccy": "EUR", "text": "нет данных: нет кривой EUR"}


def test_ndf_row_predicate_is_the_ocps():
    assert pv1.is_ndf_row({"x": {"x_product_group": "fx_ndf"}}) and pv1.is_ndf_row({"x_product_group": "NDF"})
    assert pv1.is_ndf_row({}, [{"ndf": True}]) and not pv1.is_ndf_row({"x_product_group": "fx_forward"})


def test_a_run_before_616_keeps_its_reason():
    f = {"instr_type": "FX", "ccy": "USD", "risk": {}, "fx_delta": {"USD": 1.0},
         "extra": {"quality": "reported_value", "reported": {"x": {"x_product_group": "fx_ndf"}}}}
    t = concepts.risk_table([{"position_id": "P", "fact": f}], {"USD": 80.0})
    row = next(r for r in t["rows"] if r["level"] == "kind")
    assert row["risk_missing"] == [{"reason": "reported_no_risk", "text": concepts.MISSING_TEXT["reported_no_risk"],
                                    "n": 1}]


def test_adapter_warning_for_an_assumed_settlement_currency_is_in_words():
    """The adapter's unit warning ``ndf_settle_ccy_assumed`` (an NDF without a settlement currency in its terms, given
    the configured default RUB, ``x_settle_ccy_assumed``) has its words on the inbox screens (``daystatus``)."""
    from collections import Counter

    from desk import daystatus

    want = "NDF без валюты расчёта в условиях сделки: принята валюта по умолчанию из настроек адаптера"
    for code in ("ndf_settle_ccy_assumed", "ndf_settle_ccy_assumed_rub"):
        assert daystatus._codes(Counter({("warning", code): 3})) == [
            {"severity": "warning", "code": code, "count": 3, "text": want}]
