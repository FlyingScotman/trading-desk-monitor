"""TDM#499: RUONIA OIS past its last quote (core 0.42 ``ValuationPolicy.ois_tail``, ``valuation.bonds.OIS_TAIL``).

The fixture's RUONIA OIS quotes end at 10Y. Without the tail the curve runs on at the 9Y-10Y forward, so a bucket bump
of 10Y moves the whole tail and a RUONIA position past 10Y swings between the 7Y and 10Y buckets with opposite signs
(the sum right). With it core adds synthetic 15Y / 20Y par quotes at the KBD's par spread at 10Y and the risk lands on
15Y. The bond is synthetic: the fixture's fixed OFZ maturing 2041 (about 14.6 years) reshaped as a corporate, so it is
discounted on RUONIA OIS at its own quote."""

from __future__ import annotations

import dataclasses
import math
from datetime import timedelta
from pathlib import Path

import pytest
from test_slice_s2 import prev_outcome, shifted_prev
from test_swaps_repo_tdm45 import FIX as FIX_1_4
from test_swaps_repo_tdm45 import inputs as swap_inputs
from test_swaps_repo_tdm45 import leg as swap_leg

from desk.curves.rates import RUONIA_OIS_CURVE_ID
from desk.marketdata.adapter import KBD_CURVE_ID
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import Outcome, attach_pnl, prepare
from desk.valuation import derivatives as dv
from desk.valuation import positions as pv1
from desk.valuation.bonds import OIS_TAIL, BondValuation, bond_request, value_bonds

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
LONG = "SU26238RMFS4"  # fixed coupon to 2041-05-15
QTY = 1000.0


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), "2026-10-15")


def long_corporate(ctx):
    return dataclasses.replace(ctx.static.bonds[LONG], bond_class="corporate")


def valued(ctx, market, *, ois_tail: bool = True):
    t = long_corporate(ctx)
    quote = ctx.market.quote(ctx.static.bonds[LONG])
    out, results = value_bonds(market, [bond_request("x", t, quote, ctx.business_date, QTY)],
                               ofz_basis=ctx.ofz_basis, ois_tail=ois_tail)
    v = out["x"]
    assert isinstance(v, BondValuation), v
    return v, results[0], Outcome(t.secid, t, quote, valuation=v, core_result=results[0])


def test_long_ruonia_bond_risk_lands_past_10y_with_dv01_kept(ctx):
    tail, res, _ = valued(ctx, ctx.risk_market)
    flat, res0, _ = valued(ctx, ctx.risk_market, ois_tail=False)
    assert tail.discount_curve_id == flat.discount_curve_id == RUONIA_OIS_CURVE_ID
    a, b = tail.risk()["RUB RUONIA"], flat.risk()["RUB RUONIA"]
    # without the tail: 7Y +380 / 10Y -711 / 15Y -3 RUB/bp (1 000 bonds), the swing of the last segment's forward
    assert b["7Y"] > 0 > b["10Y"] and min(abs(b["7Y"]), abs(b["10Y"])) > 50 * abs(b["15Y"])
    # with it: +24 / +41 / -398, no opposite-sign swing: 7Y and 10Y a tenth of 15Y, nothing past 20Y+ to speak of
    assert a["15Y"] < 0 and abs(a["7Y"]) < 0.1 * abs(a["15Y"]) and abs(a["10Y"]) < 0.15 * abs(a["15Y"])
    assert abs(a["20Y+"]) < 0.01 * abs(a["15Y"])
    # the total DV01 is the same rate risk in other buckets: -317.07 against -317.41 (0.1 %)
    assert tail.dv01_rub == pytest.approx(flat.dv01_rub, rel=2e-3)
    assert math.fsum(a.values()) == pytest.approx(tail.dv01_rub)
    # buckets up to 5Y are the 10Y quote's neighbours only through the bootstrap: within a few per cent
    for k in ("1Y", "2Y", "3Y", "5Y"):
        assert a[k] == pytest.approx(b[k], rel=0.05, abs=0.1), k
    # core records the rule taken on the OIS curve (run history); without the field the model is the plain one
    used = {c.curve_id: c.model_version for c in res.curves}
    assert used[RUONIA_OIS_CURVE_ID].endswith("+ois-tail-kbd-par-spread-v1")
    assert "+ois-tail" not in {c.curve_id: c.model_version for c in res0.curves}[RUONIA_OIS_CURVE_ID]
    assert ctx.rates["ruonia_ois"]["tail"]["rule"] == "kbd"


