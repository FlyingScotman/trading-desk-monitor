"""TDM#33: a KBD floater (MOEX ``ZR_YLD_CRV``, the hub's benchmark ``ZCYC``, contract 1.4 terms) valued by core.

The golden fixtures of 15.10.2026 have no KBD floater, so the key-rate floater RU000A109XR1 is given the terms of one
(KBD 2Y + 175 bp, fixed one ZCYC publication day before the period, rounded to 0.01 %) in its static snapshot: its
published running coupon stays cash, the coupons after it project on the KBD."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from rub_fixed_income import FloatingCouponLeg

from desk.marketdata.adapter import KBD_CURVE_ID
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare, value
from desk.publish import summary, tdm_run
from desk.valuation import bonds

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
KEY = "RU000A109XR1"
TERMS = {"benchmark": "ZCYC", "benchmark_raw": "ZR_YLD_CRV", "benchmark_tenor_years": "2", "fixing_anchor": "accrual_start",
         "fixing_offset": 1, "fixing_offset_unit": "zcyc_publication_days", "rate_rounding_decimals": 2,
         "spread_bp": "175.0", "status": "curated"}


@pytest.fixture(scope="module")
def ctx():
    snaps = SnapshotDir(FIX)
    get, cache = snaps.get, {}
    snaps.get = lambda i: cache.setdefault(i, get(i))  # type: ignore[method-assign]  # one static object to edit
    static = snaps.get(snaps.market("2026-10-15", "eod_final").static_snapshot_id)
    next(f for f in static.data["floater_formulas"] if f["secid"] == KEY).update(TERMS)
    return prepare(snaps, "2026-10-15")


@pytest.fixture(scope="module")
def out(ctx):
    (o,) = value(ctx, [(KEY, 1000.0)], position_ids=["P1"], fallback=False)
    return o


def test_kbd_floater_is_valued_by_core_projecting_on_the_kbd(ctx, out):
    v = out.valuation
    assert v is not None and v.quality == "exact" and v.discount_curve_id == bonds.DISCOUNT_CURVE_ID
    leg = v.floater.instrument.coupon_leg
    assert isinstance(leg, FloatingCouponLeg) and leg.projection_curve_id == KBD_CURVE_ID
    assert leg.rule.index == "kbd" and leg.rule.index_tenor_years == 2.0 and leg.rule.fixing_date.offset_days == 1
    assert ctx.rates["fixings"]["KBD"]["tenors"] == [2.0]
    # the coupons after the running one follow the KBD's 2Y forward: risk on RUB OFZ (par buckets) around 2Y-3Y,
    # positive (higher yields, higher coupons), and the discount on RUONIA, negative
    kbd, ruo = v.curve_buckets["RUB OFZ"], v.curve_buckets["RUB RUONIA"]
    assert kbd["2Y"] > 0 and kbd["3Y"] > 0 and math.fsum(ruo.values()) < 0
    # its DM is to the KBD (the projection curve), the Z to RUONIA OIS; no G-spread for a floater
    assert v.discount_margin_bp is not None and abs(v.discount_margin_bp - v.z_spread_bp) > 1.0
    assert v.g_spread_bp is None


def test_kbd_floater_publishes_its_ofz_basis_and_its_ruonia_risk(ctx, out):
    """Core#90: a KBD floater on RUONIA OIS in RUONIA + basis, both computed. Its basis is the projection's KBD par
    risk with RUONIA held, to the bit the plain two-curve run's (``ctx.ofz_basis`` off, variant P); its RUONIA risk
    moves the discount and the projection's KBD by RUONIA's quotes, the basis held (``ofz_ruonia01``), close to variant
    P's discount + projection but computed."""
    import dataclasses

    v = out.valuation
    assert v.ofz_ruonia and v.curve_buckets["RUB OFZ"]
    row = tdm_run.run_content(ctx, [("P1", "B", out)], None, {}).rows[0]
    ex = row["extra"]
    assert ex["ofz_basis01"] == pytest.approx(math.fsum(row["risk"]["RUB OFZ"].values()))
    assert ex["ofz_ruonia01"] == pytest.approx(math.fsum(x for c in row["risk"].values() for x in c.values()))
    assert ex["dm_bp"] == v.discount_margin_bp
    (plain,) = value(dataclasses.replace(ctx, ofz_basis=False), [(KEY, 1000.0)], position_ids=["P1"], fallback=False)
    p = plain.valuation
    assert not p.ofz_ruonia  # variant P: the plain factors, KBD (projection) and RUONIA (discount)
    for b in v.curve_buckets["RUB OFZ"]:
        assert v.curve_buckets["RUB OFZ"][b] == pytest.approx(p.curve_buckets["RUB OFZ"][b], rel=1e-9, abs=1e-9), b
    p_ruonia = math.fsum(x for c in p.curve_buckets.values() for x in c.values())
    assert ex["ofz_ruonia01"] == pytest.approx(p_ruonia, rel=0.05)  # the same order, not the same number
    assert ex["ofz_ruonia01"] != pytest.approx(p_ruonia, rel=1e-6)
    s = summary.build([{"position_id": "P1", "book_id": "B", "pv_rub": row["mv_rub"], "risk": row["risk"],
                        "cs01": row["cs01"], "ofz": row["ofz_spr01"], "instr_type": "BOND", "segment": row["segment"],
                        "asset_class": row["asset_class"], "coupon_type": row["coupon_type"], "ccy": "RUB",
                        "notional": row["notional"], "notional_rub": row["notional_rub"], "price": row["price"],
                        "spread_bp": row["spread_bp"], "dv01": row["dv01"], "dm_bp": ex["dm_bp"], "pnl": None,
                        "ofz_basis": ex["ofz_basis01"], "quality": "exact", "ofz_ruo": ex["ofz_ruonia01"]}])
    # «RUONIA + базис»: RUONIA its computed RUONIA risk, the basis the projection's KBD risk
    assert s["basis"] == pytest.approx(ex["ofz_basis01"]) and s["ofzRuo"] == pytest.approx(ex["ofz_ruonia01"])
    assert s["dv01"]["RUB RUONIA"] == pytest.approx(ex["ofz_ruonia01"])


