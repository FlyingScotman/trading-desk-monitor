"""Report lines of a spot type with a future value date that are really a derivative's nominal (TDM#693).

The OCP report writes the nominal of some FX derivatives (an NDF, a cross NDF, a forward, a future) as a line of a spot
type dated at the settlement of the deal. Such a line is in the base of the report, and the monitor adds the same deal
again from its positions or terms (``rules.ocp.published_day``): it is counted twice, and the base holds the
undiscounted nominal. The owner's rule (12.11.2026): a line the monitor can match with a derivative of the same book is
**always** taken out of the base; in its place stands the deal's own exposure: the market-data valuation (discounted)
when the deal is valued, else the legs of its terms at the discount factor 1 (``NO_CURVE``).

The adapter lists such lines in the report row's ``spot_future`` (private/1.9; a spot type, a value date later than the
settlement window after the report date): ``[{src, value_date, pair, amount_ccy}]``; an adapter 0.9.14 gave them in
``x_spot_future``, which is read while the row has no ``spot_future`` (``rules.ocp._spot_future_of``). This module
reads them (``spot_lines``), describes the deals they can be matched with (``Deal``) and matches (``match``) by the rule of the owner:

- the book of the line (a line of an entity-level unit: any book of the scope);
- the currency of the line is the currency of one of the deal's legs;
- the amount of the line equals the nominal of that leg, the same sign, within ``REL_TOL`` or ``ABS_TOL`` units;
- the date of the line is within ``WINDOW_BD`` business days of a date of the deal (the settlement or fixing date: the
  report writes the settlement date, the terms may give the fixing date or the other way round).

A line is matched with at most one deal, a leg of a deal with at most one line: the nearest date first, then the
position id. A line with several candidates takes the nearest and names the others (``Match.others``). Pure functions:
no store, no run; the calendar is the caller's (``is_business_day``).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

WINDOW_BD = 5
"""The most business days between the line's date and a date of the deal."""
REL_TOL = 1e-6
REL_TOL_RATE = 1e-4
ABS_TOL = 1.0
"""An amount matches the leg's nominal within ``max(ABS_TOL, REL_TOL * |nominal|)`` (units of the currency); a leg the
terms do not state but the contract rate gives (``Deal.rate_legs``: the nominal of the other currency × the rate) within
``REL_TOL_RATE``: the rate is rounded."""
MATCH_KINDS = ("fx_forward", "ndf", "fx_future")
"""The derivative kinds (``rules.ocp.DERIVATIVE_KINDS``) a line is matched with: forwards, NDFs (also a cross), futures."""
DF1_TEXT = "{why}: дисконт-фактор 1"
"""The mark of a deal taken at the legs' nominal (``Deal.note``)."""


@dataclass(frozen=True)
class SpotLine:
    """One entry of a report row's ``spot_future``, with the row's book and currency."""

    ccy: str
    value_date: date
    amount: float
    book_id: str | None = None
    pair: str | None = None
    src: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe, for the reconciliation's comparisons (``rules.ocp.reconcile``)."""
        return {"ccy": self.ccy, "value_date": self.value_date.isoformat(), "amount": self.amount,
                "book_id": self.book_id, "pair": self.pair, "src": self.src}

    @classmethod
    def of(cls, d: Mapping[str, Any]) -> SpotLine:
        return cls(str(d["ccy"]), date.fromisoformat(d["value_date"]), float(d["amount"]), d.get("book_id"),
                   d.get("pair"), d.get("src"))


@dataclass(frozen=True)
class Deal:
    """A derivative of the run's positions a line can be matched with."""

    position_id: str
    book_id: str
    kind: str
    component: str
    legs: Mapping[str, float]
    """The nominal per currency (not the reporting one), signed, all legs of the trade: what a line is matched on."""
    open_legs: Mapping[str, float]
    """The same of the legs not yet settled on the run's date: what the deal gives the OCP at the discount factor 1."""
    dates: tuple[date, ...]
    """The settlement, value and fixing dates of the deal."""
    trade_ids: tuple[str, ...] = ()
    rate_legs: frozenset[str] = frozenset()
    """The currencies whose nominal is not in the terms but the other leg's × ``contract_rate`` (a wider tolerance)."""
    own_legs: bool = False
    """True: the deal is taken at ``open_legs`` (not valued by core, its terms are known): the legs replace what the
    positions give for it, whatever the kind's rule says. False: the deal's own lines (PV, or the row's notional)."""
    note: str = ""
    """Why ``open_legs`` are at the discount factor 1 (``DF1_TEXT``); "" for a valued deal."""


@dataclass(frozen=True)
class Match:
    line: SpotLine
    deal: Deal
    days: int
    """Business days between the line's date and the nearest date of the deal."""
    others: tuple[str, ...] = ()
    """The other deals that matched the line too (position ids): the choice is the nearest date, the doubt is shown."""


@dataclass(frozen=True)
class Unmatched:
    line: SpotLine
    why: str
    """``no_deal`` (no deal of the book with a leg of this currency), ``amount``, ``date``, ``sign``, ``taken`` (every
    candidate matched another line)."""


