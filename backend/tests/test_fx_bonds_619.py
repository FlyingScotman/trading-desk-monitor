"""TDM#619: foreign-face bonds (EUR, CNY): the yield and the spread the same for every bond of a currency.

On the contract 1.4 golden snapshot of 15.10.2026 with synthetic EUR and CNY copies of its amortizing USD-face bond in every
group of the issue (``fx_bonds_619``: the quote on the business date / an earlier day's / none, MOEX's yield published or
not; the run's curve of the currency there or not). Before: a bond whose quote was of an earlier day was refused before
core (``stale_quote``) and valued by the ladder on ``stale_price``: the source's yield, or none, and no spread though the
currency had a curve; with no yield of the source the DV01 was a time to maturity at a zero rate, unsaid.

Now: core values it at its last price on its currency's curve as a RUB bond's (``fallback.at_last_price``: yield, Z-spread
to the curve, risk by core, the price's age in the assumptions); where core cannot (no curve), the ladder has the yield of
its own from the last price and the flows, never the source's, and says why there is no spread (``extra.spread_missing``);
with no price at all neither figure, each with its reason (``extra.yield_missing`` / ``extra.spread_missing``)."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from fx_bonds_619 import BD, DATE, GROUPS, STALE_DATE, secid, synthetic_dir, without_curve
from rub_fixed_income import solve_z_spread

from desk.orchestrator import Outcome, foreign_bond_curves, horizon, prepare, value
from desk.publish import tdm_run
from desk.valuation.bonds import SPREAD_CONVENTION
from desk.valuation.fallback import (
    FOREIGN_PRICE_MISSING,
    OWN_ANALYTICS_TEXT_FX,
    REDEMPTIONS_LABEL,
    SPREAD_MISSING,
    YIELD_MISSING,
    FallbackContext,
    approximate,
    at_last_price,
    foreign_curve_rate,
    price_missing,
)

Q = 1000.0
CURVE = {"EUR": "EUR AAA", "CNY": "CNY XCCY"}
SOURCE_YIELD = 5.1
"""What MOEX publishes in the groups with a yield (``fx_bonds_619.synthetic_dir``): far from the bond's own, which a
price of 97.25 and the flows give about 9.5 %."""


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    return replace(prepare(synthetic_dir(tmp_path_factory.mktemp("fx619")), DATE), proxy_curves=True)


def _value(ctx, groups=GROUPS):
    return {g: o for g, o in zip(groups, value(ctx, [(secid(*g), Q) for g in groups]))}


@pytest.fixture(scope="module")
def full(ctx):
    """Every group on a run that has both currencies' curves."""
    return _value(ctx)


def _independent_z(ctx, v, ccy) -> float:
    """The Z-spread (bp) of the valuation's flows at its dirty price in its currency on the currency's own curve: core's
    ``solve_z_spread`` at the ``SPREAD_CONVENTION``, as core's engine solves it for a foreign bond (the amounts in RUB at one
    rate: the same spread)."""
    rate = ctx.fx.rates[ccy][0]
    curve = ctx.fx.curves[CURVE[ccy]].curve
    z = solve_z_spread([(c.payment_date, c.amount) for c in v.cashflows], v.dirty_rub / rate, curve,
                       settlement_date=v.valuation_date, convention=SPREAD_CONVENTION)
    return z * 1e4


# --- the diagnosis: which group is valued how ------------------------------------------------------------------


def test_the_groups_of_the_issue_before_the_change_are_what_the_issue_says(ctx):
    """The reasons core gives in each group (they did not change): a quote of the business date goes through core on a
    run with the curve; an earlier day's is ``stale_quote``, none ``no_quote``; without the curve ``no_foreign_curve``."""
    out = {g: o.reason for g, o in _value(ctx).items()}
    assert {g: r for g, r in out.items() if g[1] == "today"} == dict.fromkeys([g for g in GROUPS if g[1] == "today"])
    assert {r for g, r in out.items() if g[1] == "stale"} == {"stale_quote"}
    assert {r for g, r in out.items() if g[1] == "none"} == {"no_quote"}
    bare = _value(without_curve(ctx, "EUR"))
    assert {o.reason for g, o in bare.items() if g[:2] == ("EUR", "today")} == {"no_foreign_curve"}
    assert {o.reason for g, o in bare.items() if g[:2] == ("CNY", "today")} == {None}
    assert foreign_bond_curves(ctx) == {"CNY": ("CNY XCCY",), "EUR": ("EUR AAA",)}
    assert "EUR" not in foreign_bond_curves(without_curve(ctx, "EUR"))
    assert set(foreign_bond_curves(replace(ctx, proxy_curves=False))) == {"CNY"}  # the setting off: EUR has none


