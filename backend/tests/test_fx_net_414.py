"""TDM#414: the pages show FX PnL only as one source «FX (нетто ОВП)» = fx_gross + fx_funding (``summary.fx_net``),
None only when both parts are missing; the display builders (summary factors, the concept pages' and the Desk
Monitor's position PnL, the run totals' FX basis) take it from there."""

from __future__ import annotations

import pytest

from desk.api import compat, concepts_run
from desk.publish import summary


def test_fx_net_sums_the_parts_and_is_none_only_without_both():
    assert summary.fx_net({"fx_gross": 1_000_000.0, "fx_funding": -990_000.0}) == pytest.approx(10_000.0)
    assert summary.fx_net({"fx_gross": 0.0, "fx_funding": 0.0}) == 0.0
    assert summary.fx_net({}) is None and summary.fx_net(None) is None
    assert summary.fx_net({"fx_gross": None, "fx_funding": None}) is None
    assert summary.fx_net({"fx_gross": 5.0}) == 5.0 and summary.fx_partial({"fx_gross": 5.0})
    assert summary.fx_net({"fx_funding": -7.0}) == -7.0 and summary.fx_partial({"fx_funding": -7.0})
    assert not summary.fx_partial({"fx_gross": 5.0, "fx_funding": -7.0}) and not summary.fx_partial({})


def test_fx_net_by_cbr_rates_takes_the_market_part_without_its_cbr_amount():
    q = {"fx_gross": 100.0, "fx_funding": -90.0, "fx_gross_cbr": 120.0}
    assert summary.fx_net(q, cbr=True) == pytest.approx(30.0)  # 120 (CBR) - 90 (market: no fx_funding_cbr)
    assert summary.fx_net({**q, "fx_funding_cbr": -100.0}, cbr=True) == pytest.approx(20.0)
    assert summary.fx_net({}, cbr=True) is None


def test_display_builders_show_one_fx_factor():
    q = {"carry": 10.0, "rolldown": 0.0, "rates": 0.0, "spread": 0.0, "convexity": 0.0, "other": 0.0,
         "new_trades": 0.0, "funding": -3.0, "fx_gross": 1_000_000.0, "fx_funding": -990_000.0}
    p = summary.pnl_factors(q)
    assert p["fx"] == pytest.approx(10_000.0) and not {"fx_gross", "fx_funding"} & set(p)
    assert summary.pnl_factors({k: v for k, v in q.items() if not k.startswith("fx")})["fx"] is None
    assert concepts_run._pnl(q)["fx"] == pytest.approx(10_000.0)
    assert not {"fxGross", "fxFunding"} & set(concepts_run._pnl(q))
    # the run totals' official fallback: the explained plus the net FX by the CBR rates minus the market one
    off, exp, _, _ = compat.run_totals([{"fact": {}, "pnl": {**q, "fx_gross_cbr": 1_100_000.0}}])
    assert off - exp == pytest.approx(100_000.0)