@dataclass
class Result:
    matched: list[Match] = field(default_factory=list)
    unmatched: list[Unmatched] = field(default_factory=list)

    def deals(self) -> list[Deal]:
        """The matched deals, each once, in order of position id."""
        return sorted({m.deal.position_id: m.deal for m in self.matched}.values(), key=lambda d: d.position_id)


def _num(x: Any) -> float | None:
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _day(x: Any) -> date | None:
    if isinstance(x, date):
        return x
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def spot_lines(x: Any, *, ccy: str, book_id: str | None) -> list[SpotLine]:
    """The lines of a report row's ``spot_future`` (or ``x_spot_future``: stored as is); an entry that is not of the shape
    ``{value_date, amount_ccy, ...}`` is left out. None, anything but a list: ``[]``."""
    out = []
    for e in x if isinstance(x, list) else []:
        if not isinstance(e, Mapping):
            continue
        d, a = _day(e.get("value_date")), _num(e.get("amount_ccy"))
        if d is None or a is None:
            continue
        pair = e.get("pair")
        src = e.get("src")
        out.append(SpotLine(ccy, d, a, book_id, str(pair)[:7] if isinstance(pair, str) and pair else None,
                            str(src)[:20] if isinstance(src, str) and src else None))
    return out


def _between(a: date, b: date, is_business_day: Callable[[date], bool] | None) -> int:
    """The business days in (min, max]."""
    lo, hi = (a, b) if a <= b else (b, a)
    f = is_business_day or (lambda d: d.weekday() < 5)
    n, d = 0, lo
    while d < hi:
        d += timedelta(days=1)
        n += f(d)
    return n


def _same(amount: float, nominal: float, rel: float = REL_TOL) -> bool:
    return abs(amount - nominal) <= max(ABS_TOL, rel * abs(nominal))


def match(lines: Sequence[SpotLine], deals: Sequence[Deal],
          is_business_day: Callable[[date], bool] | None = None) -> Result:
    """Match ``lines`` with ``deals`` by the module's rule. ``deals``: those the caller allows for the lines' scope."""
    res = Result()
    cands: list[tuple[int, str, int, Deal]] = []  # (days, position id, line index, deal)
    reason: dict[int, str] = {}
    steps = ["no_deal", "amount", "sign", "date"]  # how far a line got: the furthest step is its reason
    for i, ln in enumerate(lines):
        far = 0
        for d in deals:
            if ln.book_id is not None and d.book_id != ln.book_id:
                continue
            nominal = d.legs.get(ln.ccy)
            if nominal is None:
                continue
            far = max(far, 1)
            if not _same(abs(ln.amount), abs(nominal), REL_TOL_RATE if ln.ccy in d.rate_legs else REL_TOL):
                continue
            if (ln.amount < 0) != (nominal < 0) and nominal != 0:
                far = max(far, 2)
                continue
            gap = min((_between(ln.value_date, x, is_business_day) for x in d.dates), default=None)
            if gap is None or gap > WINDOW_BD:
                far = max(far, 3)
                continue
            cands.append((gap, d.position_id, i, d))
        reason[i] = steps[far]
    cands.sort(key=lambda c: (c[0], c[1], c[2]))
    # per line its candidate legs, nearest date first (then position id): the order of the greedy pass and of the
    # search below; a leg is (position id, currency)
    by_line: dict[int, list[tuple[int, str, Deal]]] = {}
    for gap, pid, i, d in cands:
        by_line.setdefault(i, []).append((gap, pid, d))
    holder: dict[tuple[str, str], int] = {}  # leg -> the line that has it
    chosen: dict[int, tuple[int, Deal]] = {}  # line -> (days, deal)

    def assign(i: int, seen: set[tuple[str, str]]) -> bool:
        """Give line ``i`` a leg: a free one (nearest first), else one whose line can take another (a path that
        lengthens the matching: a line left without a deal by the greedy pass may move the others)."""
        for gap, pid, d in by_line.get(i, ()):
            key = (pid, lines[i].ccy)
            if key in holder or key in seen:
                continue
            holder[key] = i
            chosen[i] = (gap, d)
            return True
        for gap, pid, d in by_line.get(i, ()):
            key = (pid, lines[i].ccy)
            if key in seen:
                continue
            seen.add(key)
            if holder[key] != i and assign(holder[key], seen):
                holder[key] = i
                chosen[i] = (gap, d)
                return True
        return False

    for gap, pid, i, d in cands:  # the greedy pass: the nearest pairs first
        key = (pid, lines[i].ccy)
        if i not in chosen and key not in holder:
            holder[key] = i
            chosen[i] = (gap, d)
    for i in sorted(by_line):  # a line with candidates but no deal: move the others if that frees one
        if i not in chosen:
            assign(i, set())
    taken_line = set(chosen)
    for i in sorted(chosen):
        gap, d = chosen[i]
        others = tuple(sorted({pid for _, pid, _ in by_line[i] if pid != d.position_id}))
        res.matched.append(Match(lines[i], d, gap, others))
    for i, ln in enumerate(lines):
        if i not in taken_line:
            why = "taken" if any(c[2] == i for c in cands) else reason[i]
            res.unmatched.append(Unmatched(ln, why))
    res.matched.sort(key=lambda m: (m.line.ccy, m.line.value_date, m.deal.position_id))
    return res
