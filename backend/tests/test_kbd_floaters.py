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


def test_kbd_floater_publishes_its_ofz_basis(ctx, out):
    row = tdm_run.run_content(ctx, [("P1", "B", out)], None, {}).rows[0]
    ex = row["extra"]
    # the projection's KBD risk is its OFZ basis risk (variant P: no RUONIA risk of the projection apart in core 0.39)
    assert ex["ofz_basis01"] == pytest.approx(math.fsum(row["risk"]["RUB OFZ"].values()))
    assert "ofz_ruonia01" not in ex and ex["dm_bp"] == out.valuation.discount_margin_bp
    s = summary.build([{"position_id": "P1", "book_id": "B", "pv_rub": row["mv_rub"], "risk": row["risk"],
                        "cs01": row["cs01"], "ofz": row["ofz_spr01"], "instr_type": "BOND", "segment": row["segment"],
                        "asset_class": row["asset_class"], "coupon_type": row["coupon_type"], "ccy": "RUB",
                        "notional": row["notional"], "notional_rub": row["notional_rub"], "price": row["price"],
                        "spread_bp": row["spread_bp"], "dv01": row["dv01"], "dm_bp": ex["dm_bp"], "pnl": None,
                        "ofz_basis": ex["ofz_basis01"], "quality": "exact", "ofz_ruo": None}])
    # «RUONIA + базис»: RUONIA is the discount plus the projection, the basis the projection
    assert s["basis"] == pytest.approx(ex["ofz_basis01"]) and s["cs01"] == pytest.approx(row["cs01"]) and row["cs01"] < 0
    assert s["dv01"]["RUB RUONIA"] == pytest.approx(math.fsum(x for c in row["risk"].values() for x in c.values()))


def test_kbd_floater_flash_pnl_reprojects_on_the_kbd(ctx, out):
    """The flash PnL reprojects its coupons on each step's KBD (``pnl.flash``) and core explains it the same way."""
    from test_pnl_compare import _rows

    (r,) = _rows(ctx, [out])
    assert r.flash and r.core, r.reason
    assert r.core["total"] == pytest.approx(r.identity_total, abs=0.05)
    assert r.delta["carry"] == pytest.approx(0, abs=0.5)


@pytest.mark.parametrize("curve", ["RUB OFZ", "RUB RUONIA"])
def test_kbd_floater_exact_scenario_moves_each_curve_as_its_risk(ctx, out, curve):
    """A scenario on ``RUB OFZ`` reaches the KBD floater's projection (the unnamed shock, ``scenario.exact``), one on
    ``RUB RUONIA`` its discount only: the repricing matches its risk on that curve to the first order."""
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
