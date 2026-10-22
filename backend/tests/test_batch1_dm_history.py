"""Batch 1, item 6: a floater's DM on every date of its price history, not only from the day the position appeared
(R35; ``api.concepts.dm_from_price``). The deep history through the API is in ``test_hub_history``."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from desk.api.concepts import _annuity, dm_from_price

D0 = date(2026, 1, 5)
HORIZON = date(2029, 1, 5)


def test_dm_from_the_price_around_the_runs():
    dates = [D0 + timedelta(days=i) for i in range(5)]
    price = [99.0, 99.5, None, 100.0, 100.2]
    # runs on dates 3 and 4: DM 120 / 110, CS01 -300 RUB/bp on 1 mm face (spread duration 3 bp-years per 1e4)
    anchors = [(3, 120.0, 100.0, -300.0, 1e6), (4, 110.0, 100.2, -300.0, 1e6)]
    out = dm_from_price(dates, price, anchors, HORIZON, 0.0)
    assert out[3] == pytest.approx(120.0) and out[4] == pytest.approx(110.0)  # a run date: the run's DM
    assert out[2] is None  # no price
    # before the first run: from it, the spread duration of the run averaged with the one moved to the date
    for i in (0, 1):
        t_a, t_d = (HORIZON - dates[3]).days / 365.25, (HORIZON - dates[i]).days / 365.25
        sd = 3e-4 * (1 + t_d / t_a) / 2
        assert out[i] == pytest.approx(120.0 - (price[i] - 100.0) / 100 / sd, abs=0.01)
    assert out[0] > out[1] > out[3]  # a lower price is a wider margin


def test_dm_from_the_price_short_position_and_edges():
    dates = [D0, D0 + timedelta(days=1)]
    short = dm_from_price(dates, [99.0, 100.0], [(1, 50.0, 100.0, 300.0, -1e6)], HORIZON, 0.15)
    long_ = dm_from_price(dates, [99.0, 100.0], [(1, 50.0, 100.0, -300.0, 1e6)], HORIZON, 0.15)
    assert short == long_ and short[0] > 50.0  # the sign of the position does not change the margin
    assert dm_from_price(dates, [99.0, 100.0], [(1, 50.0, 100.0, -300.0, 1e6)], None, 0.0) is None
    assert dm_from_price(dates, [99.0, 100.0], [(1, 50.0, 100.0, 0.0, 1e6)], HORIZON, 0.0) is None  # no spread risk
    assert dm_from_price(dates, [99.0, 100.0], [(1, 50.0, 100.0, -300.0, 1e6)], D0, 0.0) is None  # past the horizon
    assert _annuity(2.0, 0.0) == 2.0 and _annuity(2.0, 0.1) == pytest.approx((1 - 2.718281828 ** -0.2) / 0.1)
