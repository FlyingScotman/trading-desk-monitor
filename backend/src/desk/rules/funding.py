"""Funding engine (FND-5, package P2): the daily funding charge of positions, HTM purchase lots, cash
balances and REPO deals, and its accrual over a period, per position, book and entity.

Pure and firm-agnostic. The organisation's rules arrive as data (``FundingRules``: the private adapter's
``reference()["funding_rules"]`` once A7 lands); index fixings and FX rates through injected providers
(``rate_index``); every open owner decision is a ``FundingPolicy`` field, listed with its decision id in
``OPEN_DECISIONS``. A field's default is either a neutral generic choice or unset, and an unset convention the
engine needs is a ``FundingProblem``, never a guess: the funding base of a market-value book (clean or dirty),
the HTM base and the PnL write-off periodicity have no default and come from the adapter's settings
(``reference.policy_from_reference``) or from the decision. Not wired into the run yet (wave B1: ``pnl/flash.py``,
``publish/tdm_run.py``, ``api/compat.py``). ``PROTOTYPE_POLICY`` reproduces ``tdm/writer.py``
``derive_funding()`` for POS and HTM.

**Accrual.** The period is (previous business day, as-of date]: every calendar day of it accrues (on a
Monday: Saturday, Sunday, Monday). Day d accrues ``body(d) x rate(d) / basis`` in the line currency,
simple interest (interest is not funded, coupons do not compound). ``rate(d)`` comes from the rule valid on
d (the latest ``valid_from`` on or before d) with the index value in force on d plus the spread, or the
fixed rate, plus any extra spread of the bond type. The interest is converted to the reporting currency
at the FX rate of ``FundingPolicy.fx_date``.

**Sign.** The body is the cash on the desk's side of the funding: a long bond is funded by borrowing,
body = -base and the charge is negative (a cost); a positive cash balance, or the cash a REPO brought in,
earns (body = +cash).

**Lines** (``LineKind``), by the base of the book type (``FundingPolicy.base_by_book_type``):

- ``POS`` — base ``mv_clean`` / ``mv_dirty``: one line per position. Which snapshot gives the MV, and how
  positions opened or closed during the period are funded, is policy (DEC01).
- ``HTM`` — base ``cost``: one line per purchase lot (``HtmLot``) at its contract rate, from the day after
  the purchase to maturity. A position of a ``cost`` book without a lot is a problem.
- ``CASH`` — base ``cash``: one line per book x currency balance, at the rules of the book's type.
- ``ENTITY_CASH`` — an entity of type ``other`` is funded on its whole cash balance, every book, at the rules
  that apply to the balance (or those of ``FundingPolicy.entity_cash_rate_book_type``); its positions and
  REPO deals get no line of their own.
- ``REPO`` — the deal's cash leg, from the day after the start to maturity, at the contract rate or the
  collateral's rate (``RepoPolicy``).
- base ``none`` (a futures book): not funded.

A missing input (rule, fixing, FX rate, contract rate) is a ``FundingProblem`` and the line is left out,
never a 0.

**Also here:** ``funding_liabilities`` (the currency funding liability of the bond books, which offsets the
FX delta of a foreign-currency bond, OCP-4), ``liability_fx_pnl``, ``write_off_dates`` / ``last_write_off``
(PnL write-off schedule) and ``total_by`` (accrual over several runs, counted once).
"""

from __future__ import annotations

import calendar
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Any

from .rate_index import FxProvider, RateIndexProvider

BP = 1e-4


class FundingBase(StrEnum):
    """What a book type is funded on (the adapter's ``funding.json`` base, A7)."""

    MV_CLEAN = "mv_clean"
    MV_DIRTY = "mv_dirty"
    CASH = "cash"
    COST = "cost"
    NONE = "none"


class LineKind(StrEnum):
    POS = "POS"
    HTM = "HTM"
    CASH = "CASH"
    ENTITY_CASH = "ENTITY_CASH"
    REPO = "REPO"


class EntityType(StrEnum):
    MAIN = "main"
    OTHER = "other"


class DayBasis(StrEnum):
    ACT_365 = "ACT/365"
    ACT_360 = "ACT/360"

    @property
    def denominator(self) -> int:
        return 360 if self is DayBasis.ACT_360 else 365


class BaseDate(StrEnum):
    """Which snapshot gives the base of the whole period."""

    PREVIOUS = "previous_business_day"
    AS_OF = "as_of"


class NewPosition(StrEnum):
    """A position in the as-of snapshot and not in the previous one."""

    NEXT_PERIOD = "next_period"  # no charge now; funded from the next period on (it is then previous)
    LAST_DAY = "last_day"  # one day at the as-of MV (funded from the trade date)
    FULL_PERIOD = "full_period"  # every day of the period at the as-of MV (the tdm prototype)


class ClosedPosition(StrEnum):
    """A position in the previous snapshot and not in the as-of one."""

    FULL_PERIOD = "full_period"  # every day of the period at the previous MV
    NOT_FUNDED = "not_funded"  # no charge (the tdm prototype)


class FxDate(StrEnum):
    """Date of the FX rate that converts the interest to the reporting currency."""

    AS_OF = "as_of"
    PREVIOUS = "previous_business_day"
    EACH_DAY = "each_day"


