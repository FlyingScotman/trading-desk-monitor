"""TDM#768: a bond of a currency without a curve (RUS-30, XS0114288789: USD, no curve in the run) whose hub-corrected
redemption schedule is complete has its yield and duration computed by the monitor from the flows and the price, with
core's functions; MOEX computed its own on the rounded schedule (53.8 % at a close yield of 6.8 %) and the hub lists
them as unreliable (``BondQuote.source_analytics_unreliable``, ``Instrument.source_analytics_unreliable_reasons``,
contract 1.10). Without a complete schedule (a broken one, a floater) the bond has none, as after TDM#764."""

from __future__ import annotations

import copy
import math
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from desk.api.concepts import risk_table
from desk.marketdata.adapter import Coupon, MarketView, Redemption, StaticView
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import Outcome, prepare, value
from desk.publish import tdm_run
from desk.valuation.bonds import MANUAL_OVERRIDE_LABEL, ROUNDED_LABEL, fixed_schedule
from desk.valuation.fallback import (
    BROKEN_SCHEDULE_TEXT,
    OWN_ANALYTICS_TEXT,
    OWN_ANALYTICS_TEXT_CURVE,
    OWN_ANALYTICS_TEXT_FX,
    REDEMPTIONS_LABEL,
    FallbackContext,
    approximate,
    unreliable_source,
    yield_pnl,
)

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
Q = 1000.0
AMORT = "RU000A10BRM5"  # an amortizing bond with a complete schedule, USD face; MOEX's yield 9.8989 % on it is right
FRN = "RU000A109XR1"  # a rouble floater
RUS30 = "XS0114288789"
UNRELIABLE = "bond_source_analytics_unreliable"
REASONS = (ROUNDED_LABEL, REDEMPTIONS_LABEL)
FIELDS = ["effective_yield_pct", "yield_close_pct", "duration_days", "zspread_bp"]
MOEX_WRONG = {"effective_yield_pct": "53.8", "yield_close_pct": "6.79", "duration_days": "505"}
PAYS = [date(2027, 3, 31), date(2027, 9, 30), date(2028, 3, 31), date(2028, 9, 30), date(2029, 3, 31),
        date(2029, 9, 30), date(2030, 3, 31)]
FACE, STEP = Decimal("0.035"), Decimal("0.005")


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), "2026-10-15")


@pytest.fixture(scope="module")
def exact(ctx):
    return {o.secid: o for o in value(ctx, [(s, Q) for s in sorted(ctx.static.bonds)])}


def _d360(a: date, b: date) -> int:
    return (b.year - a.year) * 360 + (b.month - a.month) * 30 + (min(b.day, 30) - min(a.day, 30))


def _rus30_flows():
    """RUS-30's flows after 2026-09-30 on the right schedule: seven amortizations of 0.005, a coupon of 7.5 % a year
    (30/360) on the face outstanding: [(date, coupon, amortization)]."""
    out, prev, f = [], date(2026, 9, 30), FACE
    for p in PAYS:
        out.append((p, f * Decimal("0.075") * _d360(prev, p) / 360, STEP))
        prev, f = p, f - STEP
    return out


