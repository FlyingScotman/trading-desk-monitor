"""TDM#441 and TDM#442: a model price is valued by core at that price (``fallback.at_model_price``), so its flash PnL is
exact; and every PnL with an unexplained part says why (``pnl.extra.other_reason``, ``pnl.reasons``), which the
period's checks group by (``api.pnl_checks.Unexplained``).

On the contract 1.4 golden snapshot of 15.10.2026 rolled to 16.10 (``test_batch1_fx_pnl.two_dates``: prices, curve
quotes and FX rates moved, invented) and on the 1.0 fixture with a synthetic previous date (``test_slice_s2``).
Synthetic data only."""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_batch1_fx_pnl import NEXT, two_dates  # noqa: F401
from test_fx_core_035 import DATE, USD_BOND
from test_rates_inputs_r14 import cache_without
from test_slice_s2 import prev_outcome, shifted_prev

import desk.orchestrator as orch
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import Outcome, attach_pnl, prepare, value
from desk.pnl import direct, reasons
from desk.pnl.flash import METHOD, explain
from desk.publish.tdm_run import pnl_extra
from desk.scenario import carry
from desk.valuation import fallback
from desk.valuation.fallback import (
    MODEL_Z_TOLERANCE_BP,
    CarriedSpread,
    FallbackContext,
    approximate,
    at_model_price,
    by_core,
    used_model_spreads,
)

RZD, KEY = "RU000A0JXQ44", "RU000A109XR1"
Q = 1000.0
FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
MEDIAN = "issuer median Z 210 bp (3 bonds) on 2026-10-14"


def _no_price(monkeypatch, *ctxs, secid=RZD):
    """``secid`` with today's quote and no price on each of ``ctxs``, and no earlier close: the model price rung."""
    for c in ctxs:
        quote, t = c.market.quote, c.static.bonds[secid]
        today = dataclasses.replace(quote(t), prices_pct={})
        monkeypatch.setattr(c.market, "quote", lambda terms, quote=quote, today=today:
                            today if terms.secid == secid else quote(terms))
    monkeypatch.setattr(FallbackContext, "past_quotes", lambda self, terms: iter(()))


@pytest.fixture
def dates(two_dates):  # noqa: F811
    """(D-1, D): fresh contexts, so a monkeypatched quote stays in one test."""
    return prepare(two_dates, DATE), prepare(two_dates, NEXT)


def _carried(z: float) -> dict[str, CarriedSpread]:
    return {RZD: CarriedSpread(RZD, z, date(2026, 10, 14), MEDIAN, "RUB RUONIA")}


# ---- TDM#441: a model price through core ------------------------------------------------------------------------

def test_a_model_price_is_valued_by_core_at_its_price_and_gives_back_its_spread(dates, monkeypatch):
    prev, _ = dates
    (e,) = value(prev, [(RZD, Q)])
    spreads = _carried(e.valuation.z_spread_bp + 40.0)
    _no_price(monkeypatch, prev)
    (o,) = value(prev, [(RZD, Q)], carried_spreads=spreads)
    v = o.valuation
    assert o.reason == "no_price" and o.core_result is not None and by_core(v)
    assert v.quality == "model_price" and v.price_basis == "model" and v.model_spread_source == MEDIAN
    assert v.assumptions[0].startswith("not exact: no_price") and v.assumptions[1].startswith("model price: RUB RUONIA")
    assert v.assumptions[2] == "valued by core at this price"
    # not circular: core's Z at the model's price is the spread the price was built from, and is carried as it is
    assert v.z_spread_bp == pytest.approx(spreads[RZD].z_spread_bp, abs=MODEL_Z_TOLERANCE_BP)
    (c,) = used_model_spreads([o])
    assert c.z_spread_bp == pytest.approx(spreads[RZD].z_spread_bp, abs=MODEL_Z_TOLERANCE_BP) and c.source == MEDIAN
    # the same value and risk as the ladder's own model price, now with core's flows
    monkeypatch.setattr(orch, "at_model_price", lambda *a: None)
    (ladder,) = value(prev, [(RZD, Q)], carried_spreads=spreads)
    m = ladder.valuation
    assert m.quality == "model_price" and not by_core(m) and ladder.core_result is None
    assert v.pv_rub == pytest.approx(m.pv_rub, rel=1e-9) and v.accrued_rub == pytest.approx(m.accrued_rub, abs=1e-9)
    assert v.dv01_rub == pytest.approx(m.dv01_rub, rel=1e-4) and v.cashflows


