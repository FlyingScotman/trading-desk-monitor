"""TDM#466: the day-over-day PnL of a derivative valued by core takes the cash its trades paid or settled in (D-1, D].

Core counts a swap flow up to its payment date and an FX leg up to its value date, so the coupon paid on D, the last
flow of a trade that ended and a leg that settled are in PV D-1 and not in PV D. Without the cash they showed as a
jump: a swap's coupon as a false PnL in ``other``, the last day of a trade and an FX swap's near leg as a loss of their
whole PV. On the hub's contract 1.4 golden snapshot (15.10.2026 is D, a copy one day earlier with the same curves is
D-1, so the change of PV is the day's carry and the flows) and synthetic trades (invented amounts and rates).
"""

from __future__ import annotations

import copy
import dataclasses
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from rub_fixed_income import FixingEntry
from test_slice_s2 import shifted_prev

from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import Outcome, prepare
from desk.pnl import direct
from desk.pnl.reasons import NO_PNL_TEXT
from desk.publish import tdm_run
from desk.store import PositionInputs
from desk.valuation import derivatives as dv
from desk.valuation import positions as pv1

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1_4"
BD = date(2026, 10, 15)
PREV = date(2026, 10, 14)


def _with_fixings_from_september(ctx):
    """The golden fixings start on 01.10: a swap whose period began in September needs the weekdays before them."""
    out = []
    for f in ctx.core_market.fixings:
        h = f.history
        first_day, first_rate = h.fixings[0]
        early = tuple((d, first_rate) for n in range(40) if (d := date(2026, 9, 1) + timedelta(days=n)) < first_day
                      and d.weekday() < 5)
        out.append(FixingEntry(f.fixing_id, dataclasses.replace(h, fixings=early + h.fixings)))
    return dataclasses.replace(ctx, core_market=dataclasses.replace(ctx.core_market, fixings=tuple(out)))


@pytest.fixture(scope="module")
def ctx():
    return _with_fixings_from_september(prepare(SnapshotDir(FIX), BD.isoformat()))


@pytest.fixture(scope="module")
def prev(ctx):
    """The same market one day earlier (no curve move: the change of PV is time and flows, not the market)."""
    p = shifted_prev(ctx, PREV, 0.0)
    m = copy.copy(ctx.market)
    m.business_date = PREV
    m.fx = ctx.market.fx  # the rates of D-1 are D's: no FX move between the two dates
    return dataclasses.replace(p, market=m)


def leg(tid, leg_id, side, **kw):
    return {"trade_id": tid, "leg_id": leg_id, "book_id": "IRB", "product_kind": "ois", "trade_date": "2026-09-10",
            "side": side, "ccy": "RUB", "notional_ccy": "1000000000", "start_date": "2026-09-14",
            "maturity_date": "2027-09-14", "payment_frequency": "1M", "day_count": "ACT/365", **kw}


def swap(tid, maturity="2027-09-14", **kw):
    return [leg(tid, "fix", "pay", fixed_rate_pct="15.5", maturity_date=maturity, **kw),
            leg(tid, "flt", "receive", index_code="RUONIA", maturity_date=maturity, **kw)]


def repo(tid, side, start, end, **kw):
    return {"trade_id": tid, "book_id": "IRB", "product_kind": "repo", "trade_date": start, "side": side,
            "ccy": "RUB", "notional_ccy": "500000000", "start_date": start, "maturity_date": end, **kw}


def fx_leg(tid, leg_id, side, nb, nq, vd):
    return {"trade_id": tid, "leg_id": leg_id, "book_id": "IRB", "product_kind": "fx_swap", "trade_date": "2026-10-01",
            "value_date": vd, "base_ccy": "CNY", "quote_ccy": "RUB", "side": side, "notional_base": str(nb),
            "notional_quote": str(nq)}


def inputs(valid, terms, pid, **extra):
    x = {"asset_class": "otc", "ccy": "RUB", "source_instrument_id": pid, "deal_id": pid, **extra}
    return PositionInputs([(pid, "IRB", None, None)], [], {"IRB": valid}, datetime.now(UTC), "0" * 64, {pid: x},
                          trade_terms=list(terms))