def test_kbd_floater_flash_pnl_reprojects_on_the_kbd(ctx, out):
    """The flash PnL reprojects its coupons on each step's KBD (``pnl.flash``) and core explains it the same way."""
    from test_pnl_compare import _rows

    (r,) = _rows(ctx, [out])
    assert r.flash and r.core, r.reason
    assert r.core["total"] == pytest.approx(r.identity_total, abs=0.05)
    assert r.delta["carry"] == pytest.approx(0, abs=0.5)


@pytest.mark.parametrize("curve", ["RUB OFZ", "RUB RUONIA"])
def test_kbd_floater_exact_scenario_moves_each_curve_as_its_risk(ctx, out, curve):
    """A scenario on ``RUB OFZ`` moves the KBD floater's basis (``RUB OFZ - RUB RUONIA``, the projection), one on
    ``RUB RUONIA`` moves RUONIA with the KBD's level held (in the scenarios' coordinates, ``bonds.ofz_levels``): the
    repricing matches the row's risk on that curve to the first order."""
    from rub_fixed_income import RISK_BUCKETS

    from desk.scenario.exact import core_scenario, run_curves

    shifts = [0.0 if b in ("1M", "3M", "6M") else 1.0 for b in RISK_BUCKETS]
    sc = core_scenario("t", {"curves": {curve: shifts}}, 0.0, run_curves(ctx))
    (o,) = value(ctx, [(KEY, 1000.0)], position_ids=["P1"], fallback=False, scenarios=(sc,))
    pnl = o.core_result.scenarios[0].positions[0].pnl_rub
    taylor = math.fsum(out.valuation.curve_buckets[curve][b] for b in RISK_BUCKETS if b not in ("1M", "3M", "6M"))
    assert pnl == pytest.approx(taylor, rel=0.02, abs=0.5)


def test_a_floater_without_dm_says_why():
    """TDM#33: the card and C11 say why a floater has no DM history (``positions_history`` ``dmNote``)."""
    from desk.api.concepts import dm_missing

    fb = {"ccy": "RUB", "extra": {"quality": "market_value", "not_exact_reason": "floater_benchmark_unsupported"}}
    assert dm_missing(fb) == ("DM нет: ни в одном прогоне истории флоатер не оценён ядром (оценка по рыночной стоимости "
                              "(без ядра); floater_benchmark_unsupported)")
    assert dm_missing({"ccy": "USD", "extra": {}}) == "DM по цене считается только у рублёвых флоатеров"