def test_model_price_on_both_dates_is_explained_exactly(dates, monkeypatch):
    prev, cur = dates
    (e,) = value(prev, [(RZD, Q)])
    spreads = _carried(e.valuation.z_spread_bp + 40.0)
    _no_price(monkeypatch, prev, cur)
    po, co = value(prev, [(RZD, Q)], carried_spreads=spreads), value(cur, [(RZD, Q)], carried_spreads=spreads)
    attach_pnl(prev, po, cur, co)
    p = co[0].pnl
    x = p.extra()
    assert x["method"] == METHOD and x["flag"] == "model price on both dates"
    assert x["other_reason"]["code"] == reasons.FLASH_RESIDUAL
    assert p.carry > 0 and p.rates != 0  # the day's curve move on the model's flows; the spread held
    assert abs(p.spread) < 1e-3 * abs(p.rates) and abs(p.other) < 0.01
    assert p.explained == pytest.approx(p.total, abs=0.01)


@pytest.mark.parametrize("refusal", orch.EXACT_REFUSALS)
def test_a_rung_on_core_keeps_the_simple_pnl_when_the_exact_steps_refuse(dates, monkeypatch, refusal):
    prev, cur = dates
    (e,) = value(prev, [(RZD, Q)])
    spreads = _carried(e.valuation.z_spread_bp + 40.0)
    _no_price(monkeypatch, prev, cur)
    po, co = value(prev, [(RZD, Q)], carried_spreads=spreads), value(cur, [(RZD, Q)], carried_spreads=spreads)

    def refuse(*a, **k):
        raise orch.NoPnl(refusal, "synthetic")

    monkeypatch.setattr(orch, "explain", refuse)
    attach_pnl(prev, po, cur, co)
    o = co[0]
    assert o.no_pnl is None and o.pnl is not None  # a PnL, as before TDM#441, not a missing one
    x = o.pnl.extra()
    assert x["method"] == fallback.METHOD and o.pnl.other == pytest.approx(o.pnl.total)
    assert x["other_reason"]["code"] == reasons.CORE_REFUSED and x["other_reason"]["core_code"] == refusal
    assert f"exact flash refused: {refusal}" in x["note"]


def test_other_refusals_of_the_exact_steps_stay_no_pnl(dates, monkeypatch):
    prev, cur = dates
    (e,) = value(prev, [(RZD, Q)])
    spreads = _carried(e.valuation.z_spread_bp + 40.0)
    _no_price(monkeypatch, prev, cur)
    po, co = value(prev, [(RZD, Q)], carried_spreads=spreads), value(cur, [(RZD, Q)], carried_spreads=spreads)

    def refuse(*a, **k):
        raise orch.NoPnl("stale_quote", "synthetic")

    monkeypatch.setattr(orch, "explain", refuse)
    attach_pnl(prev, po, cur, co)
    assert co[0].pnl is None and co[0].no_pnl.reason == "stale_quote"


def test_model_to_market_is_flagged_and_its_spread_step_is_price_discovery(dates, monkeypatch):
    prev, cur = dates
    (e,) = value(prev, [(RZD, Q)])
    spreads = _carried(e.valuation.z_spread_bp + 40.0)
    co = value(cur, [(RZD, Q)])  # a price of the market on D
    _no_price(monkeypatch, prev)
    po = value(prev, [(RZD, Q)], carried_spreads=spreads)
    raw = explain(po[0].valuation, co[0].valuation, orch.FlashCurves.build(
        prev.kbd, cur.kbd, prev.curve, cur.curve, prev_market=prev.core_market, cur_market=cur.core_market))
    attach_pnl(prev, po, cur, co)
    p = co[0].pnl
    r = p.extra()["other_reason"]
    assert p.flag == "price discovery: model -> market" and p.extra()["method"] == METHOD
    assert r["code"] == reasons.PRICE_SOURCE_SWITCH and r["detail"] == "model -> market"
    assert raw.spread > 0  # the model's Z was 40 bp wider than the market's: its price too low
    assert p.spread == 0.0 and p.other == pytest.approx(raw.other + raw.spread) and r["spread_rub"] == raw.spread
    assert (p.carry, p.rolldown, p.rates, p.total) == (raw.carry, raw.rolldown, raw.rates, raw.total)
    assert pnl_extra(p).get("flag") == p.flag and "other_reason" not in pnl_extra(p)  # the fact's shape kept