def day(prev, ctx, terms, pid, **extra):
    """(outcome on D, outcome on D-1) with the PnL attached by ``pnl.direct``."""
    inp = inputs(ctx.business_date, terms, pid, **extra)
    prev_inp = inputs(prev.business_date, terms, pid, **extra)
    bp, pbp = pv1.plan(ctx, inp), pv1.plan(prev, prev_inp)
    direct.attach(prev, ctx, bp.direct, inp, pbp.direct, prev_inp.valid_dates)
    return bp.direct[pid], pbp.direct[pid]


def test_a_coupon_paid_on_d_is_cash_not_a_loss(ctx, prev):
    """The monthly coupon of an OIS is paid on D: PV D lacks it, PV D-1 has it. The PnL is the change of PV plus the
    coupon, the day's carry, not the coupon itself."""
    o, p = day(prev, ctx, swap("S1"), "S1")
    (flow,) = [f for f in p.valuation.extra["flows"] if f["date"] == BD.isoformat()]
    jump = o.valuation.pv_rub - p.valuation.pv_rub
    assert flow["ccy"] == "RUB" and flow["trade_id"] == "S1" and abs(flow["amount"]) > 1e6  # a month's coupon
    assert abs(jump + flow["amount"]) < 1e4 < abs(jump)  # the PV falls by the coupon paid out
    pnl = o.pnl
    assert pnl.received_rub == pytest.approx(flow["amount"])
    assert pnl.total == pytest.approx(jump + flow["amount"]) and abs(pnl.total) < 1e5  # a day of carry
    assert pnl.explained == pytest.approx(pnl.total) and pnl.extra()["received_rub"] == pnl.received_rub
    assert "received" in pnl.note and o.no_pnl is None
    assert o.valuation.extra["flows"] == []  # nothing more is paid within the next 16 days (the next is a month away)


def test_the_last_day_of_a_swap_has_a_pnl(ctx, prev):
    """A swap that matures on D (its last coupon is paid on D): core has no flows left; the row is valued as settled
    (PV 0) and the PnL of its last day is the cash less the PV of D-1."""
    o, p = day(prev, ctx, swap("S1", "2026-10-14"), "S1")
    v = o.valuation
    assert v.pv_rub == 0.0 and v.extra["core"]["model_flags"] == ["settled"] and v.extra["core"]["product"] == "ois"
    assert v.extra["settled"] == [{"trade_id": "S1", "date": "2026-10-15"}] and v.extra["flows"] == []
    assert v.dv01_rub == 0.0 and v.curve_buckets == {} and v.quality == "assumed"
    (flow,) = [f for f in p.valuation.extra["flows"] if f["date"] == BD.isoformat()]
    assert abs(p.valuation.pv_rub) > 1e6  # without the flow the PnL would be the loss of the whole PV
    assert o.pnl.total == pytest.approx(-p.valuation.pv_rub + flow["amount"]) and abs(o.pnl.total) < 1e5
    # the row is published as a swap: its side, a rates row (not an FX one)
    row = tdm_run.direct_row(ctx, "IRB", "S1", o, {"asset_class": "otc", "ccy": "RUB"}, None)
    assert row["instr_type"] != "FX" and row["side"] == "PAY" and row["mv_rub"] == 0.0
    assert row["extra"]["pnl"]["received_rub"] == o.pnl.received_rub
    # the day after: settled on both dates (D-1 is the settled row itself), nothing to carry, no jump
    nxt, _ = day(ctx, _at(ctx, date(2026, 10, 16)), swap("S1", "2026-10-14"), "S1")
    assert nxt.valuation.extra["settled"] and nxt.pnl.total == 0.0 and nxt.pnl.received_rub == 0.0