def _window_ctx(monkeypatch, history: bool):
    """The floater fixed 3 publication days before its next period (from 19.10): on 15.10 that fixing (14.10) has
    passed and MOEX has not published the coupon. ``history``: the KBD of 07.10-14.10 in the cache (the 15.10
    parameters dated those days: the fixture has one date)."""
    import dataclasses
    from datetime import date, timedelta

    from desk.curves import rates

    snaps = SnapshotDir(FIX)
    get, cache = snaps.get, {}
    snaps.get = lambda i: cache.setdefault(i, get(i))  # type: ignore[method-assign]
    market = snaps.market("2026-10-15", "eod_final")
    static = snaps.get(market.static_snapshot_id)
    next(f for f in static.data["floater_formulas"] if f["secid"] == KEY).update({**TERMS, "fixing_offset": 3})
    if history:
        z = market.data["zcyc_params"][0]
        base = rates.zcyc_parameters(z["trade_date"], z["params"])
        days = [date(2026, 10, 15) - timedelta(days=i) for i in range(1, 9)]
        hist = {d: dataclasses.replace(base, trade_date=d) for d in days if d.weekday() < 5}
        monkeypatch.setattr(rates, "kbd_history", lambda *a: hist)
    return prepare(snaps, "2026-10-15")


def test_a_coupon_fixed_but_not_published_comes_from_the_kbd_history(monkeypatch):
    """Review of #245: between the next period's fixing and MOEX publishing its coupon core takes the fixing from the
    KBD history of earlier snapshots (``rates.kbd_history``); without it the bond falls to the fallback ladder for
    those days of every period, saying why."""
    ctx = _window_ctx(monkeypatch, history=True)
    (o,) = value(ctx, [(KEY, 1000.0)], fallback=False)
    assert o.valuation is not None and o.valuation.quality == "exact", o.detail
    assert ctx.rates["fixings"]["KBD"]["rows"] == 6 and ctx.rates["fixings"]["KBD"]["latest"] == "2026-10-14"
    assert math.fsum(o.valuation.curve_buckets["RUB OFZ"].values()) > 0


def test_without_the_kbd_history_the_window_is_a_rejection(monkeypatch):
    from desk.curves import rates

    monkeypatch.setattr(rates, "kbd_history", lambda *a: {})
    ctx = _window_ctx(monkeypatch, history=False)
    (o,) = value(ctx, [(KEY, 1000.0)], fallback=False)
    assert o.valuation is None and o.reason == "core_missing_market_data"


def test_kbd_history_reads_the_last_dates_of_the_cache():
    """``kbd_history``: the last ``max fixing_offset + 5`` market dates before the run, eod_final before preliminary."""
    from datetime import date

    from desk.curves import rates
    from desk.marketdata.bundle import SnapshotError

    snaps = SnapshotDir(FIX)
    real = snaps.market("2026-10-15", "eod_final")
    zcyc = real.data["zcyc_params"][0]
    dates = [f"2026-10-{d:02d}" for d in (1, 2, 5, 6, 7, 8, 9, 12, 13, 14, 15)]

    class Fake:
        def market_dates(self, cut):
            return dates if cut == "eod_final" else ["2026-10-14"]

        def market_id(self, d, cut):
            if cut == "eod_final" and d == "2026-10-14":
                raise SnapshotError("no final")
            return f"{cut}-{d}"

        def get(self, sid):
            if sid == "eod_final-2026-10-13":  # an unreadable final: the preliminary of the date instead
                raise SnapshotError("corrupt bundle")
            return type("M", (), {"data": {"zcyc_params": [{**zcyc, "trade_date": sid[-10:]}]}})()

    static = type("S", (), {"data": {"floater_formulas": [{"benchmark": "ZCYC", "fixing_offset": 2}]}})()
    h = rates.kbd_history(Fake(), "2026-10-15", static)
    assert sorted(h) == [date(2026, 10, d) for d in (6, 7, 8, 9, 12, 13, 14)]  # 2 + 5 dates, the 14th preliminary
    assert rates.kbd_history(Fake(), "2026-10-15", type("S", (), {"data": {"floater_formulas": []}})()) == {}