def _rus30(o, *, step=STEP, labels=(MANUAL_OVERRIDE_LABEL,), reasons=REASONS, clean="101.4372", fields=tuple(FIELDS),
           analytics=None, trade_date=None, amounts=False, edit=None, day_count="30E/360", day_count_source="cbonds"):
    """The shape of RUS-30 on the fixture's bond, as MOEX ISS publishes it: a face of 0.035 USD, seven redemptions
    (``step``), coupons of 7.5 % semiannual; ``facevalue`` is 0.035 on EVERY period, paid ones too (the current face, not
    the period's); a paid coupon's ``value`` is computed on the face of ITS period (0.0035625 on 0.095, 0.001875 on 0.05,
    0.0016875 on 0.045, 2025-03-31 an anomaly 0.006187); the coupons not yet known have ``value`` None and ``valueprc`` 7.5;
    ``amounts``: the future amounts given by the source (the right ones); ``edit(i, coupon)``: a change of the i-th future
    coupon. ``day_count`` the static's (Cbonds, ``30E/360`` for the live RUS-30). The hub's manual override (``labels``),
    the static's reasons and the quote's fields as the hub 1.10 says them; MOEX's analytics are the wrong ones. Outcome of
    the exact path's refusal for want of a curve."""
    paid = [(date(2024, 9, 30), date(2024, 3, 31), "0.0035625"), (date(2025, 3, 31), date(2024, 9, 30), "0.006187"),
            (date(2025, 9, 30), date(2025, 3, 31), "0.001875"), (date(2026, 3, 31), date(2025, 9, 30), "0.0016875")]
    coupons = [Coupon(i + 1, st, pay, None, Decimal("7.5"), Decimal(v), "known", FACE) for i, (pay, st, v) in enumerate(paid)]
    coupons.append(Coupon(5, date(2026, 3, 31), date(2026, 9, 30), None, Decimal("7.5"), None, "known", FACE))
    prev, f = date(2026, 9, 30), FACE
    for i, p in enumerate(PAYS):
        c = Coupon(i + 6, prev, p, None, Decimal("7.5"), f * Decimal("0.075") * _d360(prev, p) / 360 if amounts else None,
                   "known", FACE)
        coupons.append(edit(i, c) if edit else c)
        prev, f = p, f - STEP
    reds = tuple(Redemption(p, "maturity" if p == PAYS[-1] else "amortization", step) for p in PAYS)
    t = replace(o.terms, secid=RUS30, face_value=FACE, initial_face_value=Decimal("0.095"), maturity_date=PAYS[-1],
                coupons=tuple(coupons), redemptions=reds, labels=labels, coupon_frequency=2,
                source_analytics_unreliable_reasons=tuple(reasons), day_count=day_count,
                day_count_source=day_count_source)
    q = replace(o.quote, secid=RUS30, face_value=FACE, accrued_per_bond=None, prices_pct={"legal_close": Decimal(clean)},
                source_analytics=analytics or MOEX_WRONG,
                source_analytics_unreliable={"fields": list(fields), "reasons": ["moex_schedule_rounded"]} if fields else None,
                **({"trade_date": trade_date} if trade_date else {}))
    return Outcome(RUS30, t, q, reason="no_foreign_curve")


def _dc(terms, name="ACT/365F"):
    """The fixture bond with the day count its static would state (the fixtures publish none)."""
    return replace(terms, day_count=name, day_count_source="cbonds")


def _fb(ctx):
    return FallbackContext(ctx, history=[])


def _independent(bd: date, clean: float, valuation_date=None):
    """The effective annual yield (%) and Macaulay duration (years) of RUS-30 by bisection over the dated flows
    (ACT/365), the dirty price its coupon accrued ACT/365 on the rate (as the ladder's), written here, not core's."""
    face = 0.035
    acc = 0.075 * face * (bd - date(2026, 9, 30)).days / 365
    dirty = clean / 100 * face + acc
    flows = [(p, float(c + a)) for p, c, a in _rus30_flows()]

    def pv(y, w=lambda t: 1):
        return sum(w((p - bd).days / 365) * a / (1 + y) ** ((p - bd).days / 365) for p, a in flows)

    lo, hi = -0.5, 5.0
    for _ in range(200):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if pv(mid) > dirty else (lo, mid)
    return mid * 100, pv(mid, lambda t: t) / dirty, dirty


def test_rus30_has_its_yield_duration_and_risk_from_the_flows_and_the_price(ctx, exact):
    out = _rus30(exact[AMORT])
    v = approximate(ctx, out, Q, _fb(ctx))
    y, mac, dirty = _independent(ctx.business_date, 101.4372)
    assert v.quality == "market_value" and v.pv_rub / Q / v.dirty_rub == pytest.approx(1)
    assert 6.7 < v.ytm_pct < 6.9
    assert v.ytm_pct == pytest.approx(y, abs=0.01)  # 1 bp
    assert v.macaulay_years == pytest.approx(mac, abs=0.005)
    assert v.modified_duration == pytest.approx(mac / (1 + y / 100), abs=0.005)
    assert 1.7 < v.macaulay_years < 1.9  # the right schedule: about 1.8 years, MOEX's 505 days are 1.38
    pv = v.pv_rub
    assert v.dv01_rub == pytest.approx(-v.modified_duration * pv * 1e-4, rel=1e-9) and v.dv01_rub < 0
    assert v.spread01_rub == pytest.approx(v.dv01_rub, rel=1e-9)
    assert sum(v.curve_buckets["USD"].values()) == pytest.approx(v.dv01_rub) and v.buckets
    assert dirty * 83.76 == pytest.approx(v.dirty_rub, rel=1e-3)  # the value is the price's, in RUB
    assert "risk_missing" not in v.extra
    assert OWN_ANALYTICS_TEXT in v.assumptions
    assert not any("не считаются" in a or "MOEX" in a and "days" in a for a in v.assumptions)