def test_the_last_day_of_a_repo(ctx, prev):
    """A reverse REPO ends on D: core says «settled» and the row is valued as settled; the PnL of the day is the
    repurchase amount received less the PV of D-1, a day of interest (the end amount of a fixed-rate REPO is known)."""
    terms = [repo("R1", "reverse_repo", "2026-10-09", "2026-10-15", fixed_rate_pct="16.1", day_count="ACT/365")]
    o, p = day(prev, ctx, terms, "R1")
    assert o.valuation.pv_rub == 0.0 and o.valuation.extra["rates_settled"] == [{"trade_id": "R1", "side": "REVERSE_REPO"}]
    (flow,) = p.valuation.extra["flows"]
    end = p.valuation.extra["repos"][0]["end_amount_rub"]
    assert flow["date"] == BD.isoformat() and flow["amount"] == pytest.approx(end) and end > 5e8
    assert o.pnl.total == pytest.approx(-p.valuation.pv_rub + end) and 0 < o.pnl.total < 5e5  # not -5e8


def test_an_fx_swap_the_day_after_its_near_leg(ctx, prev):
    """The near leg of an FX swap settled on D-1: core drops it on D, its PV is in PV D-1 only. The PnL takes the
    cash of the leg (CNY bought, RUB paid) at the D-1 rate, so the leg's PV is not a loss, and the FX delta of D-1
    less the leg settled is what the rate moves on."""
    terms = [fx_leg("F1", "near", "buy", 50e6, 550e6, PREV.isoformat()),  # at 11.0: worth far more than its price
             fx_leg("F1", "far", "sell", 50e6, 640e6, "2027-01-19")]
    o, p = day(prev, ctx, terms, "F1", asset_class="fx", ccy="CNY", amount_ccy=0.0)
    legs = {x["leg"] for x in p.valuation.extra["legs"]}
    assert legs == {"near", "far"} and {x["leg"] for x in o.valuation.extra["legs"]} == {"far"}
    assert any("settled legs left out" in a for a in o.valuation.assumptions)
    near = [f for f in p.valuation.extra["flows"] if f["date"] == PREV.isoformat()]
    assert {f["ccy"]: f["amount"] for f in near} == {"CNY": 50e6, "RUB": -550e6}
    rate = ctx.fx.rates["CNY"][0]
    cash = 50e6 * rate - 550e6
    assert o.no_pnl is None, o.no_pnl.detail
    assert o.pnl.received_rub == pytest.approx(cash) and cash > 7e7
    jump = o.valuation.pv_rub - p.valuation.pv_rub
    assert jump < -5e7  # the PV of the near leg gone
    assert abs(o.pnl.total) < 5e6  # settled cash instead of a 70 million loss
    # no rate moved between the two dates: the FX part is 0, the rest is the leg's time value and points
    assert o.pnl.fx_gross == pytest.approx(0.0, abs=1.0) and o.pnl.other == pytest.approx(o.pnl.total)


def test_a_forward_with_every_leg_settled_and_an_ndf(ctx, prev):
    """A deliverable forward whose only leg settled on D-1 is valued as settled on D (PV 0); an NDF's cash amount is
    not recorded: no PnL, the reason said (never 0)."""
    fwd = [{**fx_leg("F2", None, "buy", 10e6, 110e6, PREV.isoformat()), "product_kind": "fx_forward"}]
    o, p = day(prev, ctx, fwd, "F2", asset_class="fx", ccy="CNY", amount_ccy=0.0)
    assert o.valuation.pv_rub == 0.0 and o.valuation.extra["core"]["product"] == "fx_forward"
    assert abs(o.pnl.total) < 2e6 < abs(p.valuation.pv_rub)
    ndf = [{**fx_leg("F3", None, "buy", 10e6, 110e6, PREV.isoformat()), "product_kind": "fx_forward", "ndf": True,
            "settle_ccy": "RUB", "fixing_date": PREV.isoformat(), "fixing_source": "CBR"}]
    o, p = day(prev, ctx, ndf, "F3", asset_class="fx", ccy="CNY", amount_ccy=0.0)
    assert o.valuation.extra.get("core") and o.pnl is None
    assert o.no_pnl.reason == "received_unknown" and "NDF" in o.no_pnl.detail
    assert "received_unknown" in NO_PNL_TEXT