def test_a_model_price_is_carried_from_cores_value(dates, monkeypatch):
    _, cur = dates
    (e,) = value(cur, [(RZD, Q)])
    _no_price(monkeypatch, cur)
    (o,) = value(cur, [(RZD, Q)], carried_spreads=_carried(e.valuation.z_spread_bp + 40.0))
    v, h = o.valuation, cur.business_date + timedelta(days=30)
    (c,), _ = orch.horizon(cur, [(RZD, Q)], h, model_prices={"0": v.clean_pct})
    assert c.reason is None and c.carry.method == "core" and c.carry.coupon > 0
    assert c.carry.pv_start == pytest.approx(v.pv_rub, abs=1e-6)
    assert c.detail == "model price of the run; valued by core at this price"

    def row(pid, fact):
        return {"position_id": pid, "book_id": "B", "secid": RZD, "quantity": Q, "quality": "model_price",
                "pv_rub": v.pv_rub, "fact": fact}
    at, none = carry.bond_carry(cur, [row("m1", {"price": v.clean_pct}), row("m2", {})], h)
    assert at.code is None and at.method == "core" and at.carry == pytest.approx(c.carry.total)
    assert (none.carry, none.code) == (None, "model_price")  # no price stored: none, as before


def test_a_core_refusal_at_the_model_price_keeps_the_ladders_model(dates, monkeypatch):
    prev, _ = dates
    (e,) = value(prev, [(RZD, Q)])
    _no_price(monkeypatch, prev)
    real = orch.value_bonds

    def refuse_model(market, reqs, **kw):
        if any(r.price_basis == "model" for r in reqs):
            return {r.position_id: orch.Rejected("core_calculation_failed", "root not bracketed") for r in reqs}, []
        return real(market, reqs, **kw)
    monkeypatch.setattr(orch, "value_bonds", refuse_model)
    (o,) = value(prev, [(RZD, Q)], carried_spreads=_carried(e.valuation.z_spread_bp))
    assert o.valuation.quality == "model_price" and not by_core(o.valuation) and o.core_result is None
    assert o.valuation.assumptions[-1] == "core at the model price refused: core_calculation_failed (root not bracketed)"


@pytest.mark.parametrize("reason", ["coupon_unknown", "core_missing_market_data", None])
def test_only_a_missing_price_takes_core_at_the_model_price(dates, reason):
    prev, _ = dates
    t = prev.static.bonds[RZD]
    assert at_model_price(prev, "p", Outcome(RZD, t, prev.market.quote(t), reason=reason), Q, 99.0) is None


# ---- TDM#442: why the PnL is unexplained -------------------------------------------------------------------------

@pytest.fixture(scope="module")
def s2():
    """The 1.0 fixture of 15.10 and its synthetic previous date (``test_slice_s2``)."""
    ctx = prepare(SnapshotDir(FIX), "2026-10-15")
    return ctx, shifted_prev(ctx, ctx.business_date - timedelta(days=1))


def _pair(prev, ctx, p, o):
    attach_pnl(prev, [p], ctx, [o])
    return o


def test_the_exact_flash_says_its_residual(s2):
    ctx, prev = s2
    outs = [o for o in value(ctx, [(s, Q) for s in sorted(ctx.static.bonds)])
            if o.valuation and o.valuation.quality in ("exact", "assumed") and not o.valuation.foreign]
    prevs = [prev_outcome(prev, o) for o in outs]
    attach_pnl(prev, prevs, ctx, outs)
    flash = [o.pnl for o in outs if o.pnl is not None and o.pnl.extra()["method"] == METHOD]
    assert flash and all(p.other_reason["code"] == reasons.FLASH_RESIDUAL and p.other_reason["detail"] for p in flash)


def test_a_discount_curve_switch_is_named(s2):
    ctx, _ = s2
    kbd = dataclasses.replace(shifted_prev(ctx, ctx.business_date - timedelta(days=1), kbd_only=True),
                              rates={**ctx.rates, "codes": ["curve_missing:ruonia_ois", "curve_unbuilt:key_rate_swap"]})
    (o,) = value(ctx, [(RZD, Q)])
    o = _pair(kbd, ctx, prev_outcome(kbd, o), o)
    r = o.pnl.other_reason
    assert o.pnl.extra()["method"] == fallback.METHOD and o.pnl.other == o.pnl.total
    assert r["code"] == reasons.DISCOUNT_CURVE_SWITCH and r["curves"] == ["RUB OFZ", "RUB RUONIA"]
    assert r["curve"] == "RUB RUONIA" and r["date"] == "2026-10-14"


def test_a_floater_on_a_date_without_ruonia_names_the_curve(s2, tmp_path):
    ctx, prev = s2
    (e,) = value(ctx, [(KEY, Q)])
    no_ois = prepare(SnapshotDir(cache_without(tmp_path, "ruonia_ois")), "2026-10-15")
    (o,) = value(no_ois, [(KEY, Q)])
    assert o.reason == "core_missing_market_data" and o.valuation.quality == "market_value"
    o = _pair(prev, no_ois, prev_outcome(prev, e), o)
    r = o.pnl.other_reason
    assert r["code"] == reasons.NO_CURVE_ON_DATE and r["curve"] == "RUB RUONIA" and r["date"] == "2026-10-15"
    assert r["rung"] == "market_value" and r["core_code"] == "core_missing_market_data"


