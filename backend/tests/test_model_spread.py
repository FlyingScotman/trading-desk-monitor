"""Slice S5: a model price keeps its spread from run to run (carried), so only the curve moves it."""

from __future__ import annotations

from datetime import timedelta

import pytest
from test_fallback import FIX, RZD, Q, _fb, _History, _outcome
from test_slice_s2 import shifted_prev

from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare, value
from desk.valuation import fallback
from desk.valuation.fallback import CarriedSpread, approximate, used_model_spreads

MEDIAN_BP = 150.0


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), "2026-10-15")


@pytest.fixture(scope="module")
def exact(ctx):
    return {o.secid: o for o in value(ctx, [(RZD, Q)])}


def _model(ctx, o, carried=None, median_bp=MEDIAN_BP):
    """``o`` with no price in the window, priced on the model with the issuer median forced to ``median_bp``."""
    fb = _fb(ctx, [_History(ctx, 40)])
    fb.carried = {c.secid: c for c in carried or ()}
    fb.medians = None if median_bp is None else {("issuer", o.terms.issuer_id): (median_bp, 3)}
    return approximate(ctx, _outcome(o, "no_quote", quote=None), Q, fb), fb


def test_median_without_carried_spread(ctx, exact):
    o = exact[RZD]
    v, _ = _model(ctx, o)
    assert v.quality == "model_price" and v.z_spread_bp == MEDIAN_BP
    assert v.model_spread_source == f"issuer median Z 150 bp (3 bonds) on {ctx.business_date}"
    (c,) = used_model_spreads([_with(o, v), exact[RZD]])  # the exact one is not a model price
    assert (c.secid, c.z_spread_bp, c.source, c.business_date) == (RZD, MEDIAN_BP, v.model_spread_source,
                                                                  ctx.business_date)


def _with(o, v):
    out = _outcome(o, "no_quote", quote=None)
    out.valuation = v
    return out


def test_carried_spread_is_used_and_named(ctx, exact):
    o, d0 = exact[RZD], ctx.business_date - timedelta(3)
    src = f"last market Z {o.valuation.z_spread_bp:.0f} bp on {d0} (run 12)"
    c = CarriedSpread(RZD, o.valuation.z_spread_bp, d0, src)
    v, fb = _model(ctx, o, [c], median_bp=None)
    assert fb.medians is None  # every model price carried: the universe medians are never computed
    assert v.quality == "model_price" and v.z_spread_bp == c.z_spread_bp and v.model_spread_source == src
    assert v.pv_rub == pytest.approx(o.valuation.pv_rub, rel=1e-7)  # the engine at its own exact Z
    assert v.pv_rub != pytest.approx(_model(ctx, o)[0].pv_rub, rel=1e-6)  # not the median price
    assert any(a.startswith(f"model price: RUB RUONIA + Z {c.z_spread_bp:.0f} bp carried from {d0} ({src})")
               for a in v.assumptions)
    # far outside Z_BAND_BP and still the bond's own evidence: used
    wide, _ = _model(ctx, o, [CarriedSpread(RZD, 3000.0, d0, "last market Z 3000 bp")])
    assert wide.z_spread_bp == 3000.0 and wide.pv_rub < v.pv_rub


def test_a_spread_to_another_curve_is_not_carried(ctx, exact):
    """A run before the RUONIA switch has spreads to the KBD (``RUB OFZ``): a model price on RUONIA does not reuse
    one, it takes the day's median, and says why."""
    o, d0 = exact[RZD], ctx.business_date - timedelta(3)
    c = CarriedSpread(RZD, 50.0, d0, "last market Z 50 bp", "RUB OFZ")
    v, _ = _model(ctx, o, [c])
    assert v.z_spread_bp == MEDIAN_BP and v.model_spread_source.startswith("issuer median Z")
    assert any("carried Z 50 bp" in a and "is to RUB OFZ, not RUB RUONIA" in a for a in v.assumptions)
    ok, _ = _model(ctx, o, [CarriedSpread(RZD, 50.0, d0, "x", "RUB RUONIA")])
    assert ok.z_spread_bp == 50.0 and ok.discount_curve_id == "RUB_RUONIA_OIS" and set(ok.risk()) == {"RUB RUONIA"}


def test_a_carried_spread_the_engine_rejects_falls_to_the_median(ctx, exact, monkeypatch):
    real = fallback._engine

    def engine(*a, spread_bp=None, **k):
        if spread_bp == 777.0:
            raise ValueError("no root")
        return real(*a, spread_bp=spread_bp, **k)
    monkeypatch.setattr(fallback, "_engine", engine)
    o = exact[RZD]
    v, _ = _model(ctx, o, [CarriedSpread(RZD, 777.0, ctx.business_date, "x")])
    assert v.z_spread_bp == MEDIAN_BP and v.model_spread_source.startswith("issuer median Z 150 bp")
    assert any("carried Z 777 bp" in a and "failed (no root)" in a for a in v.assumptions)


def test_two_dates_keep_the_same_spread(ctx, exact):
    """D-1 on the issuer median; D, whose median moved, carries D-1's spread: same Z, the curve moves the price."""
    o = exact[RZD]
    prev = shifted_prev(ctx, ctx.business_date - timedelta(1))  # the KBD of D-1, shifted
    v0, _ = _model(prev, o)
    carried = used_model_spreads([_with(o, v0)])
    v1, _ = _model(ctx, o, carried, median_bp=MEDIAN_BP + 300)
    assert v1.z_spread_bp == v0.z_spread_bp == MEDIAN_BP
    assert v1.model_spread_source == v0.model_spread_source and v1.dirty_rub != pytest.approx(v0.dirty_rub)
    assert used_model_spreads([_with(o, v1)])[0].source == v0.model_spread_source  # propagates run after run