class HtmBase(StrEnum):
    """What of an HTM purchase is funded (OQ-02); no default."""

    CASH_SPENT = "cash_spent"  # clean cost + the accrued paid at purchase, until the coupon date
    CLEAN_COST = "clean_cost"  # clean purchase cost only


class RepoRate(StrEnum):
    SECURITY = "security"  # the funding rate of the collateral, as a bond of a bond book
    CONTRACT = "contract"  # the deal's own rate


class OutOfCurrency(StrEnum):
    """Funding currency of a bond settled in another currency than its own."""

    SECURITY_CCY = "security_ccy"
    SETTLEMENT_CCY = "settlement_ccy"


class Periodicity(StrEnum):
    DAILY = "daily"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    NONE = "none"


class WriteOffDate(StrEnum):
    LAST_CALENDAR_DAY = "last_calendar_day"
    LAST_BUSINESS_DAY = "last_business_day"


# ---------------------------------------------------------------------------------------------- policy
@dataclass(frozen=True)
class DayBasisRule:
    """Day basis of the lines of ``kind`` in ``ccy`` (None: any)."""

    basis: DayBasis
    kind: LineKind | None = None
    ccy: str | None = None


@dataclass(frozen=True)
class DayCountPolicy:
    """Basis of a line when its rule names none: the most specific ``DayBasisRule`` (kind and currency >
    currency > kind > neither), else ``default``."""

    default: DayBasis = DayBasis.ACT_365
    rules: tuple[DayBasisRule, ...] = ()

    def basis(self, kind: LineKind, ccy: str) -> DayBasis:
        best, weight = self.default, -1
        for r in self.rules:
            if (r.kind is not None and r.kind != kind) or (r.ccy is not None and r.ccy != ccy):
                continue
            w = 2 * (r.ccy is not None) + (r.kind is not None)
            if w > weight:
                best, weight = r.basis, w
        return best


@dataclass(frozen=True)
class RepoPolicy:
    rate: RepoRate = RepoRate.CONTRACT
    contract_rate_from_term_days: int | None = None
    """A deal of at least this term (maturity - start, days) is funded at its contract rate; None: none is."""
    security_rate_book_type: str = "TRADING"
    """The book type whose rules give "the security's rate"."""

    def __post_init__(self) -> None:
        if self.contract_rate_from_term_days is not None and self.contract_rate_from_term_days < 0:
            raise ValueError("contract_rate_from_term_days must be >= 0")

    def rate_for(self, term_days: int | None) -> RepoRate:
        t = self.contract_rate_from_term_days
        if t is not None and term_days is not None and term_days >= t:
            return RepoRate.CONTRACT
        return self.rate


@dataclass(frozen=True)
class WriteOffPolicy:
    """When a book's PnL is written off (swept out of its cash): an entity's own periodicity (the adapter's
    ``entities`` ``pnl_sweep``), else ``other_entity`` for an entity of type other, else the book type's. Nothing
    is set by default: an unset periodicity is None, "not known", as an absent ``pnl_sweep`` in the contract."""

    by_book_type: Mapping[str, Periodicity] = field(default_factory=dict)
    by_entity: Mapping[str, Periodicity] = field(default_factory=dict)
    other_entity: Periodicity | None = None
    date_rule: WriteOffDate = WriteOffDate.LAST_CALENDAR_DAY

    def periodicity(self, book_type: str, entity: str,
                    entity_type: EntityType = EntityType.MAIN) -> Periodicity | None:
        if entity in self.by_entity:
            return self.by_entity[entity]
        if entity_type is EntityType.OTHER:
            return self.other_entity
        return self.by_book_type.get(book_type)


def _default_bases() -> dict[str, FundingBase]:
    """The regimes the contract's ``book_type`` names (08 §10 ``books``). ``TRADING`` is funded on its market
    value, clean or dirty, and has no default: the adapter's ``funding_rules`` ``basis`` gives it."""
    return {"HTM": FundingBase.COST, "DERIVS": FundingBase.CASH, "FUTURES": FundingBase.NONE}


@dataclass(frozen=True)
class FundingPolicy:
    """Every convention the engine applies. A default is **provisional** (``OPEN_DECISIONS``): a neutral generic
    choice standing for an owner decision not taken yet, or unset (None, no entry) where any choice would be a
    firm's own; the adapter's settings or the decision replace it."""

    base_by_book_type: Mapping[str, FundingBase] = field(default_factory=_default_bases)
    mv_base_date: BaseDate = BaseDate.PREVIOUS
    cash_base_date: BaseDate = BaseDate.PREVIOUS
    new_positions: NewPosition = NewPosition.NEXT_PERIOD
    closed_positions: ClosedPosition = ClosedPosition.FULL_PERIOD
    day_count: DayCountPolicy = field(default_factory=DayCountPolicy)
    fx_date: FxDate = FxDate.AS_OF
    reporting_ccy: str = "RUB"
    htm_base: HtmBase | None = None
    repo: RepoPolicy = field(default_factory=RepoPolicy)
    out_of_currency: OutOfCurrency = OutOfCurrency.SECURITY_CCY
    entity_cash_rate_book_type: str | None = None
    write_off: WriteOffPolicy = field(default_factory=WriteOffPolicy)

    def lineage(self) -> dict[str, Any]:
        """The policy as plain JSON values, for the run record."""
        return asdict(self)


