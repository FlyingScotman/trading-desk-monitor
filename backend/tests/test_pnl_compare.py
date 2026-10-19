"""Slice S4 step 4: flash PnL vs core ``explain_horizon_pnl`` on the vendored fixture.

The previous date is synthetic as in test_slice_s2 (KBD level 10 bp lower, prices 0.05 higher).

Tolerances: carry, rolldown and rates are the same three repricings in both (same core pricer,
same flows, the same curve nodes), so they agree to rounding, 1e-6 RUB per 1000 bonds.
``spread + other`` agrees to the same order, and core's total equals the identity to the
flash identity tolerance (0.01 RUB): both calibrate their spreads to the same dirty prices,
to the Z-spread solver's tolerance. On the dev snapshots (positions up to 2 bn RUB) the largest
differences were 2e-7 RUB (carry, rolldown, rates) and 6e-4 RUB (identity).
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest
from test_slice_s2 import FIX, prev_outcome, shifted_prev

from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import attach_pnl, prepare, value
from desk.pnl.compare import compare
from desk.pnl.flash import IDENTITY_TOLERANCE_RUB

DATE = "2026-10-15"


def _rows(ctx, outcomes):
    prev = shifted_prev(ctx, ctx.business_date - timedelta(days=1))
    prevs = [prev_outcome(prev, o) for o in outcomes]
    cur = [dataclasses.replace(o) for o in outcomes]
    attach_pnl(prev, prevs, ctx, cur)
    return compare(prev, prevs, ctx, cur)


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), DATE)


def test_flash_and_core_agree(ctx):
    outcomes = [o for o in value(ctx, [(s, 1000.0) for s in sorted(ctx.static.bonds)], fallback=False)
                if o.valuation and o.valuation.quality == "exact"]  # core's explain: fixed bonds, no assumptions
    rows = _rows(ctx, outcomes)
    assert len(rows) == sum(o.valuation is not None for o in outcomes) >= 3
    for r in rows:
        assert r.flash and r.core, r.reason
        d = r.delta
        assert max(abs(d[c]) for c in ("carry", "rolldown", "rates", "spread+other")) < 1e-6, (r.secid, d)
        assert abs(r.core["total"] - r.identity_total) < IDENTITY_TOLERANCE_RUB
        assert abs(r.identity_error("core")) < IDENTITY_TOLERANCE_RUB
        assert r.z is not None and abs(r.z[2] - r.z[0]) < 1e-6 and abs(r.z[3] - r.z[1]) < 1e-6
        assert r.flash["other"] == pytest.approx(0, abs=1e-6)  # the fixture's schedule does not change
        # core's residual is the spread x curve cross term: small next to the spread move
        assert abs(r.core["other"]) < 0.05 * abs(r.flash["spread"]) + 1e-6



def test_only_core_valued_bonds_on_the_filled_schedule(ctx, monkeypatch):
    """An assumed bond is compared on the schedule its valuation filled; the fallback rungs are left out."""
    t = ctx.static.bonds["RU000A0JXQ44"]
    nxt = next(c for c in t.coupons if c.pay_date > ctx.business_date)
    gap = dataclasses.replace(t, coupons=tuple(dataclasses.replace(c, amount_per_bond=None) if c == nxt else c
                                               for c in t.coupons))
    monkeypatch.setitem(ctx.static.bonds, t.secid, gap)
    (o,) = value(ctx, [(t.secid, 1000.0)])
    assert o.valuation.quality == "assumed"
    (r,) = _rows(ctx, [o])
    assert r.core and r.flash, r.reason
    assert max(abs(r.delta[c]) for c in ("carry", "rolldown", "rates", "spread+other")) < 1e-6
    mv = dataclasses.replace(o, valuation=dataclasses.replace(o.valuation, quality="market_value"))
    assert compare(ctx, [mv], ctx, [mv]) == []