def test_the_coupons_of_rus30_are_filled_by_the_schedules_face_unrounded_in_the_statics_day_count(ctx, exact):
    """``bonds._fill_coupons`` (TDM#770): a coupon of a known rate and no amount is the rate x the face outstanding at the
    period's START by the redemption schedule (not the source's ``facevalue``, 0.035 on every period) x days / basis of the
    static's day count, unrounded (at 0.01 each was 0.00)."""
    t = _rus30(exact[AMORT]).terms
    got = fixed_schedule(t, ctx.business_date)
    right = [c for _, c, _ in _rus30_flows()]
    assert [c.amount_per_bond for c in got.coupons] == right and all(c.status == "known" for c in got.coupons)
    assert [c.amount_per_bond for c in got.coupons][-1] == Decimal("0.005") * Decimal("0.075") * 180 / 360
    assert any("coupon amount from rate x face of the period's start x days/basis (30E/360): 7 coupon(s)" in n
               for n in got.assumptions)
    act = fixed_schedule(replace(t, day_count="ACTUAL/365 (ACTUAL/365F)"), ctx.business_date).coupons
    assert act[0].amount_per_bond == FACE * Decimal("0.075") * 182 / 365 and act[0].amount_per_bond != right[0]


def test_a_bond_without_a_supported_day_count_has_its_rate_only_coupons_assumed_at_their_own_rate(ctx, exact):
    t = _rus30(exact[AMORT], day_count=None).terms
    got = fixed_schedule(t, ctx.business_date)
    assert all(c.status == "assumed" for c in got.coupons)
    assert any("not computed from it" in n and "assumed at their rate" in n for n in got.assumptions)


def test_a_step_up_rate_of_a_coupon_without_a_day_count_is_not_lost_and_the_static_day_count_is_used(ctx, exact):
    """A coupon of a known rate and no amount that cannot be computed from it (no day count) is assumed at ITS rate, not
    the last known one's (a step-up), and does not refuse the bond for want of an earlier rate; with the day count stated
    (but no start) the assumed amount is in that day count, else days / 365."""
    def step_up(i, c):
        return replace(c, rate_pct=Decimal(9) if i == 3 else c.rate_pct)
    t = _rus30(exact[AMORT], day_count=None, edit=step_up).terms
    got = fixed_schedule(t, ctx.business_date).coupons
    assert [c.rate_pct for c in got] == [Decimal("7.5")] * 3 + [Decimal(9)] + [Decimal("7.5")] * 3
    assert got[3].amount_per_bond == (Decimal("0.02") * Decimal(9) / 100 * (date(2028, 9, 30) - date(2028, 3, 31)).days
                                      / 365)
    future_only = replace(t, coupons=tuple(c for c in t.coupons if c.pay_date > ctx.business_date))
    only = fixed_schedule(future_only, ctx.business_date).coupons  # no earlier rate: no coupon_not_known, its own rate is known
    assert [c.rate_pct for c in only][:3] == [Decimal("7.5")] * 3 and all(c.status == "assumed" for c in only)


def test_assumed_coupons_are_computed_in_the_static_day_count_when_there_is_one(ctx, exact):
    """A coupon of no rate at all (unknown) is assumed at the last known rate on the face of the period's start, in the
    static's day count (30E/360: 180 / 360), not days / 365."""
    out = _rus30(exact[AMORT], edit=lambda i, c: replace(c, rate_pct=None, status="unknown") if i == 0 else c)
    first = fixed_schedule(out.terms, ctx.business_date).coupons[0]
    assert first.status == "assumed" and first.rate_pct == Decimal("7.5")
    assert first.amount_per_bond == FACE * Decimal("0.075") * 180 / 360
    act = fixed_schedule(replace(out.terms, day_count=None), ctx.business_date).coupons[0]
    assert act.amount_per_bond == FACE * Decimal("0.075") * 182 / 365


