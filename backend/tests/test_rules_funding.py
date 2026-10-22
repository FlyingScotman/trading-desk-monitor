"""Funding engine (``desk.rules.funding``, P2) on synthetic rules and data: no firm values.

Calendar: Fri 2026-10-16, Mon 2026-10-19, Tue 2026-10-20. Index IDX_K steps from 10 % to 12 % effective
Monday; IDX_ON is an overnight series with weekday fixings only. Rates are decimals, spreads in bp.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest

from desk.marketdata.adapter_rates import Fixing
from desk.rules.funding import (
    OPEN_DECISIONS,
    PROTOTYPE_POLICY,
    PROVISIONAL_POLICY,
    AccrualPeriod,
    BaseDate,
    CashBalance,
    ClosedPosition,
    DayBasis,
    DayBasisRule,
    DayCountPolicy,
    EntityType,
    ExtraSpread,
    FundingBase,
    FundingPolicy,
    FundingPosition,
    FundingRule,
    FundingRules,
    FxDate,
    HtmBase,
    HtmLot,
    LineKind,
    NewPosition,
    OutOfCurrency,
    Periodicity,
    RepoDeal,
    RepoPolicy,
    RepoRate,
    WriteOffDate,
    WriteOffPolicy,
    compute_funding,
    funding_liabilities,
    last_write_off,
    liability_fx_pnl,
    total_by,
    write_off_dates,
)
from desk.rules.rate_index import FixingTable, FxTable

FRI, SAT, SUN, MON, TUE = (date(2026, 10, d) for d in (16, 17, 18, 19, 20))
WEEKEND = AccrualPeriod(FRI, MON)
ONE_DAY = AccrualPeriod(MON, TUE)

RATES = FixingTable({
    "IDX_K": {date(2026, 9, 1): 0.10, MON: 0.12},
    "IDX_ON": {FRI: 0.09, MON: 0.095, TUE: 0.096},
})
FX = FxTable("RUB", {"USD": {FRI: 80.0, MON: 82.0, TUE: 81.0}, "CNY": {FRI: 11.0, MON: 11.2}})

RULES = FundingRules((
    FundingRule(ccy="RUB", book_type="TRADING", index="IDX_K", spread_bp=100),
    FundingRule(ccy="USD", book_type="TRADING", fixed_rate=0.04),
    FundingRule(ccy="RUB", book_type="DERIVS", index="IDX_ON"),
    FundingRule(ccy="USD", book_type="DERIVS", fixed_rate=0.03),
))


# The conventions these tests run under, set explicitly (the engine's defaults leave the market-value base, the HTM
# base and the write-off unset): synthetic choices, not anyone's rules.
POLICY = FundingPolicy(
    base_by_book_type={"TRADING": FundingBase.MV_CLEAN, "HTM": FundingBase.COST, "DERIVS": FundingBase.CASH,
                       "FUTURES": FundingBase.NONE},
    htm_base=HtmBase.CASH_SPENT, repo=RepoPolicy(RepoRate.SECURITY), entity_cash_rate_book_type="TRADING")


def pos(pid="P1", mv=1_000_000.0, *, book="B1", book_type="TRADING", entity="E1", ccy="RUB", accrued=0.0, **kw):
    return FundingPosition(pid, book, book_type, entity, ccy, mv, accrued, **kw)


def run(period=ONE_DAY, rules=RULES, policy=POLICY, **kw):
    return compute_funding(period, rules, RATES, FX, policy=policy, **kw)


def only(result, kind=None):
    lines = [x for x in result.lines if kind is None or x.kind is kind]
    assert len(lines) == 1, (result.lines, result.problems)
    return lines[0]


# ---------------------------------------------------------------------------------------------- accrual
def test_one_day_bond_charge_previous_day_clean_mv():
    r = run(positions=[pos(mv=2e6, accrued=5e4)], previous_positions=[pos(mv=1e6, accrued=4e4)])
    line = only(r)
    assert (line.kind, line.base, line.rate_expr) == (LineKind.POS, FundingBase.MV_CLEAN, "IDX_K + 100 bp")
    (d,) = line.days
    assert (d.day, d.body, d.basis) == (TUE, -1e6, DayBasis.ACT_365)
    assert d.rate == pytest.approx(0.13)
    assert line.interest == pytest.approx(-1e6 * 0.13 / 365)
    assert r.by_position() == {"P1": pytest.approx(line.interest)}
    assert r.by_book() == {"B1": pytest.approx(line.interest)}
    assert r.by_entity() == {"E1": pytest.approx(line.interest)}
    assert r.total == pytest.approx(line.interest) and not r.problems


def test_weekend_accrues_three_calendar_days_and_monday_rate_change():
    """Friday -> Monday: Sat and Sun at the old index value, Monday at the new one."""
    line = only(run(WEEKEND, positions=[pos()], previous_positions=[pos()]))
    assert [d.day for d in line.days] == [SAT, SUN, MON]
    assert [d.rate for d in line.days] == pytest.approx([0.11, 0.11, 0.13])
    assert line.interest == pytest.approx(-1e6 * (0.11 + 0.11 + 0.13) / 365)


def test_rule_version_valid_from_mid_period():
    """A new spread from Sunday supersedes the rule with the same selectors from that day."""
    rules = FundingRules(RULES.rates + (FundingRule(ccy="RUB", book_type="TRADING", index="IDX_K", spread_bp=50,
                                                    valid_from=SUN),))
    line = only(run(WEEKEND, rules, positions=[pos()], previous_positions=[pos()]))
    assert [d.rate for d in line.days] == pytest.approx([0.11, 0.105, 0.125])
    assert line.rate_expr == "IDX_K + 50 bp"


def test_dirty_base_and_short_position():
    policy = replace(POLICY, base_by_book_type={**POLICY.base_by_book_type, "TRADING": FundingBase.MV_DIRTY})
    long_ = only(run(policy=policy, positions=[pos(accrued=3e4)], previous_positions=[pos(accrued=3e4)]))
    assert long_.days[0].body == -1.03e6 and long_.base is FundingBase.MV_DIRTY
    short = only(run(positions=[pos(mv=-5e5)], previous_positions=[pos(mv=-5e5)]))
    assert short.interest > 0


def test_as_of_base_uses_the_as_of_mv():
    policy = replace(POLICY, mv_base_date=BaseDate.AS_OF)
    line = only(run(policy=policy, positions=[pos(mv=2e6)], previous_positions=[pos(mv=1e6)]))
    assert line.days[0].body == -2e6


# ---------------------------------------------------------------------------------------------- new / closed
@pytest.mark.parametrize("mode, days", [(NewPosition.NEXT_PERIOD, None), (NewPosition.LAST_DAY, [MON]),
                                        (NewPosition.FULL_PERIOD, [SAT, SUN, MON])])
def test_new_position(mode, days):
    r = run(WEEKEND, policy=replace(POLICY, new_positions=mode), positions=[pos("NEW", 3e5)], previous_positions=[])
    if days is None:
        assert not r.lines and "1 new position(s) funded from the next period" in r.notes[0]
    else:
        line = only(r)
        assert [d.day for d in line.days] == days and line.days[0].body == -3e5


def test_no_previous_snapshot_treats_everything_as_new():
    r = run(positions=[pos()], previous_positions=None)
    assert not r.lines and r.notes[0].startswith("no previous positions snapshot")


@pytest.mark.parametrize("mode", list(ClosedPosition))
def test_closed_position(mode):
    r = run(WEEKEND, policy=replace(POLICY, closed_positions=mode), positions=[], previous_positions=[pos("OLD")])
    if mode is ClosedPosition.NOT_FUNDED:
        assert not r.lines
    else:
        assert len(only(r).days) == 3 and only(r).days[0].body == -1e6


def test_reduced_position_follows_the_base_date():
    prev, cur = [pos(mv=1e6)], [pos(mv=4e5)]
    assert only(run(positions=cur, previous_positions=prev)).days[0].body == -1e6
    as_of = replace(POLICY, mv_base_date=BaseDate.AS_OF)
    assert only(run(policy=as_of, positions=cur, previous_positions=prev)).days[0].body == -4e5


# ---------------------------------------------------------------------------------------------- currencies
@pytest.mark.parametrize("fx_date, fx", [(FxDate.AS_OF, [82.0] * 3), (FxDate.PREVIOUS, [80.0] * 3),
                                         (FxDate.EACH_DAY, [80.0, 80.0, 82.0])])
def test_foreign_bond_interest_in_reporting_currency(fx_date, fx):
    usd = pos("U1", 1e4, ccy="USD")
    line = only(run(WEEKEND, policy=replace(POLICY, fx_date=fx_date), positions=[usd], previous_positions=[usd]))
    assert line.ccy == "USD" and line.rate_expr == "4.00% fixed"
    assert line.interest == pytest.approx(-1e4 * 0.04 * 3 / 365)
    assert [d.fx for d in line.days] == fx
    assert line.interest_reporting == pytest.approx(sum(-1e4 * 0.04 / 365 * f for f in fx))


def test_multi_currency_book_totals():
    ps = [pos("R1", 1e6), pos("U1", 1e4, ccy="USD"), pos("R2", 5e5, book="B2", entity="E2")]
    r = run(positions=ps, previous_positions=ps)
    rub, usd = -1e6 * 0.13 / 365, -1e4 * 0.04 / 365 * 81.0   # FX in force on the as-of date
    assert r.by_book() == {"B1": pytest.approx(rub + usd), "B2": pytest.approx(-5e5 * 0.13 / 365)}
    assert set(total_by([r], "book_ccy")) == {("B1", "RUB"), ("B1", "USD"), ("B2", "RUB")}
    assert total_by([r], "book_ccy", reporting=False)[("B1", "USD")] == pytest.approx(-1e4 * 0.04 / 365)


def test_out_of_currency_bond():
    usd = pos("U1", 1e4, ccy="USD", settlement_ccy="RUB")
    assert only(run(positions=[usd], previous_positions=[usd])).ccy == "USD"   # security_ccy (provisional)
    policy = replace(POLICY, out_of_currency=OutOfCurrency.SETTLEMENT_CCY)
    line = only(run(policy=policy, positions=[usd], previous_positions=[usd]))
    assert line.ccy == "RUB" and line.days[0].body == pytest.approx(-1e4 * 82.0)   # MV at FX of MON (prev)
    assert line.rate_expr == "IDX_K + 100 bp" and "settlement currency" in line.note


# ---------------------------------------------------------------------------------------------- problems
def test_missing_inputs_are_problems_not_zero():
    no_fix = FundingRules((FundingRule(ccy="RUB", book_type="TRADING", index="IDX_MISSING"),))
    chf = pos("C1", 1e4, ccy="CHF")
    fx_less = FundingRules(RULES.rates + (FundingRule(ccy="CHF", fixed_rate=0.01),))
    cases = [(no_fix, [pos()], "no IDX_MISSING value"), (RULES, [chf], "no funding rule for TRADING B1 CHF"),
             (fx_less, [chf], "no CHF FX rate"), (RULES, [pos(book_type="ODD")], "no funding base")]
    for rules, ps, reason in cases:
        r = run(rules=rules, positions=ps, previous_positions=ps)
        assert not r.lines and reason in r.problems[0].reason, r.problems


def test_stale_fixing_bound():
    stale = FixingTable({"IDX_K": {date(2026, 9, 1): 0.10}}, max_age_days=30)
    r = compute_funding(ONE_DAY, RULES, stale, FX, positions=[pos()], previous_positions=[pos()], policy=POLICY)
    assert not r.lines and "no IDX_K value" in r.problems[0].reason


def test_duplicate_ids_raise():
    with pytest.raises(ValueError, match="duplicate position"):
        run(positions=[pos(), pos()], previous_positions=[])


def test_validation():
    with pytest.raises(ValueError, match="exactly one"):
        FundingRule(index="X", fixed_rate=0.1)
    with pytest.raises(ValueError, match="exactly one"):
        FundingRule()
    with pytest.raises(ValueError, match="no spread"):
        FundingRule(fixed_rate=0.1, spread_bp=10)
    with pytest.raises(ValueError, match="empty"):
        AccrualPeriod(MON, MON)
    with pytest.raises(ValueError):
        RepoPolicy(contract_rate_from_term_days=-1)


# ---------------------------------------------------------------------------------------------- rule resolution
def test_most_specific_rule_wins():
    rules = FundingRules((
        FundingRule(book_type="TRADING", index="IDX_K"),
        FundingRule(ccy="RUB", book_type="TRADING", index="IDX_K", spread_bp=100),
        FundingRule(ccy="RUB", book_id="B2", fixed_rate=0.07),
        FundingRule(ccy="RUB", entity="E3", fixed_rate=0.08),
        FundingRule(instrument_id="ISIN1", fixed_rate=0.09),
        FundingRule(ccy="RUB", entity="E3", fixed_rate=0.05),  # same weight as the E3 rule above: first wins
    ))
    def rate(**kw):
        return rules.resolve(TUE, **({"ccy": "RUB", "book_type": "TRADING", "entity": "E1", "book_id": "B1"} | kw))

    assert rate().spread_bp == 100
    assert rate(ccy="CNY").index == "IDX_K" and rate(ccy="CNY").spread_bp == 0
    assert rate(book_id="B2").fixed_rate == 0.07
    assert rate(entity="E3").fixed_rate == 0.08
    assert rate(book_id="B2", entity="E3", instrument_id="ISIN1").fixed_rate == 0.09
    assert rules.resolve(TUE, ccy="RUB", book_type="DERIVS", entity="E1", book_id="B1") is None


def test_valid_to_ends_a_rule():
    rules = FundingRules(RULES.rates + (FundingRule(ccy="RUB", book_id="B1", fixed_rate=0.2, valid_to=SUN),))
    line = only(run(WEEKEND, rules, positions=[pos()], previous_positions=[pos()]))
    assert [d.rate for d in line.days] == pytest.approx([0.2, 0.11, 0.13])


def test_extra_spread_by_bond_type():
    rules = replace(RULES, extra_spreads=(ExtraSpread("SUB", 25), ExtraSpread("SUB", 40, ccy="RUB", valid_from=MON)))
    p = pos(bond_type="SUB")
    line = only(run(WEEKEND, rules, positions=[p], previous_positions=[p]))
    assert [d.rate for d in line.days] == pytest.approx([0.1125, 0.1125, 0.134])
    assert line.rate_expr == "IDX_K + 100 bp + extra 40 bp"


def test_day_basis_policy_and_rule_override():
    policy = replace(POLICY, day_count=DayCountPolicy(DayBasis.ACT_365, (
        DayBasisRule(DayBasis.ACT_360, ccy="USD"), DayBasisRule(DayBasis.ACT_365, LineKind.POS, "USD"))))
    assert policy.day_count.basis(LineKind.CASH, "USD") is DayBasis.ACT_360
    assert policy.day_count.basis(LineKind.POS, "USD") is DayBasis.ACT_365
    assert policy.day_count.basis(LineKind.CASH, "RUB") is DayBasis.ACT_365
    cash = [CashBalance("D1", "DERIVS", "E1", "USD", 1e4)]
    line = only(run(policy=policy, cash=cash, previous_cash=cash))
    assert line.interest == pytest.approx(1e4 * 0.03 / 360)
    rules = FundingRules((FundingRule(ccy="USD", book_type="DERIVS", fixed_rate=0.03, day_basis=DayBasis.ACT_365),))
    assert only(run(rules=rules, policy=policy, cash=cash, previous_cash=cash)).days[0].basis is DayBasis.ACT_365


# ---------------------------------------------------------------------------------------------- cash, entity cash, futures
def test_derivs_cash_balances():
    prev = [CashBalance("D1", "DERIVS", "E1", "RUB", 2e6), CashBalance("D1", "DERIVS", "E1", "USD", -1e4),
            CashBalance("B1", "TRADING", "E1", "RUB", 7e6)]   # cash of a bond book is not funded
    cur = [CashBalance("D1", "DERIVS", "E1", "RUB", 3e6)]
    r = run(WEEKEND, cash=cur, previous_cash=prev)
    rub = next(x for x in r.lines if x.ccy == "RUB")
    assert rub.kind is LineKind.CASH and rub.days[0].body == 2e6
    assert rub.interest == pytest.approx(2e6 * (0.09 + 0.09 + 0.095) / 365)   # O/N: Friday's fixing holds
    usd = next(x for x in r.lines if x.ccy == "USD")
    assert usd.interest_reporting == pytest.approx(-1e4 * 0.03 * 3 / 365 * 82.0)
    assert len(r.lines) == 2
    as_of = only(run(WEEKEND, policy=replace(POLICY, cash_base_date=BaseDate.AS_OF), cash=cur, previous_cash=prev))
    assert as_of.days[0].body == 3e6
    missing = run(cash=cur, previous_cash=None)
    assert not missing.lines and "no previous-day cash snapshot" in missing.problems[0].reason


def test_other_entity_funds_the_whole_cash_balance():
    entities = {"E1": "main", "OTH1": EntityType.OTHER}
    cash = [CashBalance("X1", "DERIVS", "OTH1", "RUB", -1e6), CashBalance("X2", "TRADING", "OTH1", "RUB", 5e5)]
    ps = [pos("CP", 1e6, entity="OTH1"), pos("P1")]
    repo = RepoDeal("R1", "X1", "DERIVS", "OTH1", "RUB", 1e6, FRI, TUE)
    r = run(cash=cash, previous_cash=cash, positions=ps, previous_positions=ps, repos=[repo], entities=entities)
    ent = [x for x in r.lines if x.kind is LineKind.ENTITY_CASH]
    assert [x.days[0].body for x in ent] == [-1e6, 5e5]
    assert all(x.rate_expr == "IDX_K + 100 bp" for x in ent)   # the TRADING rules (entity_cash_rate_book_type)
    own = run(cash=cash, previous_cash=cash, entities=entities,
              policy=replace(POLICY, entity_cash_rate_book_type=None))   # default: each balance's own rules
    assert [x.rate_expr for x in own.lines] == ["IDX_ON", "IDX_K + 100 bp"]
    assert [x.ref for x in r.lines if x.kind is LineKind.POS] == ["P1"]
    assert not any(x.kind is LineKind.REPO for x in r.lines)
    unknown = run(positions=[pos(entity="E9")], previous_positions=[pos(entity="E9")], entities=entities)
    assert not unknown.lines and unknown.problems[0].reason == "unknown entity 'E9'"


def test_futures_book_is_not_funded():
    p = pos(book_type="FUTURES")
    r = run(positions=[p], previous_positions=[p], cash=[CashBalance("F", "FUTURES", "E1", "RUB", 1e6)],
            previous_cash=[CashBalance("F", "FUTURES", "E1", "RUB", 1e6)])
    assert not r.lines and not r.problems


# ---------------------------------------------------------------------------------------------- HTM
def lot(**kw):
    base = {"lot_id": "L1", "position_id": "H1", "book_id": "H", "entity": "E1", "ccy": "RUB",
            "purchase_date": date(2026, 1, 10), "clean_cost": 9.5e5, "contract_rate": 0.08,
            "purchase_accrued": 2e4, "accrued_until": SUN}
    return HtmLot(**(base | kw))


def test_htm_cash_spent_accrued_until_coupon():
    h = pos("H1", 9.9e5, book="H", book_type="HTM")
    r = run(WEEKEND, positions=[h], previous_positions=[h], htm_lots=[lot()])
    line = only(r)
    assert line.kind is LineKind.HTM and line.position_id == "H1" and line.rate_expr == "8.00% contract"
    assert [d.body for d in line.days] == [-9.7e5, -9.7e5, -9.5e5]
    assert line.interest == pytest.approx(-(9.7e5 * 2 + 9.5e5) * 0.08 / 365)
    clean = only(run(WEEKEND, policy=replace(POLICY, htm_base=HtmBase.CLEAN_COST), htm_lots=[lot()]))
    assert [d.body for d in clean.days] == [-9.5e5] * 3


def test_htm_purchase_and_maturity_inside_the_period():
    line = only(run(WEEKEND, htm_lots=[lot(purchase_date=SAT, maturity=MON)]))
    assert [d.day for d in line.days] == [SUN, MON]
    assert not run(WEEKEND, htm_lots=[lot(purchase_date=MON)]).lines


def test_htm_problems():
    assert run(htm_lots=[lot(contract_rate=None)]).problems[0].reason == "no contract rate"
    assert "coupon date" in run(htm_lots=[lot(accrued_until=None)]).problems[0].reason
    h = pos("H2", 1e6, book="H", book_type="HTM")
    r = run(positions=[h], previous_positions=[h], htm_lots=[lot()])
    assert r.problems[0].ref == "H2" and "without an HTM purchase lot" in r.problems[0].reason


# ---------------------------------------------------------------------------------------------- REPO
RULES_REPO = FundingRules(RULES.rates + (FundingRule(instrument_id="ISIN9", fixed_rate=0.15),))


def test_repo_at_the_security_rate():
    deal = RepoDeal("R1", "RP", "DERIVS", "E1", "RUB", 1e6, FRI, SUN, contract_rate=0.2, collateral_id="ISIN9")
    line = only(run(WEEKEND, RULES_REPO, repos=[deal]))
    assert line.kind is LineKind.REPO and [d.day for d in line.days] == [SAT, SUN]
    assert line.rate_expr == "15.00% fixed" and line.note == "rate=security"
    assert line.interest == pytest.approx(1e6 * 0.15 * 2 / 365)
    rev = replace(deal, deal_id="R2", cash_amount=-1e6, collateral_id=None)
    assert only(run(WEEKEND, RULES_REPO, repos=[rev])).days[0].rate == pytest.approx(0.11)   # TRADING RUB rule


def test_repo_contract_rate_threshold():
    short = RepoDeal("S", "RP", "DERIVS", "E1", "RUB", 1e6, FRI, date(2026, 10, 23), contract_rate=0.2)
    long_ = replace(short, deal_id="L", maturity=date(2027, 1, 15))
    policy = replace(POLICY, repo=RepoPolicy(RepoRate.SECURITY, contract_rate_from_term_days=30))
    r = run(WEEKEND, RULES_REPO, policy=policy, repos=[short, long_])
    assert {x.ref: x.days[0].rate for x in r.lines} == pytest.approx({"S": 0.11, "L": 0.2})
    assert RepoPolicy(RepoRate.CONTRACT).rate_for(None) is RepoRate.CONTRACT
    no_rate = replace(long_, contract_rate=None)
    assert run(WEEKEND, RULES_REPO, policy=policy, repos=[no_rate]).problems[0].reason == "no contract rate"


# ---------------------------------------------------------------------------------------------- accrual over runs
def test_accrual_over_consecutive_runs_counts_each_day_once():
    p = [pos()]
    r1 = run(WEEKEND, positions=p, previous_positions=p)
    r2 = run(ONE_DAY, positions=p, previous_positions=p)
    total = total_by([r2, r1], "position")["P1"]
    assert total == pytest.approx(-1e6 * (0.11 + 0.11 + 0.13 + 0.13) / 365)
    assert total_by([r1, r2], start=SUN, end=MON)["B1"] == pytest.approx(-1e6 * 0.13 / 365)
    assert r1.on(SUN) == {"POS|P1": pytest.approx(-1e6 * 0.11 / 365)}
    with pytest.raises(ValueError, match="overlap"):
        total_by([r1, run(AccrualPeriod(SAT, TUE), positions=p, previous_positions=p)])


# ---------------------------------------------------------------------------------------------- FX liability
def test_funding_liabilities_and_fx_revaluation():
    ps = [pos("U1", 1e4, ccy="USD", accrued=200.0), pos("R1"), pos("D", 5.0, book_type="DERIVS"),
          pos("O", 1e4, ccy="USD", settlement_ccy="RUB")]
    liab, probs = funding_liabilities(ps, policy=POLICY)
    assert [(x.position_id, x.ccy, x.amount) for x in liab] == [("U1", "USD", -1e4), ("R1", "RUB", -1e6),
                                                                 ("O", "USD", -1e4)]
    assert not probs
    pnl = liability_fx_pnl(liab, FX, FRI, MON)
    assert pnl == {"U1": pytest.approx(-1e4 * 2.0), "R1": 0.0, "O": pytest.approx(-2e4)}
    settle = replace(POLICY, out_of_currency=OutOfCurrency.SETTLEMENT_CCY)
    liab2, _ = funding_liabilities(ps, policy=settle, fx=FX, on=MON)
    assert liab2[-1].ccy == "RUB" and liab2[-1].amount == pytest.approx(-8.2e5)
    _, probs2 = funding_liabilities(ps, policy=settle)
    assert probs2[0].ref == "O"


# ---------------------------------------------------------------------------------------------- write-off
def test_write_off_schedules():
    assert write_off_dates(date(2026, 9, 15), date(2026, 12, 31), Periodicity.MONTHLY) == (
        date(2026, 9, 30), date(2026, 10, 31), date(2026, 11, 30), date(2026, 12, 31))
    assert write_off_dates(date(2026, 1, 1), date(2026, 12, 31), Periodicity.QUARTERLY,
                           WriteOffDate.LAST_BUSINESS_DAY) == (
        date(2026, 3, 31), date(2026, 6, 30), date(2026, 9, 30), date(2026, 12, 31))
    # 2026-10-31 is a Saturday: the last business day is Friday 30th, inside a period ending on the 30th
    assert write_off_dates(date(2026, 10, 1), date(2026, 10, 30), Periodicity.MONTHLY,
                           WriteOffDate.LAST_BUSINESS_DAY) == (date(2026, 10, 30),)
    assert write_off_dates(FRI, MON, Periodicity.DAILY, WriteOffDate.LAST_BUSINESS_DAY) == (MON,)
    assert write_off_dates(FRI, MON, Periodicity.NONE) == ()
    assert last_write_off(TUE, Periodicity.QUARTERLY) == date(2026, 9, 30)
    assert last_write_off(TUE, Periodicity.NONE) is None
    w = WriteOffPolicy(by_book_type={"DERIVS": Periodicity.MONTHLY}, by_entity={"OTH2": Periodicity.QUARTERLY},
                       other_entity=Periodicity.DAILY)
    assert w.periodicity("DERIVS", "E1") is Periodicity.MONTHLY
    assert w.periodicity("TRADING", "OTH1", EntityType.OTHER) is Periodicity.DAILY
    assert w.periodicity("TRADING", "OTH2", EntityType.OTHER) is Periodicity.QUARTERLY
    unset = WriteOffPolicy()   # nothing set by default: "not known"
    assert unset.periodicity("DERIVS", "E1") is None and unset.periodicity("TRADING", "O", EntityType.OTHER) is None


# ---------------------------------------------------------------------------------------------- providers
def test_rate_index_providers():
    assert RATES.rate("IDX_ON", SUN) == 0.09 and RATES.rate("IDX_ON", date(2026, 10, 1)) is None
    assert RATES.rate("NOPE", MON) is None and RATES.indices == ("IDX_K", "IDX_ON")
    rows = [Fixing("KEY_RATE", date(2026, 7, 28), 0.18, "cbr", None, None),
            Fixing("RUONIA", FRI, 0.171, "cbr", None, None)]
    t = FixingTable.from_fixings(rows, max_age_days=5)
    assert t.rate("RUONIA", MON) == 0.171 and t.rate("KEY_RATE", MON) is None
    assert FX.rate("RUB", MON) == 1.0 and FX.rate("CNY", TUE) == 11.2 and FX.rate("EUR", TUE) is None
    with pytest.raises(ValueError, match="duplicate"):
        FixingTable({"X": [(MON, 0.1), (MON, 0.2)]})


# ---------------------------------------------------------------------------------------------- policy
def test_prototype_parity_pos_and_htm():
    """``tdm/writer.py derive_funding`` POS: amount = -(mv - notional x accrued / 100) at the as-of date,
    carry = amount x conv x sum(rate %) / 100 / 365; HTM: -notional x purchase_price / 100 at the contract rate."""
    notional, mv_dirty, accrued_pct, conv = 1e4, 1.02e4, 1.5, 82.0
    usd = pos("U1", mv_dirty - notional * accrued_pct / 100, ccy="USD", accrued=notional * accrued_pct / 100)
    prev = replace(usd, mv_clean=1)
    htm = lot(clean_cost=1e6 * 97.0 / 100)
    r = run(WEEKEND, policy=PROTOTYPE_POLICY, positions=[usd, pos("NEW")], previous_positions=[prev, pos("GONE")],
            htm_lots=[htm])
    lines = {x.ref: x for x in r.lines}
    assert set(lines) == {"U1", "NEW", "L1"}
    amount = -(mv_dirty - notional * accrued_pct / 100)
    assert lines["U1"].interest_reporting == pytest.approx(amount * conv * (4.0 * 3) / 100 / 365)
    assert lines["L1"].interest == pytest.approx(-1e6 * 97.0 / 100 * (8.0 * 3) / 100 / 365)
    assert PROTOTYPE_POLICY.day_count.basis(LineKind.CASH, "USD") is DayBasis.ACT_360


def test_open_decisions_name_policy_fields():
    p = PROVISIONAL_POLICY
    for d in OPEN_DECISIONS:
        obj = p
        for part in d.field.split("[")[0].split("."):
            obj = getattr(obj, part)
        assert d.ref and d.provisional and d.alternatives
    assert "TRADING" not in p.base_by_book_type and p.htm_base is None and p.entity_cash_rate_book_type is None
    assert p.write_off.by_book_type == {} and p.write_off.other_entity is None
    assert (p.mv_base_date, p.new_positions, p.closed_positions) == (
        BaseDate.PREVIOUS, NewPosition.NEXT_PERIOD, ClosedPosition.FULL_PERIOD)
    assert json.loads(json.dumps(p.lineage()))["repo"]["rate"] == "contract"


def test_unset_conventions_are_problems():
    """With the engine's defaults a market-value book has no base and an HTM lot no HTM base: problems, no line."""
    r = run(policy=PROVISIONAL_POLICY, positions=[pos()], previous_positions=[pos()], htm_lots=[lot()])
    assert not r.lines
    assert [x.reason for x in r.problems] == ["no funding base for book type 'TRADING'",
                                              "no HTM base configured (htm_base)"]
    deal = RepoDeal("R1", "RP", "DERIVS", "E1", "RUB", 1e6, FRI, TUE, contract_rate=0.2)
    assert only(run(policy=PROVISIONAL_POLICY, repos=[deal])).note == "rate=contract"   # the deal's own rate
