"""Slice S4 fallback ladder on the golden fixtures: every rung on a constructed case, and every
position published with its quality."""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_slice_s2 import shifted_prev

import desk.orchestrator as orch
from desk.marketdata.adapter import Redemption
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import Outcome, attach_pnl, prepare, value
from desk.publish import tdm_run
from desk.report import validation
from desk.valuation import fallback
from desk.valuation.bonds import DISCOUNT_CURVE_ID as RUONIA
from desk.valuation.bonds import Rejected, face_on, fixed_schedule
from desk.valuation.fallback import (
    FallbackContext,
    SimplePnl,
    approximate,
    fx_rate,
    fx_rate_cbr,
    simple_pnl,
)

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
Q = 1000.0
RZD, FRN, USD, UNKNOWN = "RU000A0JXQ44", "RU000A109XR1", "RU000A10BRM5", "RU000A10GCT1"
OFZ_PK = "SU29008RMFS8"


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), "2026-10-15")


@pytest.fixture(scope="module")
def exact(ctx):
    return {o.secid: o for o in value(ctx, [(s, Q) for s in sorted(ctx.static.bonds)])}


class _History:
    """An earlier market snapshot that holds ``quote`` for every bond, dated ``days`` before."""

    def __init__(self, ctx, days):
        self.ctx, self.days = ctx, days

    def quote(self, terms):
        q = self.ctx.market.quote(terms)
        return q and replace(q, trade_date=q.trade_date - timedelta(self.days))


def _fb(ctx, history=()):
    return FallbackContext(ctx, history=list(history))


def _outcome(o, reason, **kw):
    return Outcome(o.secid, o.terms, kw.get("quote", o.quote), reason=reason)


def test_market_value_without_accrued_keeps_exact_value_and_risk(ctx, exact):
    o = exact[RZD]
    no_accrued = replace(o.quote, accrued_per_bond=None)
    v = approximate(ctx, _outcome(o, "no_accrued", quote=no_accrued), Q, _fb(ctx))
    e = o.valuation
    assert v.quality == "market_value" and any("accrued computed" in a for a in v.assumptions)
    assert v.pv_rub == pytest.approx(e.pv_rub, rel=1e-9)  # accrued 0: the period starts on the trade date
    assert v.dv01_rub == pytest.approx(e.dv01_rub, rel=1e-6)
    assert v.spread01_rub == pytest.approx(e.spread01_rub, rel=1e-6)


def test_market_value_floater_on_duration(ctx, exact):
    for sid, kind in ((FRN, "credit"), (OFZ_PK, "ofz")):
        o = exact[sid]
        v = approximate(ctx, o, Q, _fb(ctx))
        clean = float(o.quote.prices_pct["legal_close"])
        assert v.quality == "market_value" and v.spread_kind == kind
        dirty = clean * float(o.quote.face_value) / 100 + float(o.quote.accrued_per_bond)
        assert v.pv_rub == pytest.approx(Q * dirty)
        assert v.dv01_rub < 0 and math.isclose(sum(v.buckets.values()), v.dv01_rub)
        assert sum(1 for x in v.buckets.values() if x) <= 2  # split between two pillars
        assert any("next reset" in a for a in v.assumptions)
        # the spread risk keeps the full duration (an OFZ-PK's spread to RUONIA too, as core's ofz_spr01 since 0.2.6)
        assert v.spread01_rub < v.dv01_rub