def test_a_trade_settled_long_ago_keeps_the_reports_rule(ctx):
    """Only a trade whose last flow is within the flow window is valued as settled: an old one is «settled» as before."""
    old = [fx_leg("F4", None, "buy", 10e6, 110e6, (BD - timedelta(days=dv.FLOW_DAYS + 5)).isoformat())]
    old[0]["product_kind"] = "fx_forward"
    o = pv1.plan(ctx, inputs(BD, old, "F4", asset_class="fx", ccy="CNY", amount_ccy=0.0)).direct["F4"]
    assert o.core_not_valued[0] == "settled" and not (o.valuation and o.valuation.extra.get("core"))


def test_flows_that_could_not_be_read_are_said(prev, ctx):
    """A D-1 valuation without its flows (core could not project them) gives no PnL, with the reason."""
    o, p = day(prev, ctx, swap("S1"), "S1")
    p.valuation = dataclasses.replace(p.valuation, extra={**p.valuation.extra, "flows": None})
    with pytest.raises(direct.NoPnl) as e:
        direct._core_fx(prev, ctx, o, p)
    assert e.value.reason == "received_unknown"
    p.valuation = dataclasses.replace(p.valuation, extra={**p.valuation.extra, "flows": []})
    far = dataclasses.replace(ctx, market=copy.copy(ctx.market))
    far.market.business_date = PREV + timedelta(days=dv.FLOW_DAYS + 1)
    with pytest.raises(direct.NoPnl) as e:
        direct._core_fx(prev, far, o, p)
    assert e.value.reason == "received_unknown" and str(dv.FLOW_DAYS) in e.value.detail


def _ctx(d, usd):
    from types import SimpleNamespace

    return SimpleNamespace(business_date=d, market=SimpleNamespace(
        fx=lambda ccy: {"cbr": usd, "cbr_next": None, "moex_tom": None, "moex_19": usd}))


def test_the_settled_leg_is_out_of_the_fx_delta_and_its_cash_is_at_the_d1_rate():
    """USD 1m of FX delta, all of it a leg that settled on D-1 (cash 1m USD against 80m RUB, PV 5m before it); the
    rate moved 80 -> 82 after: that move is the cash row's, not the derivative's. The change of PV is the leg's PV
    gone, the cash at 80."""
    from types import SimpleNamespace

    d0, d1 = _ctx(PREV, 85.0), _ctx(BD, 82.0)
    flows = [{"trade_id": "T", "kind": "fx", "date": PREV.isoformat(), "out_on": BD.isoformat(), "ccy": "USD",
              "amount": 1e6},
             {"trade_id": "T", "kind": "fx", "date": PREV.isoformat(), "out_on": BD.isoformat(), "ccy": "RUB",
              "amount": -80e6}]
    p = Outcome(None, None, None, valuation=SimpleNamespace(
        pv_rub=5e6, extra={"core": True, "flows": flows}, fx_delta={"USD": 1e6}))
    o = Outcome(None, None, None, valuation=SimpleNamespace(pv_rub=0.0, extra={"core": True}, fx_delta={}))
    pnl = direct._core_fx(d0, d1, o, p)
    assert pnl.received_rub == pytest.approx(5e6) and pnl.total == pytest.approx(0.0)  # 1m x 85 - 80m - 5m + 0
    assert pnl.fx_gross == 0.0 and pnl.other == pytest.approx(0.0)
    # a flow outside (D-1, D] is not taken: the leg settles after D
    later = [{**f, "out_on": (BD + timedelta(days=1)).isoformat()} for f in flows]
    p.valuation.extra["flows"] = later
    o.valuation.fx_delta = {"USD": 1e6}
    again = direct._core_fx(d0, d1, o, p)
    assert again.received_rub == 0.0 and again.fx_gross == pytest.approx(1e6 * (82.0 - 85.0))


def _at(ctx, d):
    """The golden market as of another date (no curve move)."""
    p = shifted_prev(ctx, d, 0.0)
    m = copy.copy(ctx.market)
    m.business_date = d
    m.fx = ctx.market.fx
    return dataclasses.replace(p, market=m)