@dataclass(frozen=True)
class OpenDecision:
    field: str
    ref: str
    question: str
    provisional: str
    alternatives: tuple[str, ...]


OPEN_DECISIONS: tuple[OpenDecision, ...] = (
    OpenDecision("base_by_book_type['TRADING']", "DEC01, FND-6", "Market-value book base: clean or dirty MV",
                 "none: a problem until the adapter's funding_rules give the basis", ("mv_clean", "mv_dirty")),
    OpenDecision("base_by_book_type", "OQ-01, FND-2, FND-3", "Base of each book type (the adapter's funding_rules)",
                 "the regimes the contract's book_type names: HTM cost, DERIVS cash, FUTURES none",
                 ("any FundingBase per book type",)),
    OpenDecision("mv_base_date", "DEC01, FND-7", "MV of the previous business day or of the as-of date",
                 "previous_business_day (the position held overnight; the tdm prototype used as_of)", ("as_of",)),
    OpenDecision("new_positions", "DEC01, FND-7", "Positions opened during the period",
                 "next_period (consistent with the previous-day base)", ("last_day", "full_period")),
    OpenDecision("closed_positions", "DEC01, FND-7", "Positions closed or sold during the period",
                 "full_period (the previous-day MV until the as-of date)", ("not_funded",)),
    OpenDecision("cash_base_date", "DEC01", "Cash balance of the previous business day or the as-of date "
                 "(intraday cash)", "previous_business_day", ("as_of",)),
    OpenDecision("fx_date", "DEC01", "Date of the (CBR) rate converting interest to the reporting currency",
                 "as_of (the rate in force on the as-of date, as the tdm prototype)",
                 ("previous_business_day", "each_day")),
    OpenDecision("day_count", "DEC02, FND-8", "Day basis per line kind and currency",
                 "ACT/365 for every line", ("ACT/360 for chosen kinds x currencies",)),
    OpenDecision("htm_base", "OQ-02, FND-11", "HTM base: cash spent (purchase accrued funded until the coupon) "
                 "or clean purchase cost", "none: an HTM lot is a problem until it is set",
                 ("cash_spent", "clean_cost")),
    OpenDecision("repo.rate", "DEC03, FND-12", "REPO cash leg at the contract rate or the collateral's rate",
                 "contract (the deal's own rate)", ("security",)),
    OpenDecision("repo.contract_rate_from_term_days", "DEC03, FND-12",
                 "Term from which a REPO is funded at its contract rate (with rate security)", "None (no threshold)",
                 ("a number of days",)),
    OpenDecision("repo.security_rate_book_type", "DEC03", "Book type whose rules give the collateral's rate",
                 "TRADING (the market-value book type)", ("another book type",)),
    OpenDecision("out_of_currency", "FND-15", "Funding currency of a bond bought in another currency",
                 "security_ccy (the position's own currency)", ("settlement_ccy",)),
    OpenDecision("entity_cash_rate_book_type", "FND-13", "Rates of the cash of an entity funded on its cash balance",
                 "None (the rules that apply to the balance: its entity, book, currency, book type)",
                 ("a book type whose rules apply",)),
    OpenDecision("write_off", "FND-13, FND-14", "PnL write-off periodicity and date",
                 "none set (the adapter's entities pnl_sweep per entity); last calendar day",
                 ("per book type periodicity", "other_entity periodicity", "last_business_day")),
)

PROVISIONAL_POLICY = FundingPolicy()

PROTOTYPE_POLICY = FundingPolicy(
    base_by_book_type={**_default_bases(), "TRADING": FundingBase.MV_CLEAN}, mv_base_date=BaseDate.AS_OF, cash_base_date=BaseDate.AS_OF, new_positions=NewPosition.FULL_PERIOD,
    closed_positions=ClosedPosition.NOT_FUNDED, htm_base=HtmBase.CLEAN_COST,
    day_count=DayCountPolicy(DayBasis.ACT_365, tuple(DayBasisRule(DayBasis.ACT_360, k, c)
                                                     for k in (LineKind.CASH, LineKind.REPO)
                                                     for c in ("USD", "EUR", "CHF"))))
"""``tdm/writer.py derive_funding()``: as-of clean MV, clean cost for HTM, ACT/360 for USD/EUR/CHF cash and
REPO. For parity checks, not a decision."""


# ---------------------------------------------------------------------------------------------- rules
_WEIGHTS = (("instrument_id", 16), ("book_id", 8), ("entity", 4), ("ccy", 2), ("book_type", 1))