def _ctx_with(monkeypatch, terms: dict, history: bool = True):
    """The fixture's KEY floater as a KBD floater with ``terms`` (over ``TERMS``) and, with ``history``, the 15.10 KBD
    dated 07.10-14.10 as the cache's history."""
    import dataclasses
    from datetime import date, timedelta

    from desk.curves import rates

    snaps = SnapshotDir(FIX)
    get, cache = snaps.get, {}
    snaps.get = lambda i: cache.setdefault(i, get(i))  # type: ignore[method-assign]
    market = snaps.market("2026-10-15", "eod_final")
    static = snaps.get(market.static_snapshot_id)
    next(f for f in static.data["floater_formulas"] if f["secid"] == KEY).update({**TERMS, **terms})
    z = market.data["zcyc_params"][0]
    base = rates.zcyc_parameters(z["trade_date"], z["params"])
    days = [date(2026, 10, 15) - timedelta(days=i) for i in range(1, 9)]
    hist = {d: dataclasses.replace(base, trade_date=d) for d in days if d.weekday() < 5} if history else {}
    monkeypatch.setattr(rates, "kbd_history", lambda *a: hist)
    return prepare(snaps, "2026-10-15")


def test_calendar_days_fixing_on_a_weekend_takes_the_last_publication_before(monkeypatch):
    """TDM#252: a fixing ``N`` calendar days before the period that falls on no ZCYC publication day takes the last one
    before it, as the hub's ``verify/zcycfloat.fixing_date`` does: core's ``FixingDateRule`` in calendar days observes the
    latest business day of the fixing calendar on or before (``observation_days`` business). The period from 19.10 (a
    Monday) fixed 8 calendar days before falls on Sunday 11.10: core reads Friday 09.10 from the history, exactly."""
    ctx = _ctx_with(monkeypatch, {"fixing_offset": 8, "fixing_offset_unit": "calendar_days"})
    (o,) = value(ctx, [(KEY, 1000.0)], fallback=False)
    assert o.valuation is not None and o.valuation.quality == "exact", o.detail
    assert o.valuation.floater.instrument.coupon_leg.rule.fixing_date.unit == "calendar_days"
    ctx_no = _ctx_with(monkeypatch, {"fixing_offset": 8, "fixing_offset_unit": "calendar_days"}, history=False)
    (o_no,) = value(ctx_no, [(KEY, 1000.0)], fallback=False)
    assert o_no.valuation is None and o_no.reason == "core_missing_market_data"  # Friday's fixing is what it needs


def test_margin_steps_by_the_spread_schedule(monkeypatch):
    """TDM#252: a margin step of the hub's ``spread_schedule`` (at a put or a call offer) applies from its period on
    (``adapter.period_margin``, core ``FloatingCouponPeriod.margin``): +50 bp from the period of 18.12.2026 raises
    each projected coupon from then on by face x 0.5 % x its accrual, the earlier ones unchanged."""
    from datetime import date

    from desk.marketdata.adapter import period_margin

    step = [{"effective_from": "2024-01-01", "spread_bp": "175.0", "basis": "document"},
            {"effective_from": "2026-12-18", "spread_bp": "225.0", "basis": "document"}]
    flat = _ctx_with(monkeypatch, {})
    (o0,) = value(flat, [(KEY, 1000.0)], fallback=False)
    stepped = _ctx_with(monkeypatch, {"spread_schedule": step})
    (o1,) = value(stepped, [(KEY, 1000.0)], fallback=False)
    t = stepped.static.bonds[KEY]
    assert period_margin(t, date(2026, 11, 18)) == pytest.approx(0.0175)
    assert period_margin(t, date(2026, 12, 18)) == pytest.approx(0.0225)
    assert period_margin(flat.static.bonds[KEY], date(2026, 12, 18)) is None
    f0 = {c.payment_date: c.amount for c in o0.valuation.cashflows}
    f1 = {c.payment_date: c.amount for c in o1.valuation.cashflows}
    periods = {p.payment_date: p for p in o1.valuation.floater.instrument.coupon_leg.periods}
    for d, a in f1.items():
        p = periods.get(d)
        if p is None or p.published_coupon_rub is not None:
            continue  # the principal, a published coupon: as before
        days = (p.accrual_end - p.accrual_start).days
        extra = p.notional_rub * 0.005 * days / 365 if p.accrual_start >= date(2026, 12, 18) else 0.0
        assert a - f0[d] == pytest.approx(extra, abs=1e-6), d
    # the price is the market's: richer coupons at the same price are a wider spread
    assert o1.valuation.pv_rub == pytest.approx(o0.valuation.pv_rub, rel=1e-9)
    assert o1.valuation.z_spread_bp > o0.valuation.z_spread_bp + 1.0
