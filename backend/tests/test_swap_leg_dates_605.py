"""TDM#605: swap legs with dates of their own. A CCS (core's ``XccySwap`` takes each leg's periods) is valued with each
leg's periods and exchanges on that leg's start and maturity: a maturity a day apart, a foreign leg starting later;
common dates value as before. An IRS (core's ``InterestRateSwap``: one pair of dates, core#106) is still refused, its
reason naming both legs' dates. On the hub's contract 1.4 golden snapshot of 15.10.2026, synthetic trades."""

from __future__ import annotations

import pytest
from test_swaps_repo_tdm45 import BD, FIX, ccs, ccs_plan, leg

from desk.api import coverage
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare
from desk.valuation import derivatives as dv


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), BD.isoformat())


def ccs_dates(rub=("2026-10-06", "2028-10-06"), cny=("2026-10-06", "2028-10-06")):
    t = ccs("C1", "pay", 1_250_000_000, 100_000_000, index_code="RUONIA")
    for r, (s, e) in zip(t, (rub, cny), strict=True):
        r["start_date"], r["maturity_date"] = s, e
    return t


def value(ctx, terms):
    bp, _ = ccs_plan(ctx, terms)
    o = bp.direct["C1"]
    assert o.core_not_valued is None and o.valuation is not None, (o.reason, o.detail, o.core_not_valued)
    return o.valuation


def test_common_dates_value_as_before(ctx):
    v = value(ctx, ccs_dates())
    (fact,) = v.extra["swaps"]
    assert "leg_dates" not in fact and fact["start_date"] == "2026-10-06" and fact["maturity_date"] == "2028-10-06"


def test_a_maturity_a_day_apart(ctx):
    same = value(ctx, ccs_dates())
    v = value(ctx, ccs_dates(cny=("2026-10-06", "2028-10-09")))  # 09.10.2028: the next business day after 06.10
    (fact,) = v.extra["swaps"]
    assert fact["maturity_date"] == "2028-10-09"
    assert fact["leg_dates"] == {"RUB": {"start_date": "2026-10-06", "maturity_date": "2028-10-06"},
                                 "CNY": {"start_date": "2026-10-06", "maturity_date": "2028-10-09"}}
    assert v.pv_rub != same.pv_rub and v.pv_rub == pytest.approx(same.pv_rub, rel=0.05)


def test_a_foreign_leg_that_starts_later(ctx):
    """The CNY leg starts on 06.04.2027 (forward start), the RUB leg is seasoned: the CNY initial exchange is still to
    come, so the CCS's OCP of CNY nets it against the final one."""
    v = value(ctx, ccs_dates(cny=("2027-04-06", "2028-10-06")))
    (fact,) = v.extra["swaps"]
    assert fact["start_date"] == "2026-10-06" and fact["leg_dates"]["CNY"]["start_date"] == "2027-04-06"
    t = dv.CcsTerms.of(ccs_dates(cny=("2027-04-06", "2028-10-06")))
    assert (t.foreign.start.isoformat(), t.rub.start.isoformat()) == ("2027-04-06", "2026-10-06")
    assert dv.ccs_exchange_amount(t.foreign.exchange, t.foreign.side, t.foreign.notional, t.foreign.start,
                                  t.foreign.maturity, BD) == 0.0  # receive: -N now (to come) + N at the end


def test_a_leg_ending_before_it_starts_is_invalid():
    with pytest.raises(dv.NotValued) as e:
        dv.CcsTerms.of(ccs_dates(cny=("2027-04-06", "2027-04-06")))
    assert e.value.reason == "terms_invalid"


def test_an_irs_with_legs_of_different_dates_names_both(ctx):
    t = [leg("S1", "fix", "pay", fixed_rate_pct="15.5"),
         leg("S1", "flt", "receive", index_code="RUONIA", start_date="2026-10-07", maturity_date="2028-10-09")]
    with pytest.raises(dv.NotValued) as e:
        dv.SwapTerms.of(t)
    code, detail = e.value.reason, e.value.detail
    assert code == "swap_dates_mismatch"
    assert detail == "сделка S1: фиксированная нога 06.10.2026–06.10.2028, плавающая 07.10.2026–09.10.2028"
    why = coverage.explain(f"{code}: {detail}", "IRS", None, None)
    assert "IRS" in why["why"] and why["detail"] == detail