@dataclass(frozen=True)
class FundingRule:
    """One funding rate: ``index`` + ``spread_bp`` or ``fixed_rate`` (decimal), for the positions its
    selectors match (None: any). Valid on [``valid_from``, ``valid_to``); a rule with the same selectors and
    a later ``valid_from`` supersedes it. The most specific valid rule wins (instrument 16 > book 8 >
    entity 4 > currency 2 > book type 1); at equal weight the first in the list."""

    ccy: str | None = None
    book_type: str | None = None
    entity: str | None = None
    book_id: str | None = None
    instrument_id: str | None = None
    index: str | None = None
    spread_bp: float = 0.0
    fixed_rate: float | None = None
    valid_from: date = date.min
    valid_to: date | None = None
    day_basis: DayBasis | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if (self.index is None) == (self.fixed_rate is None):
            raise ValueError("a funding rule has exactly one of index and fixed_rate")
        if self.fixed_rate is not None and self.spread_bp:
            raise ValueError("a fixed funding rate takes no spread")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")

    @property
    def selector(self) -> tuple[str | None, ...]:
        return tuple(getattr(self, k) for k, _ in _WEIGHTS)

    @property
    def weight(self) -> int:
        return sum(w for k, w in _WEIGHTS if getattr(self, k) is not None)

    def valid_on(self, on: date) -> bool:
        return self.valid_from <= on and (self.valid_to is None or on < self.valid_to)

    def matches(self, facts: Mapping[str, str | None]) -> bool:
        return all(getattr(self, k) is None or getattr(self, k) == facts.get(k) for k, _ in _WEIGHTS)

    @property
    def expr(self) -> str:
        """'KEY_RATE + 150 bp', 'RUONIA', '5.00% fixed'."""
        if self.fixed_rate is not None:
            return f"{self.fixed_rate:.2%} fixed"
        if not self.spread_bp:
            return str(self.index)
        return f"{self.index} {'+' if self.spread_bp > 0 else '-'} {abs(self.spread_bp):g} bp"


@dataclass(frozen=True)
class ExtraSpread:
    """An additional spread of a bond type, on top of the resolved rule; ``ccy`` None: any."""

    bond_type: str
    spread_bp: float
    ccy: str | None = None
    valid_from: date = date.min


@dataclass(frozen=True)
class FundingRules:
    rates: tuple[FundingRule, ...] = ()
    extra_spreads: tuple[ExtraSpread, ...] = ()

    def resolve(self, on: date, *, ccy: str, book_type: str, entity: str, book_id: str,
                instrument_id: str | None = None) -> FundingRule | None:
        facts = {"ccy": ccy, "book_type": book_type, "entity": entity, "book_id": book_id,
                 "instrument_id": instrument_id}
        latest: dict[tuple[str | None, ...], tuple[int, FundingRule]] = {}
        for i, r in enumerate(self.rates):
            if not r.valid_on(on) or not r.matches(facts):
                continue
            k = r.selector
            if k not in latest or r.valid_from > latest[k][1].valid_from:
                latest[k] = (i, r)
        if not latest:
            return None
        return min(latest.values(), key=lambda t: (-t[1].weight, t[0]))[1]

    def extra_spread_bp(self, on: date, bond_type: str | None, ccy: str) -> float:
        """The latest valid extra spread of the bond type, the currency's own before an any-currency one."""
        if bond_type is None:
            return 0.0
        best: ExtraSpread | None = None
        for s in self.extra_spreads:
            if s.bond_type != bond_type or s.valid_from > on or (s.ccy is not None and s.ccy != ccy):
                continue
            if best is None or ((s.ccy is not None, s.valid_from) > (best.ccy is not None, best.valid_from)):
                best = s
        return best.spread_bp if best else 0.0


# ---------------------------------------------------------------------------------------------- inputs
@dataclass(frozen=True)
class FundingPosition:
    """A position of one snapshot; MV and accrued in the position currency, signed (a short is < 0)."""

    position_id: str
    book_id: str
    book_type: str
    entity: str
    ccy: str
    mv_clean: float
    accrued: float = 0.0
    instrument_id: str | None = None
    bond_type: str | None = None
    settlement_ccy: str | None = None

    @property
    def mv_dirty(self) -> float:
        return self.mv_clean + self.accrued


@dataclass(frozen=True)
class HtmLot:
    """One HTM purchase: clean cost and accrued paid, in the currency, at its contract rate (decimal)."""

    lot_id: str
    position_id: str
    book_id: str
    entity: str
    ccy: str
    purchase_date: date
    clean_cost: float
    contract_rate: float | None
    purchase_accrued: float = 0.0
    accrued_until: date | None = None
    """The coupon date that returns the purchase accrued (funded on days up to and including it)."""
    maturity: date | None = None
    book_type: str = "HTM"


@dataclass(frozen=True)
class CashBalance:
    book_id: str
    book_type: str
    entity: str
    ccy: str
    balance: float


@dataclass(frozen=True)
class RepoDeal:
    """``cash_amount`` > 0: cash received (REPO), < 0: cash lent (reverse REPO)."""

    deal_id: str
    book_id: str
    book_type: str
    entity: str
    ccy: str
    cash_amount: float
    start_date: date
    maturity: date | None = None
    contract_rate: float | None = None
    collateral_id: str | None = None
    collateral_bond_type: str | None = None

    @property
    def term_days(self) -> int | None:
        return None if self.maturity is None else (self.maturity - self.start_date).days