def test_the_day_count_names_of_the_static_and_30_360_against_30e_360():
    from desk.valuation.bonds import COUPON_DAY_COUNTS as D
    from desk.valuation.bonds import coupon_day_count
    a, b = date(2026, 1, 29), date(2026, 3, 31)
    assert D["30/360"](a, b) == (62, 360) and D["30E/360"](a, b) == (61, 360)
    assert D["ACT/365F"](a, b) == (61, 365) and D["ACT/360"](a, b) == (61, 360)
    names = {"30E/360": "30E/360", "30/360": "30/360", "ACTUAL/365 (ACTUAL/365F)": "ACT/365F", " act/365f ": "ACT/365F",
             "ACT/360": "ACT/360", "ACT/ACT ICMA": None, "30/360 GERMAN": None, "30/360 ISDA": None, "": None, None: None}

    class T:
        day_count = None
    for raw, want in names.items():
        T.day_count = raw
        assert coupon_day_count(T) == want, raw


def test_the_day_count_of_the_static_decides_the_flows_and_the_paid_coupons_do_not(ctx, exact):
    v30 = approximate(ctx, _rus30(exact[AMORT]), Q, _fb(ctx))
    v365 = approximate(ctx, _rus30(exact[AMORT], day_count="ACT/365F"), Q, _fb(ctx))
    assert "risk_missing" not in v365.extra and v365.ytm_pct != pytest.approx(v30.ytm_pct, abs=1e-6)
    assert v365.ytm_pct == pytest.approx(v30.ytm_pct, abs=0.05)
    raw = approximate(ctx, _rus30(exact[AMORT], day_count="ACTUAL/365 (ACTUAL/365F)"), Q, _fb(ctx))  # a raw Cbonds title
    assert raw.ytm_pct == pytest.approx(v365.ytm_pct, abs=1e-9)
    assert any("(ACT/365F)" in a and "coupon amount from rate" in a for a in raw.assumptions)  # the schedule's notes are said


def test_a_coupon_projected_by_moex_is_not_known(ctx, exact):
    out = _rus30(exact[AMORT], amounts=True, edit=lambda i, c: replace(c, status="projected_by_moex") if i < 2 else c)
    v = approximate(ctx, out, Q, _fb(ctx))
    assert math.isnan(v.ytm_pct) and v.extra["risk_missing"]["reason"] == UNRELIABLE and v.dv01_rub == 0.0
    assert "Собственный расчёт монитора не вышел: купоны не известны:" in v.extra["risk_missing"]["text"]
    assert v.extra["risk_missing"]["text"] in v.assumptions


def test_a_coupon_without_the_rate_or_the_amount_of_its_period_is_not_known(ctx, exact):
    out = _rus30(exact[AMORT], edit=lambda i, c: replace(c, rate_pct=None, status="unknown") if i == 3 else c)
    v = approximate(ctx, out, Q, _fb(ctx))
    assert math.isnan(v.ytm_pct) and OWN_ANALYTICS_TEXT not in v.assumptions
    assert "купоны не известны: 1 подставлено." in v.extra["risk_missing"]["text"]


@pytest.mark.parametrize("day_count", [None, "", "ACT/ACT ICMA", "30/360 GERMAN"])
def test_a_day_count_not_stated_or_not_supported_does_not_stop_a_bond_whose_sums_the_source_gives(ctx, exact, day_count):
    """All the sums are the source's: no coupon is computed from a rate, the day count is not needed."""
    v = approximate(ctx, _rus30(exact[AMORT], amounts=True, day_count=day_count), Q, _fb(ctx))
    y, _, _ = _independent(ctx.business_date, 101.4372)
    assert "risk_missing" not in v.extra and v.ytm_pct == pytest.approx(y, abs=0.01)


@pytest.mark.parametrize("day_count", [None, "ACT/ACT ICMA"])
def test_without_the_sums_and_the_day_count_the_coupons_are_assumed_and_there_is_no_own_yield(ctx, exact, day_count):
    v = approximate(ctx, _rus30(exact[AMORT], day_count=day_count), Q, _fb(ctx))
    assert math.isnan(v.ytm_pct) and v.extra["risk_missing"]["reason"] == UNRELIABLE and v.dv01_rub == 0.0
    assert "Собственный расчёт монитора не вышел: купоны не известны: 7 подставлено." in v.extra["risk_missing"]["text"]