def test_fx_bond_in_rub_at_the_market_rate(ctx, exact):
    """R26 (owner, 21.10): a foreign-face bond is in RUB at the MOEX indicative rate of 19:00, not the CBR rate."""
    o = exact[USD]
    v = approximate(ctx, o, Q, _fb(ctx))
    fx = ctx.market.fx("USD")
    rate = fx["moex_19"]
    assert rate != fx["cbr"] and fx_rate(ctx, "USD") == (rate, "MOEX 19:00")
    # price in % of the USD face, MOEX accrued already in RUB (settlement currency)
    clean_rub = float(o.quote.prices_pct["legal_close"]) / 100 * float(o.quote.face_value) * rate
    dirty = clean_rub + float(o.quote.accrued_per_bond)
    assert v.quality == "market_value" and v.pv_rub == pytest.approx(Q * dirty)
    assert any("FX bond (USD)" in a and "MOEX 19:00" in a and "duration proxy on curve USD" in a for a in v.assumptions)
    # R8: rate risk from the MOEX duration, as a RUB bond's fallback, in RUB per bp, on the USD curve
    mac = float(o.quote.source_analytics["duration_days"]) / 365
    y = fallback._yield(o.quote.source_analytics) / 100  # the modified duration (#36): -Macaulay / (1 + y)
    assert v.macaulay_years == pytest.approx(mac) and v.dv01_rub == pytest.approx(-mac / (1 + y) * v.pv_rub * 1e-4)
    assert set(v.risk()) == {"USD"} and sum(v.risk()["USD"].values()) == pytest.approx(v.dv01_rub)
    assert sum(1 for x in v.buckets.values() if x) <= 2 and v.spread01_rub == pytest.approx(v.dv01_rub)  # credit
    row = tdm_run.run_content(ctx, [("P1", "MIXED", replace(o, valuation=v))], None, {})
    assert set(row.rows[0]["risk"]) == {"USD"} and row.rows[0]["fx_delta"] == {"USD": pytest.approx(v.pv_rub / rate)}
    assert (row.rows[0]["extra"]["fx_rate"], row.rows[0]["extra"]["fx_source"]) == (rate, "MOEX 19:00")
    (usd_fx,) = [r for r in row.reference["fx"] if r["ccy"] == "USD"]
    assert (usd_fx["rate"], usd_fx["rate_source"], usd_fx["rate_cbr"]) == (rate, "MOEX 19:00", fx["cbr"])
    usd = next(c for c in row.reference["curves"] if c["curve_id"] == "USD")
    assert (usd["ccy"], usd["points"]) == ("USD", [])  # a stub: the risk is shown, there is no USD curve yet



def test_market_fx_falls_back_14_cets_cbr_and_says_so():
    """R26: MOEX 19:00, else 14:00, else the CETS TOM close, else the CBR rate in force; the source goes with it.
    The CBR rate in force is kept apart (the official basis)."""
    class _Ctx:
        def __init__(self, fx):
            self.market = type("M", (), {"fx": lambda _s, _c: fx})()

    full = {"cbr": 80.0, "cbr_next": 81.0, "moex_tom": 82.5, "moex_19": 81.1, "moex_14": 80.9, "cets_close": 82.0}
    for drop, want in (((), (81.1, "MOEX 19:00")), (("moex_19",), (80.9, "MOEX 14:00")),
                       (("moex_19", "moex_14"), (82.0, "CETS close")),
                       (("moex_19", "moex_14", "cets_close"), (80.0, "CBR"))):
        ctx = _Ctx({k: (None if k in drop else v) for k, v in full.items()})
        assert fx_rate(ctx, "USD") == want and fx_rate_cbr(ctx, "USD") == (80.0, "CBR")
    none = _Ctx(dict.fromkeys(full))
    assert fx_rate(none, "USD") is None and fx_rate_cbr(none, "USD") is None and fx_rate(none, "RUB") == (1.0, "")


def test_ofz_in_nominal_proxy_is_flagged(exact):
    """R8 phase 1: an OFZ-IN keeps a nominal DV01 from its duration on its discount curve (the KBD since 0.2.6, as
    every fixed-coupon OFZ: ``bonds.OFZ_KBD_CLASSES``), said to be a proxy."""
    v = exact["SU52005RMFS4"].valuation
    assert v.quality == "market_value" and set(v.risk()) == {"RUB OFZ"} and v.dv01_rub < 0
    assert any("indexed face: a nominal-rate proxy" in a for a in v.assumptions)