@dataclass(frozen=True)
class AccrualPeriod:
    """Calendar days (``start``, ``end``]: ``start`` the previous business day, ``end`` the as-of date."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"accrual period ({self.start}, {self.end}] is empty")

    @property
    def days(self) -> tuple[date, ...]:
        return tuple(self.start + timedelta(days=i) for i in range(1, (self.end - self.start).days + 1))


# ---------------------------------------------------------------------------------------------- results
@dataclass(frozen=True)
class DailyAccrual:
    day: date
    body: float
    rate: float
    basis: DayBasis
    interest: float
    fx: float

    @property
    def interest_reporting(self) -> float:
        return self.interest * self.fx


@dataclass(frozen=True)
class FundingLine:
    kind: LineKind
    ref: str
    book_id: str
    book_type: str
    entity: str
    ccy: str
    base: FundingBase
    rate_expr: str
    days: tuple[DailyAccrual, ...]
    position_id: str | None = None
    note: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}|{self.ref}"

    @property
    def interest(self) -> float:
        return sum(d.interest for d in self.days)

    @property
    def interest_reporting(self) -> float:
        return sum(d.interest_reporting for d in self.days)

    def accrued(self, start: date | None = None, end: date | None = None, *, reporting: bool = True) -> float:
        """Interest of the days in (``start``, ``end``] (None: unbounded)."""
        return sum(d.interest_reporting if reporting else d.interest for d in self.days
                   if (start is None or d.day > start) and (end is None or d.day <= end))


@dataclass(frozen=True)
class FundingProblem:
    kind: LineKind
    ref: str
    reason: str


_KEYS: dict[str, Callable[[FundingLine], Any]] = {
    "position": lambda x: x.position_id, "book": lambda x: x.book_id, "entity": lambda x: x.entity,
    "kind": lambda x: x.kind, "line": lambda x: x.key, "book_ccy": lambda x: (x.book_id, x.ccy),
}


def total_by(results: Iterable[FundingResult], key: str = "book", *, start: date | None = None,
             end: date | None = None, reporting: bool = True) -> dict[Any, float]:
    """Funding of several runs summed by ``key`` (position, book, entity, kind, line, book_ccy) over the
    days in (``start``, ``end``]. The runs' periods must not overlap: each day is counted once. Lines
    without the key (a cash line by position) are left out. In the reporting currency unless
    ``reporting`` is false (then mixing currencies is the caller's choice: use ``book_ccy`` or ``line``)."""
    results = sorted(results, key=lambda r: r.period.start)
    for a, b in pairwise(results):
        if b.period.start < a.period.end:
            raise ValueError(f"funding periods overlap: ({a.period.start}, {a.period.end}] and "
                             f"({b.period.start}, {b.period.end}]")
    get = _KEYS[key]
    out: dict[Any, float] = {}
    for r in results:
        for line in r.lines:
            k = get(line)
            if k is not None:
                out[k] = out.get(k, 0.0) + line.accrued(start, end, reporting=reporting)
    return out


@dataclass(frozen=True)
class FundingResult:
    period: AccrualPeriod
    lines: tuple[FundingLine, ...]
    problems: tuple[FundingProblem, ...]
    notes: tuple[str, ...]
    policy: FundingPolicy

    @property
    def total(self) -> float:
        """In the reporting currency."""
        return sum(x.interest_reporting for x in self.lines)

    def by_position(self) -> dict[str, float]:
        return total_by([self], "position")

    def by_book(self) -> dict[str, float]:
        return total_by([self], "book")

    def by_entity(self) -> dict[str, float]:
        return total_by([self], "entity")

    def on(self, day: date) -> dict[str, float]:
        """The charge of one day by line key, in the reporting currency."""
        return {x.key: x.accrued(day - timedelta(days=1), day) for x in self.lines if any(d.day == day for d in x.days)}


# ---------------------------------------------------------------------------------------------- engine
class _Missing(Exception):
    pass


RateOf = Callable[[date], tuple[float, str, DayBasis | None]]


def _by_id(items: Iterable[Any], attr: str | Callable[[Any], Any], what: str) -> dict[Any, Any]:
    out: dict[Any, Any] = {}
    for x in items:
        k = attr(x) if callable(attr) else getattr(x, attr)
        if k in out:
            raise ValueError(f"duplicate {what} {k!r}")
        out[k] = x
    return out


class _Engine:
    def __init__(self, period: AccrualPeriod, rules: FundingRules, rates: RateIndexProvider, fx: FxProvider,
                 policy: FundingPolicy, entities: Mapping[str, EntityType | str] | None) -> None:
        self.period, self.rules, self.rates, self.fx, self.policy = period, rules, rates, fx, policy
        self.entities = None if entities is None else {k: EntityType(v) for k, v in entities.items()}
        self.lines: list[FundingLine] = []
        self.problems: list[FundingProblem] = []
        self.notes: list[str] = []
        self.htm_positions: set[str] = set()

    # -- lookups
    def entity_type(self, entity: str, kind: LineKind, ref: str) -> EntityType | None:
        if self.entities is None:
            return EntityType.MAIN
        t = self.entities.get(entity)
        if t is None:
            self.problems.append(FundingProblem(kind, ref, f"unknown entity {entity!r}"))
        return t

    def base(self, book_type: str, kind: LineKind, ref: str) -> FundingBase | None:
        b = self.policy.base_by_book_type.get(book_type)
        if b is None:
            self.problems.append(FundingProblem(kind, ref, f"no funding base for book type {book_type!r}"))
        return b

    def fx_on(self, ccy: str, day: date) -> float:
        d = {FxDate.AS_OF: self.period.end, FxDate.PREVIOUS: self.period.start}.get(self.policy.fx_date, day)
        return self.fx_at(ccy, d)

    def fx_at(self, ccy: str, d: date) -> float:
        v = 1.0 if ccy == self.policy.reporting_ccy else self.fx.rate(ccy, d)
        if v is None:
            raise _Missing(f"no {ccy} FX rate in force on {d}")
        return v

    def rule_rate(self, *, ccy: str, book_type: str, entity: str, book_id: str, instrument_id: str | None = None,
                  bond_type: str | None = None) -> RateOf:
        def rate_of(day: date) -> tuple[float, str, DayBasis | None]:
            rule = self.rules.resolve(day, ccy=ccy, book_type=book_type, entity=entity, book_id=book_id,
                                      instrument_id=instrument_id)
            if rule is None:
                raise _Missing(f"no funding rule for {book_type} {book_id} {ccy} on {day}")
            if rule.fixed_rate is not None:
                r = rule.fixed_rate
            else:
                v = self.rates.rate(str(rule.index), day)
                if v is None:
                    raise _Missing(f"no {rule.index} value in force on {day}")
                r = v + rule.spread_bp * BP
            extra = self.rules.extra_spread_bp(day, bond_type, ccy)
            expr = rule.expr + (f" + extra {extra:g} bp" if extra else "")
            return r + extra * BP, expr, rule.day_basis
        return rate_of

    @staticmethod
    def fixed(rate: float, label: str) -> RateOf:
        return lambda day: (rate, f"{rate:.2%} {label}", None)

    def add(self, kind: LineKind, ref: str, *, book_id: str, book_type: str, entity: str, ccy: str,
            base: FundingBase, bodies: Sequence[tuple[date, float]], rate_of: RateOf,
            position_id: str | None = None, note: str = "") -> None:
        days, expr = [], ""
        try:
            for day, body in bodies:
                rate, expr, basis = rate_of(day)
                basis = basis or self.policy.day_count.basis(kind, ccy)
                days.append(DailyAccrual(day, body, rate, basis, body * rate / basis.denominator,
                                         self.fx_on(ccy, day)))
        except _Missing as e:
            self.problems.append(FundingProblem(kind, ref, str(e)))
            return
        if days:
            self.lines.append(FundingLine(kind, ref, book_id, book_type, entity, ccy, base, expr, tuple(days),
                                          position_id, note))

    # -- line kinds
    def positions(self, current: Sequence[FundingPosition], previous: Sequence[FundingPosition] | None) -> None:
        pol, days = self.policy, self.period.days
        cur = _by_id(current, "position_id", "position")
        prev = _by_id(previous or (), "position_id", "previous position")
        if previous is None and current:
            self.notes.append(f"no previous positions snapshot: every position is new "
                              f"(new_positions={pol.new_positions})")
        deferred = 0
        for pid in [*cur, *(k for k in prev if k not in cur)]:
            c, p = cur.get(pid), prev.get(pid)
            if c is not None and p is not None:
                previous_base = pol.mv_base_date is BaseDate.PREVIOUS
                src, on, sched = (p, self.period.start, days) if previous_base else (c, self.period.end, days)
            elif c is not None:
                if pol.new_positions is NewPosition.NEXT_PERIOD:
                    deferred += self._position_checks(c) is not None
                    continue
                src, on = c, self.period.end
                sched = days[-1:] if pol.new_positions is NewPosition.LAST_DAY else days
            else:
                if pol.closed_positions is ClosedPosition.NOT_FUNDED:
                    continue
                src, on, sched = p, self.period.start, days
            self._position(src, on, sched)
        if deferred:
            self.notes.append(f"{deferred} new position(s) funded from the next period (new_positions=next_period)")

    def _position_checks(self, p: FundingPosition) -> FundingBase | None:
        """The MV base of a position funded by MV, None otherwise (another base, an other entity, a problem)."""
        t = self.entity_type(p.entity, LineKind.POS, p.position_id)
        if t is not EntityType.MAIN:
            return None
        base = self.base(p.book_type, LineKind.POS, p.position_id)
        if base is FundingBase.COST and p.position_id not in self.htm_positions:
            self.problems.append(FundingProblem(LineKind.HTM, p.position_id, "position of a cost book without "
                                                                             "an HTM purchase lot"))
        return base if base in (FundingBase.MV_CLEAN, FundingBase.MV_DIRTY) else None

    def _position(self, p: FundingPosition, on: date, sched: Sequence[date]) -> None:
        base = self._position_checks(p)
        if base is None:
            return
        mv, ccy = (p.mv_clean if base is FundingBase.MV_CLEAN else p.mv_dirty), p.ccy
        note = ""
        if self.policy.out_of_currency is OutOfCurrency.SETTLEMENT_CCY and p.settlement_ccy \
                and p.settlement_ccy != p.ccy:
            try:
                mv = mv * self.fx_at(p.ccy, on) / self.fx_at(p.settlement_ccy, on)
            except _Missing as e:
                self.problems.append(FundingProblem(LineKind.POS, p.position_id, str(e)))
                return
            ccy, note = p.settlement_ccy, f"funded in the settlement currency (MV {p.ccy} at FX of {on})"
        self.add(LineKind.POS, p.position_id, book_id=p.book_id, book_type=p.book_type, entity=p.entity, ccy=ccy,
                 base=base, bodies=[(d, -mv) for d in sched], position_id=p.position_id, note=note,
                 rate_of=self.rule_rate(ccy=ccy, book_type=p.book_type, entity=p.entity, book_id=p.book_id,
                                        instrument_id=p.instrument_id, bond_type=p.bond_type))

    def htm(self, lots: Sequence[HtmLot]) -> None:
        _by_id(lots, "lot_id", "HTM lot")
        for lot in lots:
            if self.entity_type(lot.entity, LineKind.HTM, lot.lot_id) is not EntityType.MAIN:
                continue
            if lot.contract_rate is None:
                self.problems.append(FundingProblem(LineKind.HTM, lot.lot_id, "no contract rate"))
                continue
            if self.policy.htm_base is None:
                self.problems.append(FundingProblem(LineKind.HTM, lot.lot_id, "no HTM base configured (htm_base)"))
                continue
            acc = lot.purchase_accrued if self.policy.htm_base is HtmBase.CASH_SPENT else 0.0
            if acc and lot.accrued_until is None:
                self.problems.append(FundingProblem(LineKind.HTM, lot.lot_id, "purchase accrued without the "
                                                                              "coupon date that returns it"))
                continue
            bodies = [(d, -(lot.clean_cost + (acc if acc and d <= lot.accrued_until else 0.0)))
                      for d in self.period.days
                      if d > lot.purchase_date and (lot.maturity is None or d <= lot.maturity)]
            self.add(LineKind.HTM, lot.lot_id, book_id=lot.book_id, book_type=lot.book_type, entity=lot.entity,
                     ccy=lot.ccy, base=FundingBase.COST, bodies=bodies, position_id=lot.position_id,
                     rate_of=self.fixed(lot.contract_rate, "contract"), note=f"htm_base={self.policy.htm_base}")

    def cash(self, current: Sequence[CashBalance], previous: Sequence[CashBalance] | None) -> None:
        pol = self.policy
        if pol.cash_base_date is BaseDate.PREVIOUS:
            if previous is None:
                if current:
                    self.problems.append(FundingProblem(LineKind.CASH, "*", "no previous-day cash snapshot "
                                                                            "(cash_base_date=previous_business_day)"))
                return
            snap = previous
        else:
            snap = current
        _by_id(snap, lambda c: (c.book_id, c.ccy), "cash balance")
        for c in snap:
            ref = f"{c.book_id}|{c.ccy}"
            t = self.entity_type(c.entity, LineKind.CASH, ref)
            if t is None:
                continue
            if t is EntityType.OTHER:
                kind, rule_type = LineKind.ENTITY_CASH, pol.entity_cash_rate_book_type or c.book_type
            else:
                if self.base(c.book_type, LineKind.CASH, ref) is not FundingBase.CASH:
                    continue
                kind, rule_type = LineKind.CASH, c.book_type
            self.add(kind, ref, book_id=c.book_id, book_type=c.book_type, entity=c.entity, ccy=c.ccy,
                     base=FundingBase.CASH, bodies=[(d, c.balance) for d in self.period.days],
                     rate_of=self.rule_rate(ccy=c.ccy, book_type=rule_type, entity=c.entity, book_id=c.book_id))

    def repos(self, deals: Sequence[RepoDeal]) -> None:
        _by_id(deals, "deal_id", "REPO deal")
        pol = self.policy.repo
        for r in deals:
            if self.entity_type(r.entity, LineKind.REPO, r.deal_id) is not EntityType.MAIN:
                continue
            mode = pol.rate_for(r.term_days)
            if mode is RepoRate.CONTRACT:
                if r.contract_rate is None:
                    self.problems.append(FundingProblem(LineKind.REPO, r.deal_id, "no contract rate"))
                    continue
                rate_of = self.fixed(r.contract_rate, "REPO contract")
            else:
                rate_of = self.rule_rate(ccy=r.ccy, book_type=pol.security_rate_book_type, entity=r.entity,
                                         book_id=r.book_id, instrument_id=r.collateral_id,
                                         bond_type=r.collateral_bond_type)
            bodies = [(d, r.cash_amount) for d in self.period.days
                      if d > r.start_date and (r.maturity is None or d <= r.maturity)]
            self.add(LineKind.REPO, r.deal_id, book_id=r.book_id, book_type=r.book_type, entity=r.entity,
                     ccy=r.ccy, base=FundingBase.CASH, bodies=bodies, rate_of=rate_of, note=f"rate={mode}")


def compute_funding(period: AccrualPeriod, rules: FundingRules, rates: RateIndexProvider, fx: FxProvider, *,
                    positions: Sequence[FundingPosition] = (),
                    previous_positions: Sequence[FundingPosition] | None = None,
                    cash: Sequence[CashBalance] = (), previous_cash: Sequence[CashBalance] | None = None,
                    htm_lots: Sequence[HtmLot] = (), repos: Sequence[RepoDeal] = (),
                    entities: Mapping[str, EntityType | str] | None = None,
                    policy: FundingPolicy = PROVISIONAL_POLICY) -> FundingResult:
    """Funding of one period. ``positions`` / ``cash`` are the as-of snapshots, ``previous_*`` those of the
    previous business day (None: none was taken, which is not the same as empty). ``entities`` maps an
    entity code to its type (None: every entity is main). Duplicate ids in one input raise ValueError."""
    e = _Engine(period, rules, rates, fx, policy, entities)
    e.htm_positions = {lot.position_id for lot in htm_lots}
    e.positions(positions, previous_positions)
    e.htm(htm_lots)
    e.cash(cash, previous_cash)
    e.repos(repos)
    return FundingResult(period, tuple(e.lines), tuple(e.problems), tuple(e.notes), policy)


# ---------------------------------------------------------------------------------------------- FX liability
@dataclass(frozen=True)
class FundingLiability:
    """The funding of a position as a currency liability: -base in the funding currency."""

    position_id: str
    book_id: str
    entity: str
    ccy: str
    amount: float


def funding_liabilities(positions: Sequence[FundingPosition], *, policy: FundingPolicy = PROVISIONAL_POLICY,
                        entities: Mapping[str, EntityType | str] | None = None, fx: FxProvider | None = None,
                        on: date | None = None) -> tuple[tuple[FundingLiability, ...], tuple[FundingProblem, ...]]:
    """The currency funding liability of every position funded by MV (OCP-4): a foreign-currency bond's FX
    delta (its dirty MV in the currency) less this liability is what the bond book keeps of FX before the
    evening OCP transfer (with a clean base: the accrued). ``fx`` and ``on`` are needed only to convert an
    out-of-currency bond funded in its settlement currency."""
    types = None if entities is None else {k: EntityType(v) for k, v in entities.items()}
    out, problems = [], []
    for p in positions:
        t = EntityType.MAIN if types is None else types.get(p.entity)
        base = policy.base_by_book_type.get(p.book_type)
        if t is None or base is None:
            problems.append(FundingProblem(LineKind.POS, p.position_id, f"unknown entity {p.entity!r}" if t is None
                                           else f"no funding base for book type {p.book_type!r}"))
            continue
        if t is EntityType.OTHER or base not in (FundingBase.MV_CLEAN, FundingBase.MV_DIRTY):
            continue
        mv, ccy = (p.mv_clean if base is FundingBase.MV_CLEAN else p.mv_dirty), p.ccy
        if policy.out_of_currency is OutOfCurrency.SETTLEMENT_CCY and p.settlement_ccy and p.settlement_ccy != ccy:
            rates = [None if fx is None or on is None else 1.0 if c == policy.reporting_ccy else fx.rate(c, on)
                     for c in (ccy, p.settlement_ccy)]
            if rates[0] is None or rates[1] is None:
                problems.append(FundingProblem(LineKind.POS, p.position_id,
                                               f"no {ccy}/{p.settlement_ccy} FX rate on {on}"))
                continue
            mv, ccy = mv * rates[0] / rates[1], p.settlement_ccy
        out.append(FundingLiability(p.position_id, p.book_id, p.entity, ccy, -mv))
    return tuple(out), tuple(problems)


def liability_fx_pnl(liabilities: Iterable[FundingLiability], fx: FxProvider, start: date, end: date,
                     reporting_ccy: str = "RUB") -> dict[str, float | None]:
    """FX revaluation of the liabilities held over (``start``, ``end``], by position, in the reporting
    currency: amount x (FX(end) - FX(start)); None when a rate is missing."""
    out: dict[str, float | None] = {}
    for x in liabilities:
        if x.ccy == reporting_ccy:
            out[x.position_id] = 0.0
            continue
        a, b = fx.rate(x.ccy, start), fx.rate(x.ccy, end)
        out[x.position_id] = None if a is None or b is None else x.amount * (b - a)
    return out


# ---------------------------------------------------------------------------------------------- write-off
def _weekday(d: date) -> bool:
    return d.weekday() < 5


def write_off_dates(start: date, end: date, periodicity: Periodicity,
                    rule: WriteOffDate = WriteOffDate.LAST_CALENDAR_DAY,
                    is_business_day: Callable[[date], bool] | None = None) -> tuple[date, ...]:
    """PnL write-off dates in (``start``, ``end``]: the last calendar or business day (``is_business_day``,
    by default Monday to Friday) of each day, month or quarter."""
    bd = is_business_day or _weekday
    if periodicity is Periodicity.NONE or end <= start:
        return ()
    if periodicity is Periodicity.DAILY:
        days = AccrualPeriod(start, end).days
        return tuple(d for d in days if rule is WriteOffDate.LAST_CALENDAR_DAY or bd(d))
    step = 1 if periodicity is Periodicity.MONTHLY else 3
    first = start + timedelta(days=1)
    y, m = first.year, -(-first.month // step) * step
    out = []
    while date(y, m, 1) <= end:
        d = date(y, m, calendar.monthrange(y, m)[1])
        if rule is WriteOffDate.LAST_BUSINESS_DAY:
            while not bd(d):
                d -= timedelta(days=1)
        if start < d <= end:
            out.append(d)
        m += step
        if m > 12:
            y, m = y + 1, m - 12
    return tuple(out)


def last_write_off(on: date, periodicity: Periodicity, rule: WriteOffDate = WriteOffDate.LAST_CALENDAR_DAY,
                   is_business_day: Callable[[date], bool] | None = None) -> date | None:
    """The latest write-off date on or before ``on`` within a year (None: none, or ``Periodicity.NONE``).
    The funding accrued since then: ``total_by(results, start=last_write_off(...))``."""
    dates = write_off_dates(on - timedelta(days=370), on, periodicity, rule, is_business_day)
    return dates[-1] if dates else None
