"""TDM#509: the calendar a swap's schedule is rolled on reaches the swap's start, not only the hub's first day.

Core rolls both legs from the swap's start and adjusts every period end on the calendar, the past ones too. A live swap
with periods that began before the hub's calendars (a window starting on 1 January: the golden cut's, a young hub) was
``terms_invalid`` ("outside calendar"); now the days before the calendar follow the fixed rules (said in the notes) and
the swap is valued as one started on the first period of the day. Only a refusal for the calendar is told apart: another
refusal of the schedule is not a swap that ended and keeps the terms' reason with its message. On the hub's contract 1.4
golden cut of 15.10.2026 (calendars from 01.01.2026) and invented trades."""

from __future__ import annotations

import copy
import dataclasses
from datetime import date
from types import SimpleNamespace

import pytest
from rub_fixed_income import CalendarRangeError, InvalidTerms
from test_fixings_history_590 import stored, value
from test_swaps_repo_tdm45 import BD, FIX, leg

from desk.marketdata.adapter import StaticView
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare
from desk.valuation import derivatives as dv


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), BD.isoformat(), fixings=stored())


def ois(start, end, freq="3M", index="RUONIA"):
    kw = {"start_date": start, "maturity_date": end, "payment_frequency": freq, "day_count": "ACT/365"}
    return [leg("Y1", "fix", "pay", fixed_rate_pct="15.5", **kw), leg("Y1", "flt", "receive", index_code=index, **kw)]


def test_a_swap_with_periods_before_the_hubs_calendar_is_valued(ctx):
    """Quarterly from 10.03.2025: its first periods end in 2025, before the calendars' first day (01.01.2026). It is valued
    as the same swap begun on its current period (10.09.2026): the past periods change nothing but the schedule."""
    assert ctx.static.calendars["RU_PRODUCTION"]["valid_from"] == "2026-01-01"
    seasoned, current = value(ctx, ois("2025-03-10", "2030-03-10")), value(ctx, ois("2026-09-10", "2030-03-10"))
    for o in (seasoned, current):
        assert o.core_not_valued is None and o.valuation is not None, (o.reason, o.detail, o.core_not_valued)
    v = seasoned.valuation
    assert v.pv_rub == pytest.approx(current.valuation.pv_rub, rel=1e-12) and v.pv_rub == pytest.approx(-34_636_940.47, abs=1)
    assert v.extra["core"]["product"] == "ois" and v.extra["swaps"][0]["start_date"] == "2025-03-10"
    assert any("start on 2026-01-01" in a and "fixed holiday rules" in a for a in v.assumptions)
    assert not any("fixed holiday rules" in a for a in current.valuation.assumptions)  # the calendar as the hub's


def test_a_key_rate_swap_and_a_longer_period_too(ctx):
    for index in ("RUONIA", "KEY_RATE"):
        o = value(ctx, ois("2024-09-10", "2029-09-10", "6M", index))
        assert o.reason is None and o.valuation is not None and o.core_not_valued is None, (index, o.reason, o.detail)


def test_the_calendar_reaches_the_earliest_swap_of_the_run(ctx):
    """A later swap of the run does not shorten the calendar an earlier one needed, an earlier one lengthens it."""
    bd, cals = ctx.business_date, {}
    terms = lambda s, e: dv.SwapTerms.of(ois(s, e))
    dv._swap_instrument(ctx, terms("2025-09-10", "2030-03-10"), "a", cals)
    first = cals["OIS:RUONIA"].calendar
    assert first.valid_from == date(2025, 9, 10) and "+rules_from_2025-09-10" in first.version
    dv._swap_instrument(ctx, terms("2026-03-10", "2030-03-10"), "b", cals)
    assert cals["OIS:RUONIA"].calendar is first
    dv._swap_instrument(ctx, terms("2024-03-10", "2030-03-10"), "c", cals)
    assert cals["OIS:RUONIA"].calendar.valid_from == date(2024, 3, 10)
    # the days the hub states are the hub's: the extension adds days before its first day only
    hub, _ = dv.rates.ois_calendar(ctx.static.snapshot, bd)
    ext = cals["OIS:RUONIA"].calendar
    assert hub.valid_from == date(2026, 1, 1) and ext.valid_to == hub.valid_to
    assert all(ext.is_business_day(d) == hub.is_business_day(d) for d in (date(2026, 1, 1), date(2026, 5, 11),
                                                                         date(2026, 12, 31), date(2027, 3, 8)))
    assert not ext.is_business_day(date(2025, 5, 9)) and not ext.is_business_day(date(2025, 3, 8))  # fixed rules, weekend
    assert ext.is_business_day(date(2025, 3, 11))