def test_a_swap_ending_on_a_weekend_pays_after_the_runs_between(ctx):
    """Maturity on Saturday 10.10: the end is adjusted to Monday and the payment (lag of one business day) falls on
    Tuesday 13.10. Monday's run still has the swap (its last payment is ahead): no jump from Friday. Tuesday's run
    has the settled row, and the last coupon is the flow of Monday's valuation, not lost."""
    fri, mon, tue = (_at(ctx, date(2026, 10, d)) for d in (9, 12, 13))
    terms = swap("S1", "2026-10-10", start_date="2026-09-10")
    o, _ = day(fri, mon, terms, "S1")
    assert o.valuation.extra.get("settled") is None and o.valuation.pv_rub != 0.0
    assert o.pnl.received_rub == 0.0 and abs(o.pnl.total) < 1e5 < abs(o.valuation.pv_rub)  # no jump
    (flow,) = [f for f in o.valuation.extra["flows"] if f["date"] == "2026-10-13"]
    t, _ = day(mon, tue, terms, "S1")
    assert t.valuation.pv_rub == 0.0 and t.valuation.extra["settled"][0]["date"] == "2026-10-13"
    assert t.pnl.received_rub == pytest.approx(flow["amount"])
    assert t.pnl.total == pytest.approx(-o.valuation.pv_rub + flow["amount"]) and abs(t.pnl.total) < 1e5


def test_a_row_with_a_settled_trade_and_a_live_one(ctx, prev):
    """The settled trade is left out and its last coupon is in the PnL; the live one is valued; the report's PV covers
    both, so no difference with it is published."""
    terms = [{**r, "position_id": "P"} for r in (*swap("S1", "2026-10-14"), *swap("S2"))]
    o, p = day(prev, ctx, terms, "P", mv_rub=-1e7)
    assert any("settled trades left out" in a for a in o.valuation.assumptions)
    assert "reported_pv_rub" not in o.valuation.extra and "pv_diff_rub" not in o.valuation.extra
    assert [x["trade_id"] for x in o.valuation.extra["swaps"]] == ["S2"]
    prev_flows = sum(f["amount"] for f in p.valuation.extra["flows"] if f["date"] == BD.isoformat())
    assert len(p.valuation.extra["swaps"]) == 2 and o.pnl.received_rub == pytest.approx(prev_flows)
    assert abs(o.pnl.total) < 1e5


def test_an_fx_row_with_an_amount_keeps_the_rule_of_cash(ctx, prev):
    """A settled FX row that reports an amount is valued as before (PV = amount x rate, FX delta, for MV and the
    OCP), not as settled PV 0; its PnL takes the legs' cash and the cash row's own FX move, without a jump."""
    terms = [{**fx_leg("F5", None, "buy", 10e6, 110e6, PREV.isoformat()), "product_kind": "fx_forward"}]
    o, p = day(prev, ctx, terms, "F5", asset_class="fx", ccy="CNY", amount_ccy=10e6)
    rate = ctx.fx.rates["CNY"][0]
    v = o.valuation
    assert not v.extra.get("core") and o.core_not_valued[0] == "settled" and v.pv_rub == pytest.approx(10e6 * rate)
    cash = 10e6 * rate - 110e6
    assert o.pnl.received_rub == pytest.approx(cash) and cash > 1e7
    assert abs(o.pnl.total) < 2e6 < abs(p.valuation.pv_rub)  # a jump of the whole PV of the leg is gone
    assert o.pnl.fx_gross == pytest.approx(0.0, abs=1.0) and o.pnl.explained == pytest.approx(o.pnl.total)


def test_a_swap_matured_long_ago_is_settled_not_bad_terms(ctx):
    """Dates before the run's calendars used to fail the schedule (terms_invalid) once the early exit was gone."""
    for maturity in ("2021-09-14", "2025-12-30", "1991-01-01"):
        t = swap("S1", maturity, start_date="2020-09-14" if maturity != "1991-01-01" else "1990-01-01")
        o = pv1.plan(ctx, inputs(BD, t, "S1")).direct["S1"]
        assert o.core_not_valued[0] == "settled", (maturity, o.core_not_valued)


