"""TDM#406: the flash PnL keeps the breakdowns it computes in ``pnl.extra`` (``pnl.flash`` module docstring) for the
PnL period screen (docs/architecture/briefs/PNL_PERIOD.md), and changes none of its components.

On the golden fixtures with the synthetic previous dates of test_slice_s2 (and the CNY / USD pair of
test_batch1_fx_pnl): a fixed-coupon bond, both floaters, an OFZ in RUONIA + basis, a coupon and an amortization paid
inside the interval, a CNY bond and a USD bond explained in their currency. Direct and simple methods get no new key.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from store_rows import insert_run
from test_api import book  # noqa: F401 - the module fixture of the golden runs
from test_batch1_fx_pnl import _pnl, two_dates  # noqa: F401 - the CNY / USD pair
from test_fx_core_035 import CNY_BOND, USD_BOND
from test_slice_s2 import QTY, flash_curves, prev_outcome, shifted_prev

from desk import store
from desk.api import create_app
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import Outcome, prepare, value
from desk.pnl import flash as flash_module
from desk.pnl.direct import DirectPnl
from desk.pnl.flash import BREAKDOWN_KEYS, FlashPnl, explain
from desk.publish.tdm_run import risk_by_curve

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
DATE = "2026-10-15"


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), DATE)


@pytest.fixture(scope="module")
def bonds(ctx) -> dict[str, Outcome]:
    return {o.secid: o for o in value(ctx, [(s, QTY) for s in sorted(ctx.static.bonds)], fallback=False)
            if o.valuation is not None}


def flash(ctx, o: Outcome, d0: date, shift_bp: float = 10.0) -> tuple[FlashPnl, Outcome]:
    """``o`` explained from ``d0`` (the fixture's market moved back to it, ``shift_bp`` lower); the quote's face
    dropped so that a bond amortized since ``d0`` is valued on its face then."""
    prev = shifted_prev(ctx, d0, shift_bp)
    p = prev_outcome(prev, dataclasses.replace(o, quote=dataclasses.replace(o.quote, face_value=None)),
                     price_move_pct=0.05)
    return explain(p.valuation, o.valuation, flash_curves(prev, ctx)), p


def check(f: FlashPnl, p) -> dict:
    """What every flash row keeps (module docstring) and that it changed nothing: the components and the old keys
    are those of the same explain without the D-1 instrument and its coupon flows, which then has no carry split."""
    pv = p.valuation
    ex = f.extra()
    json.dumps(ex, allow_nan=False)  # JSON-safe
    assert ex["carry_coupon"] + ex["carry_pull"] == pytest.approx(f.carry, abs=1e-6 * max(1.0, abs(f.carry)))
    assert ex["received_coupon_rub"] + ex["received_principal_rub"] == pytest.approx(f.received_rub, abs=1e-6)
    lc, mv = ex["rates_linear_curves"], ex["curve_moves_bp"]
    assert math.fsum(x for bs in lc.values() for x in bs.values()) == pytest.approx(
        f.rates_linear, rel=1e-12, abs=1e-9)
    risk = pv.risk()
    for c, bs in lc.items():
        for b, x in bs.items():
            assert x == risk[c][b] * mv[c][b]
    prev = ex["prev"]
    assert prev["clean_pct"] == pv.clean_pct and prev["accrued_per_bond_rub"] == pv.accrued_rub
    assert prev["dirty_per_bond_rub"] == pv.dirty_rub and prev["pv_rub"] == pv.pv_rub
    assert prev["ytm_pct"] == pv.ytm_pct and prev["dv01_rub"] == pv.dv01_rub and prev["quantity"] == pv.quantity
    assert prev["cs01_rub"] == (pv.spread01_rub if pv.spread_kind == "credit" else 0.0)
    assert prev["risk"] == risk_by_curve(pv) and ex["z0_bp"] == round(pv.z_spread_bp, 4)
    assert f.v0 == pytest.approx(pv.pv_rub / (pv.fx_rate[1] if pv.fx_rate else 1.0), rel=1e-9)
    assert abs(f.identity_error) <= 0.01 and f.explained == pytest.approx(f.total, abs=0.01)
    return ex


def unchanged(f: FlashPnl, p, cur: Outcome, curves) -> None:
    bare = dataclasses.replace(p.valuation, instrument=None, coupons=())
    g = explain(bare, cur.valuation, curves)
    assert g.as_tdm() == f.as_tdm() and g.carry_coupon is None and g.received_coupon_rub is None
    old = {k: v for k, v in f.extra().items() if k not in BREAKDOWN_KEYS}
    assert {k: v for k, v in g.extra().items() if k not in BREAKDOWN_KEYS} == old
    assert not {"carry_coupon", "carry_pull", "received_coupon_rub", "received_principal_rub"} & set(g.extra())


def day_coupon(v, q: float, days: int) -> float:
    """A fixed coupon's accrual over ``days``: rate x face x days / 365."""
    return q * v.current_coupon_pct / 100 * v.face_rub * days / 365


@pytest.mark.parametrize("secid,curves", [("RU000A1034P7", {"RUB RUONIA"}), ("SU29008RMFS8", {"RUB RUONIA"}),
                                          ("RU000A109XR1", {"RUB RUONIA", "RUB KEY"})])
def test_fixed_bond_and_floaters_over_a_day(ctx, bonds, secid, curves):
    o = bonds[secid]
    d0 = ctx.business_date - timedelta(days=1)
    f, p = flash(ctx, o, d0)
    ex = check(f, p)
    unchanged(f, p, o, flash_curves(shifted_prev(ctx, d0), ctx))
    assert set(ex["rates_linear_curves"]) == curves and set(ex["curve_moves_bp"]) == curves
    assert ex["received_coupon_rub"] == ex["received_principal_rub"] == 0.0
    assert all(abs(m - 10.0) < 1.0 for c in curves for m in ex["curve_moves_bp"][c].values())  # 10 bp up
    if o.valuation.floater is None:  # a day of the fixed coupon, to the rounding of the accrued per bond
        want = day_coupon(p.valuation, QTY, 1)
        assert ex["carry_coupon"] == pytest.approx(want, rel=0.02, abs=0.011 * QTY)
    else:
        assert ex["carry_coupon"] > 0


def test_ofz_in_ruonia_plus_basis(ctx, bonds):
    o = bonds["SU26238RMFS4"]
    assert o.valuation.ofz_ruonia
    d0 = ctx.business_date - timedelta(days=1)
    f, p = flash(ctx, o, d0)
    ex = check(f, p)
    unchanged(f, p, o, flash_curves(shifted_prev(ctx, d0), ctx))
    # only the KBD's published curve (its basis risk x the KBD's par move); the factor split sums to the same total
    assert set(ex["rates_linear_curves"]) == {"RUB OFZ"} and set(ex["curve_moves_bp"]) == {"RUB OFZ", "RUB RUONIA"}
    assert ex["rates_linear_ruonia"] + ex["rates_linear_basis"] == pytest.approx(f.rates_linear, abs=1e-9)
    assert ex["rates_linear_ruonia"] == pytest.approx(math.fsum(
        x * ex["curve_moves_bp"]["RUB RUONIA"][b] for b, x in p.valuation.ofz_ruonia.items()
        if b in ex["curve_moves_bp"]["RUB RUONIA"]), rel=1e-12)
    assert ex["prev"]["ofz_ruonia_risk"] == p.valuation.ofz_ruonia and ex["prev"]["ofz_spr01_rub"] != 0.0
    assert ex["carry_coupon"] == pytest.approx(day_coupon(p.valuation, QTY, 1), rel=0.02, abs=0.011 * QTY)


@pytest.mark.parametrize("secid,d0", [("SU26238RMFS4", date(2026, 6, 2)), ("RU000A0JXQ44", date(2026, 10, 14))])
def test_a_coupon_paid_inside_the_interval(ctx, bonds, secid, d0):
    o = bonds[secid]
    paid = [c for c in o.terms.coupons if d0 < c.pay_date <= ctx.business_date]
    assert paid
    f, p = flash(ctx, o, d0, shift_bp=5.0)
    ex = check(f, p)
    unchanged(f, p, o, flash_curves(shifted_prev(ctx, d0, 5.0), ctx))
    assert ex["received_coupon_rub"] == pytest.approx(QTY * sum(float(c.amount_per_bond) for c in paid))
    assert ex["received_principal_rub"] == pytest.approx(0.0, abs=1e-6)
    # the coupon paid is income only for the days held: the accrued bought on d0 comes off it
    days = (o.valuation.valuation_date - p.valuation.valuation_date).days
    assert ex["carry_coupon"] == pytest.approx(day_coupon(p.valuation, QTY, days), rel=0.02, abs=0.011 * QTY)
    assert ex["carry_coupon"] < ex["received_coupon_rub"]


def test_an_amortization_paid_inside_the_interval(ctx, bonds):
    o = bonds["RU000A109XR1"]  # a key-rate floater, amortized on 2026-07-21
    d0 = date(2026, 7, 20)
    red = [r for r in o.terms.redemptions if d0 < r.date <= ctx.business_date]
    assert red
    f, p = flash(ctx, o, d0, shift_bp=5.0)
    ex = check(f, p)
    unchanged(f, p, o, flash_curves(shifted_prev(ctx, d0, 5.0), ctx))
    assert ex["received_principal_rub"] == pytest.approx(QTY * sum(float(r.value_per_bond) for r in red))
    assert ex["received_coupon_rub"] > 0 and ex["carry_coupon"] > 0


@pytest.mark.parametrize("secid,proxy", [(CNY_BOND, False), (USD_BOND, True)])
def test_a_foreign_bond_in_its_currency(two_dates, secid, proxy):  # noqa: F811
    prev, _cur, out = _pnl(two_dates, [(secid, 1000.0)], proxy=proxy)
    o = out[secid]
    f = o.pnl
    assert isinstance(f, FlashPnl) and f.fx_rates is not None, o.no_pnl
    (p,) = value(prev, [(secid, 1000.0)])
    ex = check(f, p)
    # the coupon at the D rate, as carry: a day of the coupon in the bond's currency x the rate
    f0, f1 = f.fx_rates
    want = day_coupon(p.valuation, 1000.0, (f.valuation_date - f.prev_valuation_date).days) / f0 * f1
    assert ex["carry_coupon"] == pytest.approx(want, rel=0.02, abs=0.011 * 1000.0 * f1)


def test_direct_and_simple_methods_have_no_new_keys(two_dates):  # noqa: F811
    _, _, out = _pnl(two_dates, [(USD_BOND, 1000.0)])  # no curve for USD: its yield carry (``SimplePnl``)
    assert out[USD_BOND].pnl is not None and type(out[USD_BOND].pnl) is not FlashPnl
    d = DirectPnl("desk-fx-v1", date(2026, 10, 14), date(2026, 10, 15), 1.0, fx_gross=1.0)
    for pnl in (out[USD_BOND].pnl, d):
        assert not set(BREAKDOWN_KEYS) & set(pnl.extra())


def test_the_real_flash_run_exposes_them_through_the_period_api(store_url, book):  # noqa: F811
    ctx, positions, view = book
    rid = insert_run(store_url, ctx, positions, view)
    with store.connect(store_url) as con:
        rows = [dict(zip(("position_id", "book_id", "fact", "pnl"), r)) for r in con.execute(
            "SELECT position_id, book_id, fact, pnl FROM run_position WHERE run_id = %s AND pnl IS NOT NULL"
            " ORDER BY ord", (rid,))]
    flashes = [r for r in rows if r["pnl"]["extra"]["method"].startswith("desk-flash-exact")]
    assert flashes
    for r in flashes:  # the store keeps them; the fact's copy of the PnL does not
        assert set(BREAKDOWN_KEYS) <= set(r["pnl"]["extra"])
        assert not set(BREAKDOWN_KEYS) & set(r["fact"]["extra"]["pnl"])
    c = TestClient(create_app(store_url))
    q = {"from": DATE, "to": DATE, "run_id": rid}
    p = c.get("/api/v1/history/pnl/period", params={**q, "gran": "day"}).json()
    assert {"carry_coupon", "carry_pull"} <= set(p["keys"])
    s = p["summary"]["c"]
    want = math.fsum(r["pnl"]["extra"]["carry_coupon"] for r in flashes)
    assert s["carry_coupon"] == pytest.approx(want) and s["carry_coupon"] + s["carry_pull"] == pytest.approx(
        math.fsum(r["pnl"]["carry"] for r in flashes))
    cv = c.get("/api/v1/history/pnl/curves", params=q).json()
    assert cv["coverage"]["bucketRows"] == len(flashes)
    lin = math.fsum(x for bs in cv["linear"].values() for x in bs.values() if x is not None)
    assert lin == pytest.approx(math.fsum(r["pnl"]["extra"]["rates_linear"] for r in flashes), rel=1e-9)
    r = flashes[0]
    d = c.get("/api/v1/history/pnl/position", params={**q, "position_book": r["book_id"],
                                                       "position_id": r["position_id"]}).json()["days"][0]
    prev = r["pnl"]["extra"]["prev"]
    assert d["inputs"]["source"] == "prev" and d["inputs"]["cleanPrev"] == prev["clean_pct"]
    assert d["inputs"]["dv01Prev"] == prev["dv01_rub"] and d["inputs"]["quantityPrev"] == prev["quantity"]


class _AccruedFails:
    """A D-1 instrument whose accrued raises an unexpected error (not a core rejection)."""

    def accrued(self, *_args):
        raise TypeError("synthetic: accrued unavailable")


def test_a_failing_breakdown_never_fails_the_pnl(ctx, bonds, caplog, monkeypatch):
    """The breakdowns are nice to have: any error leaves their keys out, the PnL and its numbers as without them."""
    o = bonds["RU000A1034P7"]
    d0 = ctx.business_date - timedelta(days=1)
    f, p = flash(ctx, o, d0)
    curves = flash_curves(shifted_prev(ctx, d0), ctx)
    with caplog.at_level(logging.DEBUG, logger="desk.pnl.flash"):
        g = explain(dataclasses.replace(p.valuation, instrument=_AccruedFails()), o.valuation, curves)
    assert "TypeError" in caplog.text
    assert g.as_tdm() == f.as_tdm() and g.total == f.total and g.carry == f.carry and g.carry_coupon is None
    old = {k: v for k, v in f.extra().items() if k not in BREAKDOWN_KEYS}
    ex = g.extra()
    assert not {"carry_coupon", "carry_pull"} & set(ex)
    assert {k: v for k, v in ex.items() if k not in BREAKDOWN_KEYS} == old
    # the other helpers: an error is a missing key, never an exception
    assert flash_module._prev_inputs(object()) is None
    assert flash_module._linear_terms([("RUB RUONIA", {"1Y": "x"}, {"1Y": 1.0})], {}, None) == (None, None)

    def boom(_v):
        raise TypeError("synthetic")

    monkeypatch.setattr(flash_module, "_json_safe", boom)
    ex = f.extra()
    assert not set(BREAKDOWN_KEYS) & set(ex) and {k: v for k, v in ex.items() if k not in BREAKDOWN_KEYS} == old
