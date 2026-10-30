"""TDM#418: a foreign bond's linear rates (``rates_linear``, ``rates_linear_curves``) are in RUB at the D rate, as its
exact ``rates`` (``pnl.flash`` module docstring, «Units of the linear terms»).

Core gives a foreign bond's D-1 bucket DV01 in RUB at the D-1 rate (``fx_translated_factor_risk``); the exact steps are
in its currency x the D rate. On the synthetic CNY (XCCY) and USD (UST proxy) bonds of ``test_batch1_fx_pnl``, rolled a
day with the curve quotes +25 bp and the FX rates x 1.10 (invented moves): the linear estimate is the exact repricing
to its convexity, and the same day without the FX move gives the same linear rates in the bond's currency, so the FX
move is in ``fx_gross`` only.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest
from test_batch1_fx_pnl import NEXT, _pnl, _put, _roll
from test_fx_core_035 import CNY_BOND, DATE, USD_BOND, synthetic_dir

from desk.api.pnl_checks import LIN_PCT
from desk.marketdata.bundle import SnapshotDir
from desk.pnl.flash import FlashPnl

FX_MOVE, QUOTE_MOVE = Decimal("1.10"), Decimal("0.25")
CONVEXITY_REL = 0.02
"""The second order of a 25 bp move on these bonds, relative to their rates PnL (under 0.5 %), with room; the FX move
(10 %) is far outside it."""


def _day(tmp_path_factory, fx_move: Decimal) -> SnapshotDir:
    tmp = tmp_path_factory.mktemp(f"fx_linear_{fx_move}")
    sd = synthetic_dir(tmp)
    _put(tmp, _roll(sd.market(DATE, "eod_final").doc, DATE, NEXT, fx_move, QUOTE_MOVE))
    return SnapshotDir(tmp)


@pytest.fixture(scope="module")
def fx_day(tmp_path_factory) -> SnapshotDir:
    return _day(tmp_path_factory, FX_MOVE)


@pytest.fixture(scope="module")
def flat_fx_day(tmp_path_factory) -> SnapshotDir:
    return _day(tmp_path_factory, Decimal(1))


def _flash(sd: SnapshotDir, secid: str, proxy: bool) -> FlashPnl:
    _, _, out = _pnl(sd, [(secid, 1000.0)], proxy=proxy)
    f = out[secid].pnl
    assert isinstance(f, FlashPnl) and f.fx_rates is not None, out[secid].no_pnl
    return f


@pytest.mark.parametrize("secid,proxy", [(CNY_BOND, False), (USD_BOND, True)])
def test_linear_rates_in_rub_at_the_d_rate(fx_day, secid, proxy):
    f = _flash(fx_day, secid, proxy)
    f0, f1 = f.fx_rates
    assert f1 / f0 == pytest.approx(float(FX_MOVE), rel=1e-9) and abs(f.rates) > 1e4
    # the linear estimate is the exact repricing to its convexity, both in RUB at the D rate
    assert f.rates_linear == pytest.approx(f.rates, rel=CONVEXITY_REL)
    # at the D-1 rate (before TDM#418) it missed by the FX move, a false finding of the screen's check
    assert abs(f.rates_linear * f0 / f1 - f.rates) > LIN_PCT * abs(f.rates)
    terms = f.extra()["rates_linear_curves"]
    assert math.fsum(x for bs in terms.values() for x in bs.values()) == pytest.approx(f.rates_linear, rel=1e-12)
    assert f.explained == pytest.approx(f.total, abs=0.01)


@pytest.mark.parametrize("secid,proxy", [(CNY_BOND, False), (USD_BOND, True)])
def test_a_pure_fx_move_stays_out_of_the_linear_rates(fx_day, flat_fx_day, secid, proxy):
    moved, flat = _flash(fx_day, secid, proxy), _flash(flat_fx_day, secid, proxy)
    (m0, m1), (n0, n1) = moved.fx_rates, flat.fx_rates
    assert m0 == n0 and n1 == n0 and m1 / m0 == pytest.approx(float(FX_MOVE), rel=1e-9)
    assert flat.fx_gross == 0.0 and moved.fx_gross == pytest.approx(moved.fx_delta_ccy * (m1 - m0))
    # the same curves, so the same rates in the bond's currency: the rates and their linear terms only take the D rate
    assert moved.rates / m1 == pytest.approx(flat.rates / n1, rel=1e-9)
    assert moved.rates_linear / m1 == pytest.approx(flat.rates_linear / n1, rel=1e-9)
    mt, ft = moved.rates_linear_curves, flat.rates_linear_curves
    assert mt is not None and ft is not None and mt.keys() == ft.keys()
    for c, bs in mt.items():
        assert bs.keys() == ft[c].keys()
        for b, x in bs.items():
            assert x / m1 == pytest.approx(ft[c][b] / n1, rel=1e-9, abs=1e-12)
