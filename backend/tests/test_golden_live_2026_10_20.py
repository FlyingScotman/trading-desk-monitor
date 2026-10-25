"""R26 golden on live hub data: the eod_final of 19.10 and 20.10.2026 from marketdatahub.cc, trimmed to 15 bonds
(``fixtures/hub_live_2026-10-20``, ``golden_index.json``: the hub's snapshot IDs and the bonds; content-addressed
bundles, rebuilt by trimming the hub's bundles, never edited by hand).

Checked against MOEX's own figures in the snapshot (core 0.33, the backend on it):

- accrued interest (core ``Instrument.accrued``, the floaters' too) = MOEX ``ACCINT`` to the kopeck. Before core
  0.33 the backend's ``_floater_accrued`` missed it by a kopeck on four of these floaters (RU000A0JXE06 36.95,
  RU000A10AAU6 13.54, RU000A10C3P3 4.69, SU29008RMFS8 5.53);
- the effective yield at the weighted-average price, settled T+1 with the accrued of that date (the validation
  report, ``bond_metrics_batch``) = MOEX ``EFFECTIVEYIELDWAPRICE`` within 0.01 bp for the fixed bonds; the Macaulay
  duration within 1.5 days of ``DURATION`` (MOEX's is at its published, rounded yield);
- core's G-spread (to the KBD, on RUONIA discounting) is MOEX's definition: yield minus the KBD at the duration; a
  floater's discount margin is its Z-spread on one curve (RUONIA) and not on two (the key rate projected);
- CS01 / an OFZ's spread to the KBD by tenor bucket sums to the spread risk; an OFZ's par buckets on the KBD sum to
  its DV01 at its own yield within 1 % (0.2.6, #32);
- the FX bonds' PnL 19.10 -> 20.10 at the MOEX 19:00 rate (owner, 21.10): FX +3,618 RUB on 1,000 bonds of
  RU000A10D806, not -74,036 at the CBR rate in force (the CBR move from 83.6177 to 82.8504 vs the market's from
  82.8504 to 82.8879); the CBR figure stays beside it as ``fx_gross_cbr``. Their carry at their yield (batch 1,
  item 5: no USD / CNY curve in these snapshots, the fallback rung) is out of «Прочее»: +2,225 RUB.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from rub_fixed_income import RISK_BUCKETS, kbd_effective_yield

from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import attach_pnl, prepare, value
from desk.report import validation
from desk.valuation import bonds

FIX = Path(__file__).parent / "fixtures" / "hub_live_2026-10-20"
INDEX = json.loads((FIX / "golden_index.json").read_text())
FIXED = ("SU26238RMFS4", "SU26244RMFS2", "RU000A10AS02", "RU000A107W48", "RU000A10EQV2")
KEY = ("RU000A0JXE06", "RU000A10AAU6", "RU000A10C3P3")
ONE_CURVE = ("RU000A1087J8", "SU29008RMFS8", "SU29024RMFS5", "SU29027RMFS8")  # RUONIA projects and discounts
FX = {"RU000A10D806": "USD", "RU000A10CZ68": "USD", "RU000A10BLG0": "CNY"}


@pytest.fixture(scope="module")
def run():
    snaps = SnapshotDir(FIX)
    prev, ctx = prepare(snaps, "2026-10-19"), prepare(snaps, "2026-10-20")
    req = [(s, 1000.0) for s in INDEX["bonds"]]
    cur, old = value(ctx, req), value(prev, req)
    attach_pnl(prev, old, ctx, cur)
    return ctx, {o.secid: o for o in cur}


def test_accrued_is_moex_s_to_the_kopeck(run):
    ctx, outs = run
    for s, o in outs.items():
        if s in FX:
            continue
        r = bonds.bond_request("G", o.terms, o.quote, o.quote.trade_date, 1.0)
        assert bonds._prepare(r, ctx.core_market).accrued == pytest.approx(float(o.quote.accrued_per_bond),
                                                                           abs=0.005), s


def test_yield_and_duration_at_moex_convention(run):
    ctx, outs = run
    rows = {r["secid"]: r for r in validation.compare_all(ctx, list(outs.values()))}
    for s in FIXED:
        r = rows[s]
        assert outs[s].valuation.quality == "exact" and r["check"] in ("ok", "short"), r  # short: under 30 days
        assert abs(r["d_y_wap_bp"]) < 0.01, (s, r["d_y_wap_bp"])
        assert abs(r["d_dur_days"]) < 1.5, (s, r["d_dur_days"])


def test_g_spread_dm_and_spread_buckets(run):
    ctx, outs = run
    for s in FIXED:
        v = outs[s].valuation
        assert v.g_spread_bp == pytest.approx((v.ytm_pct / 100 - kbd_effective_yield(ctx.kbd, v.macaulay_years)) * 1e4,
                                              abs=1e-6)
        assert math.fsum(v.spread_buckets.values()) == pytest.approx(v.spread01_rub)
    assert outs["SU26238RMFS4"].valuation.g_spread_bp == pytest.approx(-22.28, abs=0.01)
    # 0.2.6 (#32): an OFZ on the KBD, par buckets (rub_ofz_kbd_v3); the spread to the KBD of 1,000 bonds of a 2041
    # bullet split as its DV01 (until 0.2.5: the OFZ–RUONIA basis, 10Y -62.6, 15Y -148.5, 20Y+ 0.0)
    v = outs["SU26238RMFS4"].valuation
    sb = v.spread_buckets
    assert v.discount_curve_id == "RUB_KBD" and set(v.risk()) == {"RUB OFZ", "RUB RUONIA"}
    # 0.2.7 (#213, core 0.39): RUONIA and the basis two computed risks (RUB/bp of 1,000 bonds); the row in the scenarios'
    # coordinates: RUB OFZ the basis (the KBD's par risk), RUB RUONIA RUONIA minus it, their sum the RUONIA risk
    ruo, bas = v.ofz_ruonia, v.risk()["RUB OFZ"]
    assert (round(math.fsum(ruo.values()), 1), round(math.fsum(bas.values()), 1)) == (-317.9, -316.6)
    assert (round(ruo["15Y"], 1), round(bas["15Y"], 1), round(ruo["10Y"], 1), round(bas["10Y"], 1)) == (
        -400.5, -411.4, 41.0, 46.2)
    assert v.dv01_rub == pytest.approx(math.fsum(ruo.values()))
    for b in RISK_BUCKETS:
        assert v.risk()["RUB RUONIA"][b] + bas[b] == pytest.approx(ruo[b], abs=1e-9)
    assert (round(sb["10Y"], 1), round(sb["15Y"], 1), round(sb["20Y+"], 1)) == (45.7, -406.6, 1.1)
    for s in ("SU26238RMFS4", "SU26244RMFS2"):  # the par buckets sum to the DV01 at the bond's own yield (<= 1 %)
        v = outs[s].valuation
        assert v.dv01_rub == pytest.approx(-v.modified_duration * v.pv_rub * 1e-4, rel=0.01), s
    for s in ONE_CURVE:
        v = outs[s].valuation
        assert v.discount_margin_bp == pytest.approx(v.z_spread_bp, abs=1e-6), s
    for s in KEY:
        v = outs[s].valuation
        assert v.discount_margin_bp < v.z_spread_bp - 2.0, s  # KEY projected above RUONIA: DM under Z
    assert outs["RU000A10AAU6"].valuation.discount_margin_bp == pytest.approx(21.25, abs=0.01)
    assert outs["RU000A10AAU6"].valuation.z_spread_bp == pytest.approx(24.25, abs=0.01)


def test_fx_bonds_pnl_at_the_19_00_rate(run):
    ctx, outs = run
    usd = ctx.market.fx("USD")
    assert (usd["moex_19"], usd["cbr"], usd["cbr_next"]) == (82.8879, 82.8504, 82.8879)
    o = outs["RU000A10D806"]
    p = o.pnl
    assert p.fx_gross == pytest.approx(3618.34, abs=0.01) and p.fx_gross_cbr == pytest.approx(-74036.01, abs=0.01)
    # batch 1, item 5: a day of carry at the bond's yield (10.6878 %) out of other; without a USD curve the yield move
    # stays in other
    assert p.carry == pytest.approx(2225.30, abs=0.01) and p.extra()["method"] == "desk-flash-yield-v1"
    assert p.other == pytest.approx(41468.06 - 2225.30, abs=0.01) and p.total == pytest.approx(45086.40, abs=0.01)
    for s, ccy in FX.items():
        v = outs[s].valuation
        assert any("MOEX 19:00" in a for a in v.assumptions), s
        q = outs[s].pnl
        assert q.carry > 0 and q.carry + q.other + q.fx_gross == pytest.approx(q.total), s
        delta = q.v0 / (82.8504 if ccy == "USD" else 12.4176)  # the D-1 FX delta at the 19.10 19:00 rate
        now = ctx.market.fx(ccy)["moex_19"]
        assert q.fx_gross == pytest.approx(delta * (now - (82.8504 if ccy == "USD" else 12.4176)), rel=1e-9), s


def test_flash_pnl_of_an_ofz_splits_its_linear_rates_by_ruonia_and_basis(run):
    """#213: an OFZ in RUONIA + basis: the first-order check of its rates PnL (``rates_linear``: its basis risk, the
    KBD's par risk, x the KBD's par move, as before) by its two factors, RUONIA (its RUONIA risk x the OIS par move) and
    the basis (the rest)."""
    _, outs = run
    for s in ("SU26238RMFS4", "SU26244RMFS2"):
        p = outs[s].pnl
        ruo, bas = p.rates_linear_ofz
        assert ruo + bas == pytest.approx(p.rates_linear, rel=1e-12, abs=1e-9), s
        assert p.extra()["rates_linear_ruonia"] == ruo and p.extra()["rates_linear_basis"] == bas
        assert abs(p.rates_linear - p.rates) < 0.2 * abs(p.rates), s  # a first-order check of a sub-bp day
    assert outs["RU000A10AS02"].pnl.rates_linear_ofz is None