# --- a price and the curve of the currency: yield and spread, one rule ---------------------------------------


@pytest.mark.parametrize("quote", ["today", "stale"])
@pytest.mark.parametrize("src", ["src", "nosrc"])
@pytest.mark.parametrize("ccy", ["EUR", "CNY"])
def test_a_bond_with_a_price_and_its_currencys_curve_has_yield_and_z_spread(ctx, full, ccy, quote, src):
    o = full[(ccy, quote, src)]
    v = o.valuation
    assert math.isfinite(v.ytm_pct) and math.isfinite(v.z_spread_bp), (v.quality, v.ytm_pct, v.z_spread_bp)
    assert v.discount_curve_id == CURVE[ccy] and v.spread_curve == CURVE[ccy] and v.foreign
    assert "yield_missing" not in v.extra and "spread_missing" not in v.extra
    # the Z-spread of core's definition: the flows at the dirty price on the currency's curve
    assert v.z_spread_bp == pytest.approx(_independent_z(ctx, v, ccy), abs=1e-6)
    # the yield is the bond's own: the same with and without the source's, and not the source's
    assert v.ytm_pct == pytest.approx(full[(ccy, "today", "nosrc")].valuation.ytm_pct, abs=0.01)
    assert abs(v.ytm_pct - SOURCE_YIELD) > 3
    assert v.quality == ("stale_price" if quote == "stale" else "exact" if ccy == "CNY" else "assumed")
    assert math.isfinite(v.dv01_rub) and v.dv01_rub < 0 and set(v.risk()) == {CURVE[ccy]}


@pytest.mark.parametrize("ccy", ["EUR", "CNY"])
def test_a_stale_price_is_valued_by_core_and_says_how_old_it_is(ctx, full, ccy):
    today, stale = full[(ccy, "today", "src")].valuation, full[(ccy, "stale", "src")].valuation
    assert stale.quality == "stale_price" and stale.instrument is not None and stale.cashflows  # by core: its flows
    assert f"last price {STALE_DATE}, 3 days old (legal_close)" in stale.assumptions
    assert "valued by core at this price" in stale.assumptions
    assert stale.valuation_date == BD and stale.clean_pct == pytest.approx(today.clean_pct)
    # the same clean price on the same curve: the spread of the day's own trade, but for the accrual of each (core's
    # for the stale one, MOEX's published for the day's)
    assert stale.z_spread_bp == pytest.approx(today.z_spread_bp, abs=0.2)
    assert stale.ytm_pct == pytest.approx(today.ytm_pct, abs=0.01)
    assert full[(ccy, "stale", "src")].reason == "stale_quote"  # why it is not exact, kept


def test_a_bond_with_no_quote_today_and_a_price_before_is_valued_by_core_at_that_price(ctx, monkeypatch):
    """The group without a quote today but an earlier close in the window: the same as a stale quote."""
    g = ("CNY", "none", "src")
    earlier = replace(_value(ctx)[("CNY", "stale", "src")].quote, secid=secid(*g))
    monkeypatch.setattr(FallbackContext, "past_quotes", lambda self, terms: iter([earlier]))
    v = _value(ctx, [g])[g].valuation
    assert v.quality == "stale_price" and math.isfinite(v.ytm_pct) and math.isfinite(v.z_spread_bp)
    assert f"last price {STALE_DATE}, 3 days old (legal_close)" in v.assumptions and v.discount_curve_id == "CNY XCCY"


# --- a price and no curve: its own yield, no spread and why -------------------------------------------------


@pytest.mark.parametrize("quote", ["today", "stale"])
@pytest.mark.parametrize("src", ["src", "nosrc"])
@pytest.mark.parametrize("ccy", ["EUR", "CNY"])
def test_without_the_curve_the_yield_is_the_bonds_own_and_the_spread_says_why_it_is_empty(ctx, ccy, quote, src):
    run = without_curve(ctx, ccy)
    (o,) = value(run, [(secid(ccy, quote, src), Q)])
    v = o.valuation
    assert v.quality == ("stale_price" if quote == "stale" else "market_value") and not v.cashflows  # the ladder's own
    with_curve = _value(ctx)[(ccy, quote, src)].valuation
    assert v.ytm_pct == pytest.approx(with_curve.ytm_pct, abs=0.01) and abs(v.ytm_pct - SOURCE_YIELD) > 3
    assert OWN_ANALYTICS_TEXT_FX in v.assumptions
    assert math.isnan(v.z_spread_bp) and "yield_missing" not in v.extra
    sm = v.extra["spread_missing"]
    assert sm["reason"] == "no_foreign_curve" and sm["ccy"] == ccy and sm["text"] == SPREAD_MISSING[sm["reason"]].format(ccy=ccy)
    assert sm["text"] in v.assumptions
    # the DV01 of the duration estimate at the bond's own yield (the modified duration), not at a zero rate
    mac = v.macaulay_years
    assert mac == pytest.approx(with_curve.macaulay_years, abs=0.02)
    assert v.dv01_rub == pytest.approx(-mac / (1 + v.ytm_pct / 100) * v.pv_rub * 1e-4)
    assert not any("zero rate" in a for a in v.assumptions)


