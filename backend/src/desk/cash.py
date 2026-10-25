"""Cash balances per book with history and the cash breaks between bookkeeping systems (ROADMAP §0.1 step 4, B3;
issue #61).

``cash_balances`` (private contract, 08 §10) is a stock: per (entity, book) unit the current revision with the latest
valid date <= D (``store.dataset_rows``). A multi-date file is a history delivered at once (08 §6.1), so a book's
balances of past days are there as soon as the file is scanned.

**Ledgers.** A row's optional ``ledger`` names the bookkeeping system the balance comes from, when the same balance
is reported by more than one. ``compare`` sums the rows of a date per (entity, book, currency) and ledger, over the
accounts, and gives each such balance a status:

- ``match``: two or more ledgers report it and agree within ``tolerance`` (the rule of the report controls, 08 §6.4:
  max(0.01, 1e-8 x |balance|) in the currency);
- ``break``: two or more ledgers report it and disagree;
- ``one_ledger``: the entity's rows of that date come from several ledgers, but this balance from one only (the
  other system has no such book or currency: shown, not a break);
- ``single``: the entity has one ledger (or none) that date; nothing to reconcile.

Which system is authoritative on a break is not part of the contract (08 §10) and the backend does not guess one.
**Conservative default** (until the adapter or a decision names the main system): a balance is *known* when its
ledgers agree (``match``) or only one reports it (``one_ledger``, ``single``); a ``break`` has no known balance, so
the funding run funds no line on it and says so as a problem (never the larger, the smaller or the first one).
``tolerance`` is a parameter of ``compare``; the default is ``TOLERANCE``.

``view(con, on, days)`` is ``GET /api/v1/cash``: the date's balances with their statuses, accounts and ledgers, a
history per balance over the last ``days`` dates that have cash, and per date the counts for the screen's strip
(C15 «Остатки и расхождения»).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from . import store

MATCH, BREAK, ONE_LEDGER, SINGLE = "match", "break", "one_ledger", "single"
STATUS_TEXT = {MATCH: "сходится", BREAK: "расхождение", ONE_LEDGER: "только в одной системе",
               SINGLE: "одна система"}
NO_LEDGER = ""
"""The ledger key of a row without ``ledger`` (one system, not named)."""


@dataclass(frozen=True)
class Tolerance:
    """Two ledgers agree when they differ by at most max(``abs_ccy``, ``rel`` x the larger |balance|)."""

    abs_ccy: Decimal = Decimal("0.01")
    rel: Decimal = Decimal("1e-8")

    def of(self, *values: Decimal) -> Decimal:
        return max(self.abs_ccy, self.rel * max((abs(v) for v in values), default=Decimal(0)))


TOLERANCE = Tolerance()


@dataclass
class Balance:
    """One (entity, book, currency) balance of a date, by ledger."""

    entity: str
    book_id: str
    ccy: str
    by_ledger: dict[str, Decimal]
    status: str
    valid_date: date | None = None
    accounts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.entity}|{self.book_id}|{self.ccy}"

    @property
    def known(self) -> Decimal | None:
        """The balance the desk can use: the ledgers' common value (their mean within tolerance; one ledger: its
        value); None on a break (conservative default, module docstring)."""
        if self.status == BREAK or not self.by_ledger:
            return None
        vals = list(self.by_ledger.values())
        return vals[0] if len(vals) == 1 else sum(vals, Decimal(0)) / len(vals)

    @property
    def diff(self) -> Decimal | None:
        """The largest ledger minus the smallest; None with one ledger."""
        vals = list(self.by_ledger.values())
        return max(vals) - min(vals) if len(vals) > 1 else None

    def ledgers(self) -> list[str]:
        return sorted(self.by_ledger)


def compare(rows: list[dict[str, Any]], tolerance: Tolerance = TOLERANCE) -> list[Balance]:
    """``cash_balances`` rows of one date (``store.dataset_rows(...).rows``) as balances by ledger, sorted by
    (entity, book, currency); see the module docstring for the statuses."""
    sums: dict[tuple[str, str, str], dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    accounts: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    vdate: dict[tuple[str, str, str], date | None] = {}
    entity_ledgers: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        k = (str(r["entity"]), str(r["book_id"]), str(r["ccy"]))
        led = str(r.get("ledger") or NO_LEDGER)
        amount = Decimal(str(r["balance_ccy"]))
        sums[k][led] += amount
        entity_ledgers[k[0]].add(led)
        vdate[k] = r.get("_valid_date")
        accounts[k].append({"account_id": r.get("account_id"), "account_kind": r.get("account_kind"), "ledger": led,
                            "balance_ccy": float(amount)})
    out = []
    for k in sorted(sums):
        by = dict(sums[k])
        vals = list(by.values())
        if len(by) > 1:
            status = MATCH if max(vals) - min(vals) <= tolerance.of(*vals) else BREAK
        else:
            status = ONE_LEDGER if len(entity_ledgers[k[0]]) > 1 else SINGLE
        acc = sorted(accounts[k], key=lambda a: (a["ledger"], str(a["account_id"]), str(a["account_kind"] or "")))
        out.append(Balance(k[0], k[1], k[2], by, status, vdate.get(k), acc))
    return out


def balances(con: psycopg.Connection, on: date, *, known_at: datetime | None = None,
             tolerance: Tolerance = TOLERANCE) -> tuple[list[Balance], list[dict]]:
    """The balances in force on ``on`` (``compare`` of ``store.dataset_rows``) and the units read."""
    ds = store.dataset_rows(con, "cash_balances", on, known_at=known_at)
    return compare(ds.rows, tolerance), ds.units


# --- the screen (GET /api/v1/cash) ----------------------------------------------------------------------------

def _moscow_today() -> date:
    return (datetime.now(UTC) + timedelta(hours=3)).date()


def cash_dates(con: psycopg.Connection, to: date, n: int) -> list[date]:
    """The latest ``n`` valid dates <= ``to`` that have a ``cash_balances`` unit, newest first."""
    return [d for (d,) in con.execute(
        "SELECT DISTINCT valid_date FROM dataset_set WHERE dataset = 'cash_balances' AND valid_date <= %s"
        " ORDER BY valid_date DESC LIMIT %s", (to, n))]


def _f(x: Decimal | None) -> float | None:
    return None if x is None else float(x)


def _counts(bs: list[Balance]) -> dict[str, int]:
    c = {MATCH: 0, BREAK: 0, ONE_LEDGER: 0, SINGLE: 0}
    for b in bs:
        c[b.status] += 1
    return c


def view(con: psycopg.Connection, on: date | None = None, days: int = 20,
         tolerance: Tolerance = TOLERANCE) -> dict[str, Any]:
    """The cash screen of ``on`` (default: the latest date with cash, else today, Moscow): see the module docstring.
    Amounts in the balance's currency."""
    today = _moscow_today()
    if on is None:
        latest = cash_dates(con, today, 1)
        on = latest[0] if latest else today
    dates = cash_dates(con, on, days)
    if on not in dates:  # a date without its own file: the balances carried forward to it
        dates = [on, *dates][:days]
    books = {b["book_id"]: b for b in store.current_books(con, on)}
    per_date: dict[date, list[Balance]] = {}
    units: list[dict] = []
    for d in dates:
        per_date[d], u = balances(con, d, tolerance=tolerance)
        if d == on:
            units = u
    cur = per_date.get(on, [])
    history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in sorted(per_date):
        for b in per_date[d]:
            history[b.key].append({"date": d.isoformat(), "status": b.status, "known": _f(b.known),
                                   "by_ledger": {k: float(v) for k, v in b.by_ledger.items()},
                                   "valid_date": b.valid_date.isoformat() if b.valid_date else None})
    ledgers = sorted({led for b in cur for led in b.by_ledger})
    rows = []
    for b in cur:
        m = books.get(b.book_id) or {}
        rows.append({"key": b.key, "entity": b.entity, "book_id": b.book_id, "book_name": m.get("name"),
                     "book_type": m.get("book_type"), "ccy": b.ccy, "status": b.status,
                     "status_text": STATUS_TEXT[b.status], "known": _f(b.known), "diff": _f(b.diff),
                     "by_ledger": {k: float(v) for k, v in b.by_ledger.items()},
                     "valid_date": b.valid_date.isoformat() if b.valid_date else None,
                     "age_days": (on - b.valid_date).days if b.valid_date else None,
                     "accounts": b.accounts, "history": history.get(b.key, [])})
    breaks_by_ccy: dict[str, float] = defaultdict(float)
    for b in cur:
        if b.status == BREAK and b.diff is not None:
            breaks_by_ccy[b.ccy] += float(b.diff)
    return {
        "date": on.isoformat(),
        "ledgers": ledgers,
        "tolerance": {"abs_ccy": float(tolerance.abs_ccy), "rel": float(tolerance.rel)},
        "summary": {"balances": len(cur), **_counts(cur), "breaks_by_ccy": dict(breaks_by_ccy),
                    "books": len({b.book_id for b in cur}), "units": len(units)},
        "rows": rows,
        "days": [{"date": d.isoformat(), "balances": len(per_date[d]), **_counts(per_date[d])}
                 for d in sorted(per_date, reverse=True)],
        "status_text": STATUS_TEXT,
        "default_rule": "расхождение: остатка нет (основная система не задана), фондирование по нему не считается",
    }