def test_built_curve_is_the_curve_core_continues(ctx):
    """The run's built RUONIA curve (``core_market``: the flash PnL, the fallback ladder, the chart) is the one core
    builds from the quotes with the tail: the same Z-spread to the bit on either, the same PV up to the 1e-9 the 10Y
    swap's payment lag allows."""
    tail, _, _ = valued(ctx, ctx.risk_market)
    built, _, _ = valued(ctx, ctx.core_market)
    flat, _, _ = valued(ctx, ctx.risk_market, ois_tail=False)
    assert built.z_spread_bp == pytest.approx(tail.z_spread_bp, abs=1e-9)
    assert abs(flat.z_spread_bp - tail.z_spread_bp) > 1.0  # the tail moves the spread of a bond past 10Y


def test_flash_pnl_of_a_long_ruonia_bond_adds_up(ctx):
    """D-1 and D both continued (``shifted_prev`` builds its curve as ``rates.ruonia_ois``): the exact steps reprice
    the bond at the Z-spread core calibrated with the tail, so nothing is left in ``other``."""
    _, _, o = valued(ctx, ctx.risk_market)
    prev = shifted_prev(ctx, ctx.business_date - timedelta(days=1))
    p = prev_outcome(prev, o)
    attach_pnl(prev, [p], ctx, [o])
    f = o.pnl
    assert f is not None, o.no_pnl
    assert f.total == pytest.approx(f.carry + f.rolldown + f.rates + f.spread + f.other)
    assert abs(f.other) < 1e-6 * o.valuation.pv_rub
    assert f.rates == pytest.approx(f.rates_linear, rel=0.05)


def test_long_ois_swap_risk_lands_past_10y(monkeypatch):
    """A 15Y payer OIS on RUONIA (the 1.4 fixture, synthetic terms): the derivatives' core call holds the KBD and names
    ``OIS_TAIL``, as the bonds', so its RUONIA risk is on 15Y, not swung between 7Y and 10Y; its DV01 is kept."""
    ctx4 = prepare(SnapshotDir(FIX_1_4), "2026-10-15")
    terms = [swap_leg("S1", "fix", "pay", fixed_rate_pct="15", maturity_date="2041-10-07"),
             swap_leg("S1", "flt", "receive", index_code="RUONIA", maturity_date="2041-10-07")]
    real, seen = dv.value_portfolio, {}

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(dv, "value_portfolio", spy)
    tail = pv1.plan(ctx4, swap_inputs(terms, ("S1",))).direct["S1"].valuation
    assert seen["policy"].ois_tail == OIS_TAIL and KBD_CURVE_ID in {c.curve_id for c in seen["snapshot"].curves}
    monkeypatch.setattr(dv, "value_portfolio", lambda **kw: real(**{**kw, "policy": dataclasses.replace(
        kw["policy"], ois_tail=None)}))
    flat = pv1.plan(ctx4, swap_inputs(terms, ("S1",))).direct["S1"].valuation
    a, b = tail.curve_buckets["RUB RUONIA"], flat.curve_buckets["RUB RUONIA"]
    assert abs(a["7Y"]) < 0.01 * a["15Y"] and abs(a["10Y"]) < 0.01 * a["15Y"]
    assert abs(b["7Y"]) + abs(b["10Y"]) > 0.5 * abs(b["15Y"])  # the swing without the tail
    # its DV01 within 0.5 %: unlike a bond's (its spread calibrated to its price) a swap's PV moves with the tail curve
    assert tail.dv01_rub == pytest.approx(flat.dv01_rub, rel=5e-3)