def test_the_setting_off_is_no_curve_for_a_eur_bond_and_the_xccy_curve_stays_for_a_cny_one(ctx):
    off = replace(ctx, proxy_curves=False)
    got = _value(off, [("EUR", "stale", "src"), ("CNY", "stale", "src")])
    eur, cny = got[("EUR", "stale", "src")].valuation, got[("CNY", "stale", "src")].valuation
    assert math.isnan(eur.z_spread_bp) and eur.extra["spread_missing"]["reason"] == "no_foreign_curve"
    assert math.isfinite(eur.ytm_pct) and math.isfinite(cny.z_spread_bp) and "spread_missing" not in cny.extra


# --- no price: neither, each with its reason -------------------------------------------------------------------


@pytest.mark.parametrize("src", ["src", "nosrc"])
@pytest.mark.parametrize("ccy", ["EUR", "CNY"])
def test_without_a_price_there_is_no_yield_and_no_spread_and_both_say_why(ctx, full, ccy, src):
    v = full[(ccy, "none", src)].valuation
    assert v.quality == "par_proxy" and math.isnan(v.ytm_pct) and math.isnan(v.z_spread_bp) and v.dv01_rub == 0.0
    ym, sm = v.extra["yield_missing"], v.extra["spread_missing"]
    assert (ym["reason"], sm["reason"], sm["ccy"]) == ("no_price", "no_price", ccy)
    assert ym["text"] == YIELD_MISSING["no_price"] and sm["text"] == SPREAD_MISSING["no_price"].format(ccy=ccy)
    assert "ccy" not in ym


# --- the yield cannot be computed: nothing in its place, and the spread says what core said ------------------


def test_a_yield_that_cannot_be_computed_is_empty_not_the_sources(ctx):
    """A quote of a face the schedule does not have: core refuses it (``face_mismatch``) and the monitor's yield needs the
    price on the schedule's face: no yield, though the source gives one; the DV01 is modified by the rate of the currency's
    curve at the duration (the run has one), and says so."""
    o = _value(ctx, [("CNY", "today", "src")])[("CNY", "today", "src")]
    q = replace(o.quote, face_value=Decimal(500))
    out = Outcome(o.secid, o.terms, q, reason="face_mismatch", detail="quote face 500.0 vs static 1000.0")
    v = approximate(ctx, out, Q, FallbackContext(ctx, history=[]))
    assert o.quote.source_analytics["effective_yield_pct"] == "5.1"  # the source has one
    assert math.isnan(v.ytm_pct) and v.extra["yield_missing"]["reason"] == "face_mismatch"
    assert v.extra["yield_missing"]["text"] == YIELD_MISSING["face_mismatch"]
    # the curve is in the run: the spread is empty because core refused, with its words
    sm = v.extra["spread_missing"]
    assert sm["reason"] == "core_refused" and "face_mismatch; quote face 500.0 vs static 1000.0" in sm["text"]
    # the duration is the source's (the bond has no flows' own: said), the rate the XCCY curve's at that duration
    rate, cid = foreign_curve_rate(ctx, "CNY", v.macaulay_years)
    assert cid == "CNY XCCY" and 0 < rate < 0.5
    assert any(f"DV01 modified by the CNY XCCY rate at the duration, {rate * 100:.2f} %" in a
               and "цифра источника, не проверена по потокам" in a for a in v.assumptions)
    assert v.dv01_rub == pytest.approx(-v.macaulay_years / (1 + rate) * v.pv_rub * 1e-4) and "risk_assumption" not in v.extra