def _ladder(ctx, o, reason, quality=None, prev_date=None):
    v = approximate(ctx, Outcome(o.secid, o.terms, o.quote, reason=reason), Q, FallbackContext(ctx, history=[]))
    return Outcome(o.secid, o.terms, o.quote, valuation=dataclasses.replace(
        v, **({"quality": quality} if quality else {}), **({"valuation_date": prev_date} if prev_date else {})),
        reason=reason)


@pytest.mark.parametrize("reason, prev_q, code, detail", [
    ("no_price", None, reasons.FALLBACK_PRICE, "market_value on 2026-10-15 (no_price)"),
    ("core_calculation_failed", None, reasons.CORE_REFUSED, "core_calculation_failed"),
    ("no_price", "model_price", reasons.PRICE_SOURCE_SWITCH, "model -> market")])
def test_a_fallback_rung_says_its_rung_and_why(s2, reason, prev_q, code, detail):
    ctx, prev = s2
    (e,) = value(ctx, [(RZD, Q)])
    d0 = prev.kbd.trade_date
    o = _pair(prev, ctx, _ladder(ctx, e, reason, prev_q, d0), _ladder(ctx, e, reason))
    r = o.pnl.other_reason
    assert (r["code"], r["detail"]) == (code, detail) and o.pnl.other == o.pnl.total
    assert r.get("rung") == "market_value"
    if code == reasons.PRICE_SOURCE_SWITCH:
        assert o.pnl.flag == "price discovery: model -> market"


def test_a_foreign_bond_without_a_curve_says_its_yield(two_dates):  # noqa: F811
    prev, cur = prepare(two_dates, DATE), prepare(two_dates, NEXT)
    po, co = value(prev, [(USD_BOND, Q)]), value(cur, [(USD_BOND, Q)])
    attach_pnl(prev, po, cur, co)
    r = co[0].pnl.other_reason
    assert (r["code"], r["ccy"]) == (reasons.YIELD_NO_CURVE, "USD")


@pytest.mark.parametrize("o, code", [
    (Outcome(None, None, None, reason="unmapped_instrument: X1"), reasons.NO_TERMS),
    (Outcome(None, None, None, core_not_valued=("no_trade_terms", "")), reasons.NO_TERMS),
    (Outcome(None, None, None, reason="no_quantity: X1 (RU0001)"), reasons.REPORTED_VALUE),
    (Outcome(None, None, None, core_not_valued=("ccs_unsupported", "")), reasons.REPORTED_VALUE)])
def test_a_report_value_says_if_terms_are_missing(o, code):
    d0, d1 = SimpleNamespace(business_date=date(2026, 10, 14)), SimpleNamespace(business_date=date(2026, 10, 15))
    o.valuation = SimpleNamespace(pv_rub=120.0, quality="reported_value")
    p = Outcome(None, None, None, valuation=SimpleNamespace(pv_rub=100.0, quality="reported_value"))
    pnl = direct._reported(d0, d1, o, p, None)
    assert pnl.other == 20.0 and pnl.extra()["other_reason"]["code"] == code


def test_an_fx_deal_by_core_says_its_rest():
    d0, d1 = SimpleNamespace(business_date=date(2026, 10, 14)), SimpleNamespace(business_date=date(2026, 10, 15))
    o = Outcome(None, None, None, valuation=SimpleNamespace(pv_rub=120.0, extra={"core": True}, fx_delta={}))
    p = Outcome(None, None, None, valuation=SimpleNamespace(pv_rub=100.0, extra={"core": True}, fx_delta={}))
    pnl = direct._core_fx(d0, d1, o, p)
    assert pnl.other == 20.0 and pnl.extra()["other_reason"]["code"] == reasons.FX_CORE_REST


def test_every_code_has_a_label_and_an_unknown_one_is_refused():
    assert set(reasons.OTHER_REASON_TEXT) == {
        "no_curve_on_date", "discount_curve_switch", "core_refused", "no_terms", "fallback_price", "price_source_switch",
        "yield_no_curve", "reported_value", "fx_core_rest", "flash_residual"}
    assert reasons.other_reason(reasons.CORE_REFUSED, "x", core_code="core_y", rung=None) == {
        "code": "core_refused", "detail": "x", "core_code": "core_y"}
    with pytest.raises(ValueError):
        reasons.other_reason("approximated")
    assert str(reasons.REASONS_SINCE) in reasons.NO_REASON_TEXT