def test_stale_price_within_the_window(ctx, exact):
    o = exact[RZD]
    v = approximate(ctx, _outcome(o, "no_quote", quote=None), Q, _fb(ctx, [_History(ctx, 3)]))
    assert v.quality == "stale_price" and any("days old" in a and "3 days" in a for a in v.assumptions)
    assert v.pv_rub == pytest.approx(o.valuation.pv_rub, rel=1e-9)


def test_model_price_on_issuer_median_z(ctx, exact):
    o = exact[RZD]
    fb = _fb(ctx, [_History(ctx, 40)])  # outside the window: no price
    assert fb.median_z(o.terms, "credit", RUONIA) is None  # one exact corporate in the fixtures: too few for a median
    fb.medians = {("issuer", o.terms.issuer_id, RUONIA): (o.valuation.z_spread_bp, 2)}
    v = approximate(ctx, _outcome(o, "no_quote", quote=None), Q, fb)
    assert v.quality == "model_price" and any("issuer median Z" in a for a in v.assumptions)
    # at its own Z the model gives back the exact price and risk
    assert v.z_spread_bp == pytest.approx(o.valuation.z_spread_bp, abs=1e-6)
    assert v.pv_rub == pytest.approx(o.valuation.pv_rub, rel=1e-7)
    assert v.dv01_rub == pytest.approx(o.valuation.dv01_rub, rel=1e-5)


def test_par_proxy_and_no_data(ctx, exact):
    o = exact[UNKNOWN]
    v = approximate(ctx, _outcome(o, "coupon_unknown", quote=None), Q, _fb(ctx))
    assert v.quality == "par_proxy" and v.dv01_rub == 0.0
    assert v.pv_rub == pytest.approx(Q * (float(o.terms.face_value) + v.accrued_rub))
    assert any("par proxy" in a for a in v.assumptions)
    assert approximate(ctx, Outcome("XX", None, None, reason="not_in_static_snapshot"), Q, _fb(ctx)) is None


def test_simple_pnl_all_in_other(ctx, exact):
    o = exact[FRN]
    cur = approximate(ctx, o, Q, _fb(ctx))
    prev = replace(cur, valuation_date=cur.valuation_date - timedelta(1), dirty_rub=cur.dirty_rub - 2.0)
    p = simple_pnl(prev, cur, o.terms, ctx)
    assert isinstance(p, SimplePnl) and p.other == p.total == pytest.approx(Q * 2.0 + p.received_rub)
    assert p.carry == p.rates == p.spread == p.rolldown == 0.0
    assert p.extra()["method"] == "desk-flash-simple-v1"


def test_every_position_published_with_quality(ctx, exact, tmp_path):
    fb = _fb(ctx)
    outs = [replace(o) for o in exact.values()]
    for o in outs:
        if o.valuation is None:
            o.valuation = approximate(ctx, o, Q, fb)
    db = tmp_path / "tdm.sqlite"
    positions = [(f"P{i}", "MIXED", o) for i, o in enumerate(outs)]
    run_id, status, warnings = tdm_run.publish(str(db), ctx, positions, prev=None)
    assert status == "complete"
    con = sqlite3.connect(db)
    rows = con.execute("SELECT extra_json, ytm, ccy FROM position WHERE run_id=?", (run_id,)).fetchall()
    assert len(rows) == len(outs)
    want = Counter(o.valuation.quality for o in outs)  # floaters become exact or assumed once core prices them
    assert Counter(json.loads(r[0])["quality"] for r in rows) == want and want["market_value"] >= 1
    assert ("USD" in {r[2] for r in rows}) and con.execute(
        "SELECT COUNT(*) FROM position_fx WHERE run_id=? AND ccy='USD'", (run_id,)).fetchone()[0] == 1
    # every curve of the risk is a run curve (tdm requires it): the USD bond's on a USD curve without points
    assert {r[0] for r in con.execute("SELECT DISTINCT curve_id FROM position_risk WHERE run_id=?", (run_id,))} \
        <= {r[0] for r in con.execute("SELECT curve_id FROM run_curve WHERE run_id=?", (run_id,))} \
        == {"RUB OFZ", "RUB RUONIA", "RUB KEY", "USD"}
    assert all(not isinstance(r[1], float) or math.isfinite(r[1]) for r in rows)
    assert sum("оценка market_value" in w for w in warnings) == want["market_value"]
    log = [m for (m,) in con.execute("SELECT message FROM calc_run_log WHERE run_id=?", (run_id,))]
    mv = f"market_value {want['market_value']}"
    assert any(m.startswith("качество оценки: exact ") and mv in m for m in log)

    rows, summary = validation.build(ctx, outs)
    assert {r["status"] for r in rows} == {"priced", "approximated"}
    assert summary["by_class"]["corporate"]["quality"] == dict(Counter(
        o.valuation.quality for o in outs if o.terms.bond_class == "corporate"))
    assert all("check" not in r for r in rows if r["status"] == "approximated")