def test_without_a_yield_and_without_a_curve_the_dv01_is_at_a_zero_rate_and_says_so_by_a_code(ctx):
    o = _value(ctx, [("CNY", "today", "src")])[("CNY", "today", "src")]
    out = Outcome(o.secid, o.terms, replace(o.quote, face_value=Decimal(500)), reason="face_mismatch", detail="x")
    bare = without_curve(ctx, "CNY")
    v = approximate(bare, out, Q, FallbackContext(bare, history=[]))
    ra = v.extra["risk_assumption"]
    assert (ra["reason"], ra["ccy"]) == ("zero_rate", "CNY") and ra["text"] in v.assumptions
    assert "нет кривой CNY" in ra["text"] and "цифра источника, не проверена по потокам" in ra["text"]
    assert v.dv01_rub == pytest.approx(-v.macaulay_years * v.pv_rub * 1e-4)  # Macaulay x PV: no rate to modify by
    assert v.extra["spread_missing"]["reason"] == "no_foreign_curve"


def test_a_floater_has_no_yield_of_its_own_and_none_of_the_sources(ctx):
    o = _value(ctx, [("EUR", "today", "src")])[("EUR", "today", "src")]
    t = replace(o.terms, coupon_type="floating")
    v = approximate(ctx, Outcome(o.secid, t, o.quote, reason="no_foreign_curve"), Q, FallbackContext(ctx, history=[]))
    assert math.isnan(v.ytm_pct) and v.extra["yield_missing"]["reason"] in ("floater", "schedule_rejected")
    assert "spread_missing" in v.extra and "risk_assumption" not in v.extra  # the EUR curve's rate modifies the DV01
    bare = without_curve(ctx, "EUR")
    v = approximate(bare, Outcome(o.secid, t, o.quote, reason="no_foreign_curve"), Q, FallbackContext(bare, history=[]))
    ra = v.extra["risk_assumption"]
    assert ra["reason"] == "zero_rate_floater" and "до ближайшего пересмотра купона" in ra["text"] and ra["text"] in v.assumptions


def _broken(ctx):
    """A EUR bond of the run whose hub label says its redemptions do not add up to the face (TDM#764), valued by the ladder."""
    o = _value(ctx, [("EUR", "today", "src")])[("EUR", "today", "src")]
    t = replace(o.terms, labels=(*o.terms.labels, REDEMPTIONS_LABEL))
    return approximate(ctx, Outcome(o.secid, t, o.quote, reason="redemption_incomplete"), Q, FallbackContext(ctx, history=[]))


def test_a_foreign_bond_with_a_broken_schedule_says_so_for_its_yield_and_its_spread(ctx):
    """TDM#764: the hub says its redemptions do not add up to the face: no yield, spread or risk of its own; each empty
    figure has the code (``schedule_broken``), beside the risk's (``risk_missing``)."""
    v = _broken(ctx)
    assert math.isnan(v.ytm_pct) and math.isnan(v.z_spread_bp) and v.dv01_rub == 0.0
    assert v.extra["risk_missing"]["reason"] == "bond_redemptions_label"
    assert v.extra["yield_missing"]["reason"] == "schedule_broken" and v.extra["spread_missing"]["reason"] == "schedule_broken"
    assert v.extra["spread_missing"]["ccy"] == "EUR" and v.extra["spread_missing"]["text"] in v.assumptions
    assert v.extra["spread_missing"]["text"] == SPREAD_MISSING["schedule_broken"].format(ccy="EUR")


# --- the rule, over every group and curve state -----------------------------------------------------------------


@pytest.mark.parametrize("without", [None, "EUR", "CNY"])
def test_no_bond_has_a_yield_and_no_spread_while_its_currency_has_a_curve(ctx, without):
    run = ctx if without is None else without_curve(ctx, without)
    curves = foreign_bond_curves(run)
    for g, o in _value(run).items():
        v = o.valuation
        has_curve = g[0] in curves
        yielded, spread = math.isfinite(v.ytm_pct), math.isfinite(v.z_spread_bp)
        priced = g[1] != "none"
        # a price: a yield, always; and a spread when the currency has a curve
        assert yielded == priced, (g, v.quality)
        assert spread == (priced and has_curve), (g, v.quality, without)
        # whatever is empty says why, in the fact of the position
        assert ("yield_missing" in v.extra) == (not yielded), g
        assert ("spread_missing" in v.extra) == (not spread), g
        for key in ("yield_missing", "spread_missing"):
            if key in v.extra:
                assert v.extra[key]["reason"] and v.extra[key]["text"], (g, key)


def test_a_broken_schedule_is_in_the_rule_too(ctx):
    """The rule of the sweep above for the case it does not build: nothing is computed, and each figure says why."""
    v = _broken(ctx)
    for figure, key in ((v.ytm_pct, "yield_missing"), (v.z_spread_bp, "spread_missing")):
        assert not math.isfinite(figure) and v.extra[key]["reason"] == "schedule_broken" and v.extra[key]["text"]