def test_amounts_the_source_gives_are_used_as_they_are(ctx, exact):
    given = approximate(ctx, _rus30(exact[AMORT], amounts=True), Q, _fb(ctx))
    computed = approximate(ctx, _rus30(exact[AMORT]), Q, _fb(ctx))
    assert given.ytm_pct == pytest.approx(computed.ytm_pct, abs=1e-6) and "risk_missing" not in given.extra


def test_a_perpetual_with_a_coupon_of_no_source_and_a_bond_of_projected_coupons_are_not_computed(ctx, exact):
    for secid, text in (("RU000A1034P7", "купоны не известны: 1 подставлено"),
                        ("RU000A10GCT1", "купоны не известны:")):
        o = exact[secid]
        q = replace(o.quote, source_analytics_unreliable={"fields": FIELDS, "reasons": ["x"]})
        v = approximate(ctx, Outcome(secid, o.terms, q, reason="no_price"), Q, _fb(ctx))
        assert math.isnan(v.ytm_pct) and v.extra["risk_missing"]["reason"] == UNRELIABLE
        assert text in v.extra["risk_missing"]["text"]


def test_the_numbers_of_the_source_are_not_used_at_all(ctx, exact):
    a = approximate(ctx, _rus30(exact[AMORT]), Q, _fb(ctx))
    b = approximate(ctx, _rus30(exact[AMORT], analytics={"effective_yield_pct": "-250", "yield_close_pct": "1",
                                                          "duration_days": "9999"}), Q, _fb(ctx))
    c = approximate(ctx, _rus30(exact[AMORT], analytics={}), Q, _fb(ctx))
    for v in (b, c):
        assert (v.ytm_pct, v.macaulay_years, v.modified_duration, v.dv01_rub) == (
            a.ytm_pct, a.macaulay_years, a.modified_duration, a.dv01_rub)


def test_core_functions_are_the_only_maths_the_yield_reprices_the_dirty_price(ctx, exact):
    """The yield and duration are core's (``solve_effective_annual_yield``, ``bond_duration``): the dirty price in USD
    discounted by the yield on the flows ACT/365 gives back the price."""
    v = approximate(ctx, _rus30(exact[AMORT]), Q, _fb(ctx))
    bd = ctx.business_date
    flows = [(p, float(c + a)) for p, c, a in _rus30_flows()]
    dirty_usd = v.dirty_rub / 83.76
    assert sum(a / (1 + v.ytm_pct / 100) ** ((p - bd).days / 365) for p, a in flows) == pytest.approx(dirty_usd, rel=2e-4)


def test_the_unreliable_flag_of_the_static_alone_is_enough_a_stale_price_from_an_older_snapshot(ctx, exact):
    """The quote of a snapshot before 1.10 (a stale rung's last price) carries no flag: the bond's static does."""
    bd = ctx.business_date
    full = approximate(ctx, _rus30(exact[AMORT]), Q, _fb(ctx))
    out = _rus30(exact[AMORT], fields=(), trade_date=bd - timedelta(days=3))
    assert out.quote.source_analytics_unreliable is None
    v = approximate(ctx, out, Q, _fb(ctx))
    assert v.quality == "stale_price" and "risk_missing" not in v.extra
    assert v.ytm_pct == pytest.approx(full.ytm_pct, abs=0.01) and OWN_ANALYTICS_TEXT in v.assumptions
    assert v.macaulay_years == pytest.approx(full.macaulay_years, abs=0.01) and v.dv01_rub != 0.0


def test_the_flag_of_the_quote_alone_is_enough_a_hub_without_the_static_field_says_it_on_the_quote(ctx, exact):
    v = approximate(ctx, _rus30(exact[AMORT], reasons=()), Q, _fb(ctx))
    assert "risk_missing" not in v.extra and OWN_ANALYTICS_TEXT in v.assumptions and 6.7 < v.ytm_pct < 6.9


@pytest.mark.parametrize("reasons,fields", [((), ()), ((), ["zspread_bp", "gspread_bp"])])
def test_an_old_hub_or_fields_the_ladder_does_not_read_have_the_monitors_own_yield_too(ctx, exact, reasons, fields):
    """No flag (TDM#619): the same yield and duration of the monitor's own from the flows and the price. Before, MOEX's
    (here the wrong 53.8 % and 505 days) stood where the monitor could not tell that they were wrong; a foreign bond's
    yield is never the source's now, so there is nothing to tell."""
    v = approximate(ctx, _rus30(exact[AMORT], reasons=reasons, fields=fields), Q, _fb(ctx))
    assert 6.7 < v.ytm_pct < 6.9 and "risk_missing" not in v.extra
    assert OWN_ANALYTICS_TEXT_FX in v.assumptions and OWN_ANALYTICS_TEXT not in v.assumptions
    assert not any("MOEX 505 days" in a for a in v.assumptions)