def test_a_schedule_outside_the_calendar_of_a_swap_that_ended_is_settled(ctx, monkeypatch):
    """A swap that ended just before the business date whose dates the calendar does not cover (the first days of
    January after a December end): settled, so that its last coupon is in the PnL; not a terms error."""
    def out_of_range(*a, **k):
        raise ValueError("date outside the calendar")

    monkeypatch.setattr(dv, "swap_schedule", out_of_range)
    (rows,) = [swap("S1", (BD - timedelta(days=10)).isoformat(), start_date="2026-08-14")]
    o = pv1.plan(ctx, inputs(BD, rows, "S1")).direct["S1"]
    assert o.core_not_valued is None or o.core_not_valued[0] != "terms_invalid"
    assert o.valuation is not None and o.valuation.extra["core"]["model_flags"] == ["settled"]
    assert o.valuation.extra["settled"][0]["date"] == (BD - timedelta(days=10)).isoformat()
    # still ahead of the business date: the error is the terms', not a settled swap's
    live = pv1.plan(ctx, inputs(BD, swap("S2", (BD + timedelta(days=10)).isoformat(), start_date="2026-08-14"),
                                "S2")).direct["S2"]
    assert live.core_not_valued[0] == "terms_invalid"


def test_the_settled_cash_row_moves_with_the_rate():
    """The legs' flows are at the D-1 rate, the cash held after them moves with the rate: 10m CNY bought at 11.0,
    the rate 12.0 -> 12.5. Trade: -PV(D-1) + cash at 12.0; cash row: 10m x 0.5."""
    from types import SimpleNamespace

    def cx(d, r):
        return SimpleNamespace(business_date=d, market=SimpleNamespace(
            fx=lambda ccy: {"cbr": r, "cbr_next": None, "moex_tom": None, "moex_19": r}))

    d0, d1 = cx(PREV, 12.0), cx(BD, 12.5)
    flows = [{"trade_id": "T", "kind": "fx", "date": PREV.isoformat(), "out_on": BD.isoformat(), "ccy": "CNY",
              "amount": 10e6},
             {"trade_id": "T", "kind": "fx", "date": PREV.isoformat(), "out_on": BD.isoformat(), "ccy": "RUB",
              "amount": -110e6}]
    p = Outcome(None, None, None, valuation=SimpleNamespace(pv_rub=10e6, extra={"core": True, "flows": flows},
                                                            fx_delta={"CNY": 10e6}))
    o = Outcome(None, None, None, valuation=SimpleNamespace(pv_rub=10e6 * 12.5, quantity=10e6, face_rub=12.5,
                                                            extra={}, quality="assumed"))
    pnl = direct._settled_cash(d0, d1, o, p, {"ccy": "CNY"})
    assert pnl.received_rub == pytest.approx(10e6)  # 10m x 12.0 - 110m
    assert pnl.fx_gross == pytest.approx(10e6 * 0.5) and pnl.total == pytest.approx(0.0 + 5e6)  # trade 0, cash 5m
    assert pnl.other == pytest.approx(0.0) and pnl.explained == pytest.approx(pnl.total)


def test_a_refused_horizon_stops_the_pnl_only_when_its_date_is_in_the_window(ctx, prev, monkeypatch):
    """Core refuses the horizon of every payment date: the flows of the row record the dates as unknown. A payment on D
    (in the window) gives no PnL with the reason; one due later (outside it) leaves the PnL as it is."""
    def refused(**k):
        raise ValueError("core refused the horizon")

    monkeypatch.setattr(dv, "horizon_portfolio", refused)
    o, p = day(prev, ctx, swap("S1"), "S1")  # pays on 15.10, in (14.10, 15.10]
    assert o.pnl is None and o.no_pnl.reason == "received_unknown" and "15" in o.no_pnl.detail
    assert [f["amount"] for f in p.valuation.extra["flows"]] == [None]
    later = swap("S2", "2027-09-20", start_date="2026-09-20")  # pays on 21.10, after D
    o, p = day(prev, ctx, later, "S2")
    assert [(f["amount"], f["date"]) for f in p.valuation.extra["flows"]] == [(None, "2026-10-21")]
    assert o.no_pnl is None and o.pnl.received_rub == 0.0 and abs(o.pnl.total) < 1e5