def test_january_a_swap_begun_the_year_before_a_calendar_that_starts_on_1_january(ctx):
    """The case of the issue: business date 15.01.2027, a calendar from 01.01.2027, a swap begun in March 2026."""
    snap = ctx.static.snapshot
    rows = copy.deepcopy(snap.data["calendars"])
    for c in rows:
        c["valid_from"] = "2027-01-01"
        c["holidays"] = [h for h in c["holidays"] if h >= "2027-01-01"]
        c["working_weekends"] = [h for h in c["working_weekends"] if h >= "2027-01-01"]
    static = StaticView(dataclasses.replace(snap, doc={**snap.doc, "data": {**snap.data, "calendars": rows}}))
    jan = SimpleNamespace(business_date=date(2027, 1, 15), static=static, swap_curves=ctx.swap_curves,
                          core_market=ctx.core_market)
    for index in ("ruonia", "key_rate"):
        terms = dv.SwapTerms.of(ois("2026-03-10", "2030-03-10", "3M", index.upper()))
        cals: dict = {}
        inst, notes = dv._swap_instrument(jan, terms, "j", cals)
        cal = cals[dv.SWAP_CALENDAR[index]].calendar
        assert inst.swap.effective_date == date(2026, 3, 10) and cal.valid_from == date(2026, 3, 10)
        sched = dv.swap_schedule(inst.swap, cal)
        assert sched.floating[0].accrual_end == date(2026, 6, 10) and len(sched.floating) == 16
        assert any("start on 2027-01-01" in n for n in notes)
        with pytest.raises(CalendarRangeError):  # what it was: the hub's calendar alone does not hold the first period
            dv.swap_schedule(inst.swap, dv.rates.ois_calendar(static.snapshot, jan.business_date)[0])


@pytest.mark.parametrize("refusal", [InvalidTerms("adjusted swap schedule dates must be strictly increasing"),
                                     ValueError("bad frequency")])
def test_another_refusal_of_the_schedule_is_the_terms_and_never_a_settled_swap(ctx, monkeypatch, refusal):
    """Before: any refusal of ``swap_schedule`` of a swap past its maturity was ``settled``, silently. Now it is the
    caller's: ``terms_invalid`` with core's message, for a live swap as for one that ended a few days ago."""
    def boom(*a, **k):
        raise refusal

    monkeypatch.setattr(dv, "swap_schedule", boom)
    ended = dv.SwapTerms.of(ois("2026-04-10", "2026-10-12"))  # three days before the business date
    with pytest.raises(type(refusal), match=str(refusal)):
        dv._swap_instrument(ctx, ended, "e", {})
    for start, end in (("2026-04-10", "2026-10-12"), ("2025-03-10", "2030-03-10")):
        o = value(ctx, ois(start, end))
        assert o.valuation is None and o.reason == "terms_invalid" and str(refusal) in o.detail


def test_a_calendar_refusal_is_told_apart(ctx, monkeypatch):
    """A date outside the calendar: ``settled`` for a swap that ended, ``core_calendar_out_of_range`` (the text of the
    coverage: no calendar) for a live one, not the terms' fault."""
    def outside(*a, **k):
        raise CalendarRangeError("2079-12-31 is outside calendar X (2026-01-01..2079-12-31)")

    monkeypatch.setattr(dv, "swap_schedule", outside)
    with pytest.raises(dv.NotValued) as e:
        dv._swap_instrument(ctx, dv.SwapTerms.of(ois("2026-04-10", "2026-10-12")), "e", {})
    assert e.value.reason == "settled" and e.value.last == date(2026, 10, 12)
    with pytest.raises(dv.NotValued) as e:
        dv._swap_instrument(ctx, dv.SwapTerms.of(ois("2025-03-10", "2030-03-10")), "l", {})
    assert e.value.reason == "core_calendar_out_of_range" and "outside calendar" in e.value.detail
    o = value(ctx, ois("2025-03-10", "2030-03-10"))
    assert o.valuation is None and o.reason == "core_calendar_out_of_range"