def test_a_broken_schedule_is_still_not_computed(ctx, exact):
    """The rounded schedule of MOEX (seven of 0.01, the hub's labels): no flows to compute from, as after TDM#764."""
    out = _rus30(exact[AMORT], step=Decimal("0.01"), labels=REASONS)
    v = approximate(ctx, out, Q, _fb(ctx))
    assert math.isnan(v.ytm_pct) and math.isnan(v.macaulay_years) and v.dv01_rub == 0.0
    assert v.extra["risk_missing"]["reason"] == "bond_redemptions_rounded"
    assert BROKEN_SCHEDULE_TEXT["bond_redemptions_rounded"] in v.assumptions and OWN_ANALYTICS_TEXT not in v.assumptions


def test_a_schedule_that_does_not_add_up_without_a_label_is_not_computed_either(ctx, exact):
    out = replace(_rus30(exact[AMORT], step=Decimal("0.01"), labels=()), reason="redemption_incomplete")
    v = approximate(ctx, out, Q, _fb(ctx))
    assert math.isnan(v.ytm_pct) and v.extra["risk_missing"]["reason"] == "bond_redemptions_incomplete"


def test_a_floater_without_known_coupons_is_not_computed_and_says_why(ctx, exact):
    o = exact[FRN]
    sa = {**o.quote.source_analytics, **MOEX_WRONG}
    q = replace(o.quote, source_analytics=sa, source_analytics_unreliable={"fields": FIELDS, "reasons": ["x"]})
    v = approximate(ctx, Outcome(FRN, o.terms, q, reason="no_price"), Q, _fb(ctx))
    assert math.isnan(v.ytm_pct) and math.isnan(v.macaulay_years) and v.dv01_rub == 0.0 and not any(v.buckets.values())
    assert v.extra["risk_missing"]["reason"] == UNRELIABLE
    assert "Собственный расчёт монитора не вышел: флоатер" in v.extra["risk_missing"]["text"]
    assert OWN_ANALYTICS_TEXT not in v.assumptions and OWN_ANALYTICS_TEXT_CURVE not in v.assumptions


def test_a_price_on_another_face_than_the_flows_is_not_computed(ctx, exact):
    """A quote of face 0.07 against a schedule of 0.035: the yield of a price of one face on the flows of the other
    would be a figure of nothing."""
    out = _rus30(exact[AMORT])
    out = replace(out, quote=replace(out.quote, face_value=Decimal("0.07")))
    v = approximate(ctx, out, Q, _fb(ctx))
    assert math.isnan(v.ytm_pct) and v.extra["risk_missing"]["reason"] == UNRELIABLE
    assert "цена дана к номиналу 0.07, потоки — на номинале 0.035" in v.extra["risk_missing"]["text"]


def test_a_price_no_yield_reproduces_is_not_computed(ctx, exact):
    """A price of 10^13 % of the face against flows of a few cents: no root in core's yield domain, and nothing in its
    place."""
    v = approximate(ctx, _rus30(exact[AMORT], clean="10000000000000"), Q, _fb(ctx))
    assert math.isnan(v.ytm_pct) and v.extra["risk_missing"]["reason"] == UNRELIABLE
    assert "никакая доходность не воспроизводит цену" in v.extra["risk_missing"]["text"]