def test_yield_at_minus_100_and_zero(ctx, exact):
    """A MOEX yield of -100 % (a bad print) is no yield; 0 % is a yield."""
    o = exact[FRN]
    sa = {**o.quote.source_analytics, "effective_yield_pct": None, "yield_close_pct": "-100"}
    v = approximate(ctx, _outcome(o, "x", quote=replace(o.quote, source_analytics=sa)), Q, _fb(ctx))
    assert v.quality == "market_value" and math.isnan(v.ytm_pct) and math.isfinite(v.modified_duration)
    sa = {**o.quote.source_analytics, "effective_yield_pct": "0"}
    v = approximate(ctx, _outcome(o, "x", quote=replace(o.quote, source_analytics=sa)), Q, _fb(ctx))
    assert v.ytm_pct == 0.0 and v.modified_duration == pytest.approx(v.macaulay_years)


def test_a_failing_ladder_falls_to_par_proxy(ctx, monkeypatch):
    def boom(*a, **k):
        raise ZeroDivisionError("float division by zero")
    monkeypatch.setattr(orch, "approximate", boom)
    (o,) = value(ctx, [(USD, Q)])  # not exact: the ladder runs and fails
    v = o.valuation
    assert v.quality == "par_proxy" and v.dv01_rub == 0.0
    assert v.pv_rub == pytest.approx(Q * (float(o.quote.face_value) * ctx.market.fx("USD")["moex_19"]
                                          + float(o.quote.accrued_per_bond)))
    assert "fallback ladder failed: ZeroDivisionError: float division by zero" in v.assumptions[0]


def _matured(t, d):
    return replace(t, maturity_date=d, redemptions=(Redemption(d, "maturity", t.face_value),), offers=())


def test_matured_bond_is_worth_zero_and_pnl_is_the_last_move(ctx, exact):
    o, bd = exact[RZD], ctx.business_date
    t = _matured(o.terms, bd)
    cur = approximate(ctx, Outcome(RZD, t, o.quote, reason="matured"), Q, _fb(ctx))
    assert cur.quality == "market_value" and cur.pv_rub == cur.dirty_rub == cur.dv01_rub == 0.0
    assert any(f"matured {bd}: redeemed" in a for a in cur.assumptions)
    prev = replace(approximate(ctx, o, Q, _fb(ctx)), valuation_date=bd - timedelta(1))
    received = Q * (1000 + float(next(c.amount_per_bond for c in t.coupons if c.pay_date == bd)))
    p = simple_pnl(prev, cur, t, ctx)
    assert p.received_rub == pytest.approx(received)
    assert p.total == pytest.approx(received - Q * prev.dirty_rub)  # not q x face more
    # no price either: still 0, never par
    v = approximate(ctx, Outcome(RZD, t, None, reason="no_quote"), Q, _fb(ctx))
    assert v.quality == "par_proxy" and v.pv_rub == 0.0