def test_the_reasons_are_in_the_published_fact_of_the_position(ctx):
    bare = without_curve(ctx, "EUR")
    (priced,) = value(bare, [(secid("EUR", "stale", "nosrc"), Q)])
    (unpriced,) = value(bare, [(secid("CNY", "none", "nosrc"), Q)])
    (full_,) = value(bare, [(secid("CNY", "stale", "nosrc"), Q)])
    rows = {o.secid: tdm_run.position_row(bare, "B", o.secid, o, "x") for o in (priced, unpriced, full_)}
    ex = rows[priced.secid]["extra"]
    assert ex["spread_missing"]["reason"] == "no_foreign_curve" and "yield_missing" not in ex
    assert rows[priced.secid]["spread_bp"] is None and rows[priced.secid]["ytm"] == pytest.approx(
        priced.valuation.ytm_pct)
    ex = rows[unpriced.secid]["extra"]
    assert ex["yield_missing"]["reason"] == "no_price" and ex["spread_missing"]["reason"] == "no_price"
    assert rows[unpriced.secid]["ytm"] is None and rows[unpriced.secid]["spread_bp"] is None
    ex = rows[full_.secid]["extra"]  # a bond with both: no reason of either kind
    assert "yield_missing" not in ex and "spread_missing" not in ex and rows[full_.secid]["spread_bp"] is not None



def test_horizon_has_one_request_per_position_when_the_last_price_replaces_the_stale_quote(ctx, monkeypatch):
    """``horizon_bonds`` takes the requests of ``bond_requests`` (a foreign bond's quote of an earlier day passes its checks,
    core refuses it as ``stale_quote``) and those at the ladder's last price: two of one position ID, and core's refusal of
    the first would take the second out with it. The request at the last price replaces the first.

    Core 0.45 carries no foreign-face bond (``core_unsupported_product``, today's quote too): a foreign bond has no carry,
    with that reason, not the stale quote's."""
    import desk.orchestrator as orch

    h = BD + timedelta(days=30)
    (fresh,), _ = horizon(ctx, [(secid("CNY", "today", "src"), Q)], h)
    assert fresh.carry is None and fresh.reason == "core_unsupported_product"
    (own,), _ = horizon(ctx, [(secid("CNY", "stale", "src"), Q)], h)
    assert own.reason == "stale_quote"  # as it was: the ladder takes a bond with a reason given by core only

    taken = orch.at_last_price
    monkeypatch.setattr(orch, "at_last_price", lambda c, pid, o, qty, fb: taken(c, pid, replace(o, reason="stale_quote"), qty, fb))
    (both,), _ = horizon(ctx, [(secid("CNY", "stale", "src"), Q)], h)
    assert both.reason == "core_unsupported_product"  # core was asked about the one at the last price, not the stale one


# --- the gate: what core takes at the last price -----------------------------------------------------------------


def test_a_foreign_bond_takes_an_earlier_days_quote_to_core_and_a_rouble_one_does_not(ctx):
    assert price_missing(_value(ctx)[("EUR", "stale", "src")].terms) == FOREIGN_PRICE_MISSING
    rub = ctx.static.bonds["SU26238RMFS4"]
    assert "stale_quote" not in price_missing(rub) and "stale_quote" in FOREIGN_PRICE_MISSING
    fb = FallbackContext(ctx, history=[])
    o = _value(ctx, [("EUR", "stale", "src")])[("EUR", "stale", "src")]
    for reason, taken in (("stale_quote", True), ("no_price", True), ("no_quote", True), ("no_foreign_curve", False),
                          ("face_mismatch", False)):
        got = at_last_price(ctx, "P", replace(o, reason=reason), Q, fb)
        assert (got is not None) == taken, reason
    # the same refusal of a rouble bond stays on the ladder as before (TDM#383 takes no_price / no_quote only)
    rub_o = Outcome("SU26238RMFS4", rub, replace(o.quote, secid=rub.secid, trade_date=BD - timedelta(3)),
                    reason="stale_quote")
    assert at_last_price(ctx, "P", rub_o, Q, fb) is None


def test_the_valuation_version_says_the_numbers_changed():
    """The numbers of the foreign bonds changed (TDM#619): the version is 37 and its entry says why; the next change of the
    numbers takes the next one and this test with it."""
    from pathlib import Path

    import desk
    from desk import VALUATION_VERSION

    assert VALUATION_VERSION >= 37  # 38 came with TDM#509 on top of it: the entry of 37 stays in the list
    text = Path(desk.__file__).read_text(encoding="utf-8")
    start = text.index("# 37:")
    end = text.find("\n# 38:", start)
    entry = text[start:end if end != -1 else text.index("def logic_version")]
    assert "TDM#619" in entry and "yield_missing" in entry and "spread_missing" in entry