def test_where_moex_is_right_the_monitors_yield_is_close_to_moexs(ctx, exact):
    """The amortizer of the fixture (USD, its schedule complete, MOEX computes on it right): the monitor's yield and
    duration are a few bp and a few days from MOEX's, so a flag does not move a sound bond."""
    o = exact[AMORT]
    moex_y, moex_dur = float(o.quote.source_analytics["effective_yield_pct"]), float(o.quote.source_analytics["duration_days"])
    q = replace(o.quote, source_analytics_unreliable={"fields": FIELDS, "reasons": ["x"]})
    own = approximate(ctx, Outcome(AMORT, _dc(o.terms), q, reason="no_foreign_curve"), Q, _fb(ctx))
    moex = approximate(ctx, Outcome(AMORT, o.terms, o.quote, reason="no_foreign_curve"), Q, _fb(ctx))
    # TDM#619: the unflagged bond's yield is its own too, not MOEX's figure (which stays in the quote for a comparison)
    assert moex.ytm_pct == pytest.approx(moex_y, abs=0.03) and moex.ytm_pct != moex_y
    assert OWN_ANALYTICS_TEXT not in moex.assumptions and OWN_ANALYTICS_TEXT_FX in moex.assumptions
    assert own.ytm_pct == pytest.approx(moex_y, abs=0.03)  # 3 bp
    assert own.macaulay_years * 365 == pytest.approx(moex_dur, abs=3)
    assert own.dv01_rub == pytest.approx(moex.dv01_rub, rel=0.01) and own.pv_rub == moex.pv_rub
    assert OWN_ANALYTICS_TEXT in own.assumptions and "risk_missing" not in own.extra


@pytest.mark.parametrize("secid", ["RU000A0JXQ44", "SU26238RMFS4", "SU46020RMFS2", "RU000A100GW5"])
def test_the_own_yield_and_duration_are_the_ones_core_gives_on_the_exact_path(ctx, exact, secid):
    """For a bond core values (a call or put horizon, a fixed bond to maturity, a never-called perpetual with its tail),
    the same flows (the coupons the source states) and the same price give core's own ``ytm_pct`` and Macaulay duration: the monitor's are core's
    functions on the flows ``_schedule`` builds, not other maths. (A bond core values at the price of the day: its dirty
    price is the quote's, as the ladder's.)"""
    o = exact[secid]
    q = replace(o.quote, source_analytics_unreliable={"fields": FIELDS, "reasons": ["x"]})
    v = approximate(ctx, Outcome(secid, _dc(o.terms), q, reason="no_price"), Q, _fb(ctx))
    assert "risk_missing" not in v.extra and OWN_ANALYTICS_TEXT_CURVE in v.assumptions
    assert v.ytm_pct == pytest.approx(o.valuation.ytm_pct, abs=0.005)  # within half a bp
    assert v.macaulay_years == pytest.approx(o.valuation.macaulay_years, rel=2e-3)


def test_a_rouble_bond_with_its_curve_says_the_source_fields_are_the_reason(ctx, exact):
    """A bond with a curve of its currency that falls to the ladder: the text does not say «кривой валюты нет»."""
    o = exact["RU000A0JXQ44"]
    q = replace(o.quote, source_analytics_unreliable={"fields": FIELDS, "reasons": ["x"]})
    v = approximate(ctx, Outcome(o.secid, _dc(o.terms), q, reason="no_price"), Q, _fb(ctx))
    assert v.quality in ("market_value", "stale_price") and "risk_missing" not in v.extra
    assert OWN_ANALYTICS_TEXT_CURVE in v.assumptions and OWN_ANALYTICS_TEXT not in v.assumptions
    assert math.isfinite(v.ytm_pct) and math.isfinite(v.macaulay_years) and v.dv01_rub != 0.0
    assert v.modified_duration == pytest.approx(v.macaulay_years / (1 + v.ytm_pct / 100))  # a pair, as the exact path's
    plain = approximate(ctx, Outcome(o.secid, o.terms, o.quote, reason="no_price"), Q, _fb(ctx))
    assert v.dv01_rub == pytest.approx(plain.dv01_rub) and (v.pv_rub, v.dirty_rub) == (plain.pv_rub, plain.dirty_rub)


def test_the_flash_carry_of_the_bond_is_at_its_own_yield(ctx, exact):
    """``yield_pnl`` carries a foreign bond at its D-1 ``ytm_pct``: the monitor's own now, so RUS-30 has a carry."""
    bd = ctx.business_date
    out = _rus30(exact[AMORT])
    cur = approximate(ctx, out, Q, _fb(ctx))
    prev = replace(cur, valuation_date=bd - timedelta(1), settlement=bd - timedelta(1), dirty_rub=cur.dirty_rub * 0.998,
                   pv_rub=cur.pv_rub * 0.998)
    p = yield_pnl(prev, cur, out.terms, ctx, ctx)
    assert p.method == "desk-flash-yield-v1" and p.carry != 0.0