def test_stale_price_on_an_amortizer_uses_todays_face(ctx, exact):
    """500 of 1000 amortized today (the static face of today is before it): the old quote's % applies to the 500
    outstanding."""
    o, bd = exact[RZD], ctx.business_date
    t = replace(o.terms, redemptions=(
        Redemption(bd, "amortization", Decimal(500)), Redemption(o.terms.maturity_date, "maturity", Decimal(500))))
    assert face_on(t, bd) == 500 and face_on(t, bd - timedelta(1)) == 1000
    v = approximate(ctx, Outcome(RZD, t, None, reason="no_quote"), Q, _fb(ctx, [_History(ctx, 3)]))
    clean = float(o.quote.prices_pct["legal_close"])
    assert v.quality == "stale_price" and v.face_rub == 500.0
    assert v.dirty_rub == pytest.approx(clean / 100 * 500 + v.accrued_rub)
    assert any("face 500 outstanding" in a for a in v.assumptions)


def test_pit_static_face_and_redemption_check(ctx, exact):
    """A static snapshot two days after the market, with an amortization in between: the face of the trade date."""
    t, bd = exact[RZD].terms, ctx.business_date
    later = replace(t, face_value=Decimal(500), face_date=bd + timedelta(2), redemptions=(
        Redemption(bd + timedelta(1), "amortization", Decimal(500)), Redemption(t.maturity_date, "maturity",
                                                                                Decimal(500))))
    s = fixed_schedule(later, bd)
    assert s.current_face == 1000.0
    on_static_date = replace(later, face_value=Decimal(1000), face_date=bd + timedelta(1))
    assert face_on(on_static_date, bd) == 1000 and face_on(on_static_date, bd + timedelta(1)) == 500
    with pytest.raises(Rejected, match="redemption_incomplete"):  # still checked: the final 500 missing
        fixed_schedule(replace(later, redemptions=later.redemptions[:1]), bd)
    # MOEX keeps the face before the final redemption on a matured bond
    gone = replace(_matured(t, bd + timedelta(1)), face_date=bd + timedelta(2))
    assert face_on(gone, bd) == 1000 and face_on(gone, bd + timedelta(1)) == 0
    assert ctx.point_in_time_warning is None
    pit = replace(ctx, static=type(ctx.static).__new__(type(ctx.static)))
    pit.static.__dict__.update(ctx.static.__dict__, snapshot=replace(ctx.static.snapshot, business_date="2026-10-17"))
    assert "later than the market 2026-10-15" in pit.point_in_time_warning
    assert pit.lineage()["point_in_time_warning"] == pit.point_in_time_warning


def test_no_pnl_on_par_proxy_flag_on_model_price(ctx, exact):
    o, d0 = exact[RZD], ctx.business_date - timedelta(1)
    prev = shifted_prev(ctx, d0)
    mv = approximate(ctx, _outcome(o, "x"), Q, _fb(ctx))
    mv0 = replace(mv, valuation_date=d0)

    def run(prev_v, cur_v):
        p, c = _outcome(o, "x"), _outcome(o, "x")
        p.valuation, c.valuation = prev_v, cur_v
        attach_pnl(prev, [p], ctx, [c])
        return c
    for a, b in ((replace(mv0, quality="par_proxy"), mv), (mv0, replace(mv, quality="par_proxy"))):
        c = run(a, b)
        assert c.pnl is None and c.no_pnl.reason == "par_proxy"
    c = run(replace(mv0, quality="model_price", dirty_rub=mv.dirty_rub - 1.0), mv)
    assert c.pnl.total == pytest.approx(Q * 1.0 + c.pnl.received_rub)
    assert c.pnl.extra()["flag"] == "price discovery: model -> market"
    assert "flag" not in run(mv0, mv).pnl.extra()