def test_the_row_has_the_yield_and_the_risk_and_is_not_in_the_list_of_those_without(ctx, exact):
    out = _rus30(exact[AMORT])
    out = replace(out, valuation=approximate(ctx, out, Q, _fb(ctx)))
    c = tdm_run.run_content(ctx, [("P1", "MIXED", out)], None, {})
    (row,) = c.rows
    assert row["ytm"] == pytest.approx(out.valuation.ytm_pct) and row["mdur"] is not None and row["dv01"] != 0.0
    assert "risk_missing" not in row["extra"] and OWN_ANALYTICS_TEXT in row["extra"]["assumptions"]
    fx = {"USD": ctx.market.fx("USD")["moex_19"]}
    t = risk_table([{"position_id": "P1", "fact": row}], fx)
    assert t["total"]["n_risk_missing"] == 0 and t["total"]["dv01_all_curves_rub"] != 0.0


def test_end_to_end_the_exact_path_refuses_for_want_of_a_curve_and_the_ladder_computes(ctx, exact, monkeypatch):
    """Through ``orchestrator.value``: the bond of RUS-30's shape in the static and the market of the run."""
    out = _rus30(exact[AMORT])
    monkeypatch.setitem(ctx.static.bonds, AMORT, replace(out.terms, secid=AMORT))
    monkeypatch.setitem(ctx.market.quotes, (AMORT, out.quote.board), replace(out.quote, secid=AMORT))
    (res,) = value(ctx, [(AMORT, Q)])
    v = res.valuation
    assert res.reason == "no_foreign_curve" and v.quality == "market_value" and "risk_missing" not in v.extra
    assert 6.7 < v.ytm_pct < 6.9 and v.dv01_rub != 0.0 and OWN_ANALYTICS_TEXT in v.assumptions


def test_unreliable_source_reads_the_quote_and_the_static(exact):
    o = exact[AMORT]
    t_flag, q_flag = replace(o.terms, source_analytics_unreliable_reasons=("x",)), replace(
        o.quote, source_analytics_unreliable={"fields": ["duration_days"], "reasons": ["x"]})
    assert unreliable_source(o.quote, o.terms) is None and unreliable_source(None, o.terms) is None
    assert unreliable_source(o.quote, t_flag) == UNRELIABLE and unreliable_source(None, t_flag) == UNRELIABLE
    assert unreliable_source(q_flag, o.terms) == UNRELIABLE and unreliable_source(q_flag) == UNRELIABLE
    assert unreliable_source(replace(o.quote, source_analytics_unreliable={"fields": ["zspread_bp"], "reasons": []})) is None


def test_the_static_view_reads_the_reasons_and_a_hub_without_them_as_before(ctx):
    snap = copy.deepcopy(ctx.static.snapshot)
    rows = snap.doc["data"]["instruments"]
    assert all("source_analytics_unreliable_reasons" not in r for r in rows)  # a hub before 1.10
    assert all(not t.source_analytics_unreliable_reasons for t in StaticView(snap).bonds.values())
    rows[0]["source_analytics_unreliable_reasons"] = [ROUNDED_LABEL, REDEMPTIONS_LABEL]
    rows[1]["source_analytics_unreliable_reasons"] = []
    rows[2]["source_analytics_unreliable_reasons"] = None
    new = StaticView(snap).bonds
    assert new[rows[0]["secid"]].source_analytics_unreliable_reasons == (ROUNDED_LABEL, REDEMPTIONS_LABEL)
    assert new[rows[1]["secid"]].source_analytics_unreliable_reasons == ()
    assert new[rows[2]["secid"]].source_analytics_unreliable_reasons == ()
    assert MarketView  # the quote's field is read as in test_fallback_yield_764


def test_the_static_view_reads_the_day_count_and_a_hub_without_it_as_none(ctx):
    snap = copy.deepcopy(ctx.static.snapshot)
    rows = snap.doc["data"]["instruments"]
    assert all(not t.day_count and not t.day_count_source for t in StaticView(snap).bonds.values())  # the fixtures: none
    for r in rows:
        r.pop("day_count", None)
        r.pop("day_count_source", None)
    assert all(t.day_count is None for t in StaticView(snap).bonds.values())  # a hub before the field
    rows[0]["day_count"], rows[0]["day_count_source"] = "30E/360", "cbonds"
    rows[1]["day_count"] = None
    new = StaticView(snap).bonds
    assert (new[rows[0]["secid"]].day_count, new[rows[0]["secid"]].day_count_source) == ("30E/360", "cbonds")
    assert new[rows[1]["secid"]].day_count is None
