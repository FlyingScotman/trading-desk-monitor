"""Funding in the run (wave B1, FND-5): the generic funding engine (``desk.rules.funding``) on the run's inputs, its
result in the run's publication.

**Inputs**, all as the run sees them (its date ``D``, its ``known_at``):

- the adapter's reference tables in force (``store.current_reference``): ``funding_rules`` (every revision in force
  on a day of the period, so a rate change inside a weekend applies from its day), ``entities`` and ``funding_policy``
  (private/1.3, on ``D``). ``rules.reference.policy_from_reference`` and ``apply_funding_policy`` make the engine's
  ``FundingPolicy``: the base of each book type and the write-off from the adapter, every other convention the
  adapter's when ``funding_policy`` gives it, else the engine's provisional default (``rules.funding.OPEN_DECISIONS``;
  the block's ``policy.provisional`` lists them). **No funding rule in force: "not configured"**, said in the run log,
  the block and the pages, and nothing of funding is published as 0;
- the period (previous business day, ``D``]: its start is the previous market's date, else the previous default run's,
  else the weekday before;
- positions: the run's bond rows (``tdm_run.run_content``) with their MV in their currency and the accrued (core: the
  bonds' accrued; an unmapped bond: the report's ``accrued_ccy``, else unknown: a problem on a clean-MV base), and the
  same rows of the default run of the period's start (DEC01: the previous-day MV and which positions are new or closed
  is policy). No such run: no previous snapshot (the engine treats every position as new);
- HTM purchase lots: the positions of a ``cost`` book type with the report's ``purchase_date``,
  ``purchase_price_pct`` (on the row's face), ``contract_rate_pct`` and, for ``htm_base`` ``cash_spent``, the
  ``purchase_accrued_ccy`` of private/1.3, funded until the first coupon after the purchase (the bond's schedule);
- cash: ``cash_balances`` in force on ``D`` and on the start (a stock, carried forward; a multi-date file's history
  gives the start's balance too), summed per book and currency over its accounts (``desk.cash.balances``). A balance
  several ledgers report is funded when they agree within ``cash.TOLERANCE``; when they disagree it is a problem and
  not funded (which ledger is authoritative is not in the contract: the conservative default of ``desk.cash``). A
  book funded on its cash (its book type's base ``cash``, or every book of an entity of scheme ``cash_balance``)
  with no balance is a problem, not a 0;
- REPO: none (the contract has no REPO terms yet: 08 §10 drafts), said in the notes;
- index fixings and FX: the hub fixings of both markets in percent (``rate_index.FixingTable``, a value older than
  ``FIXING_MAX_AGE_DAYS`` is none) and the CBR official rates in force (``FxTable``, the reporting currency RUB).

**Output** (``RunFunding``): the block stored as ``run_reference`` kind ``funding`` (``BLOCK_VERSION``): status, period,
lines, per position, by book (with the write-off: periodicity, last date and the funding accrued since, from the
earlier default runs' blocks), by entity, problems, notes, the policy with its sources and the rules used; per position
``fact.extra.funding`` (state ``funded`` / ``deferred`` (new, funded from the next period) / ``not_funded`` (its book
type is not funded on MV, an entity funded on its cash, not a bond) / ``problem`` / ``not_configured``) and the
``funding`` component of its flash PnL (``pnl_funding``): the line's interest in RUB, 0 for ``deferred`` and
``not_funded``, absent (null) for ``problem`` and ``not_configured``. The flash PnL's components then sum to the PnL net
of funding (on the pages the carry is without it and the funding its own factor, TDM#168). A funding line of a position without a flash PnL, of a closed position,
of cash or of an entity is a book-level line (``in_position`` false): the block's totals are ``in_positions`` +
``book_level``. The currency funding liability of the foreign bonds (OQ-04), the bonds' FX delta net of it and its FX
revaluation (``fx_funding``) are ``publish.fx_liability`` (the block's ``fx_liability``), computed after this one.

The run never fails on funding: an error is status ``failed`` with its type and message in the log.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

import psycopg

from .. import cash, store
from ..marketdata.adapter import MarketView
from ..rules import funding as F
from ..rules.rate_index import FixingTable, FxTable
from ..rules.reference import (
    apply_funding_policy,
    entities_from_reference,
    funding_rules_from_reference,
    policy_from_reference,
)

BLOCK_VERSION = 1
COMPUTED, NOT_CONFIGURED, INVALID, FAILED, NOT_COMPUTED = (
    "computed", "not_configured", "invalid_config", "failed", "not_computed")
STATUS_TEXT = {
    COMPUTED: "фондирование посчитано",
    NOT_CONFIGURED: "фондирование не настроено: адаптер не передал правил фондирования (reference() funding_rules)",
    INVALID: "фондирование не посчитано: правила фондирования адаптера противоречат друг другу",
    FAILED: "фондирование не посчитано: ошибка расчёта",
    NOT_COMPUTED: "прогон без расчёта фондирования (до B1)",
}
"""Status -> the text the run log and the pages show."""
FUNDED, DEFERRED, NOT_FUNDED, PROBLEM = "funded", "deferred", "not_funded", "problem"
FIXING_MAX_AGE_DAYS = 7
"""An index fixing older than this (calendar days) is no value on a day: a series the hub stopped is not extended."""
FX_MAX_AGE_DAYS = 7
REPORTING_CCY = "RUB"
LOG_PROBLEMS = 50
"""Problems listed one per WARN line in the run log; the rest are counted."""


def _jsonable(x: Any) -> Any:
    if isinstance(x, Enum):
        return x.value
    if isinstance(x, date):
        return x.isoformat()
    if isinstance(x, dict):
        return {str(_jsonable(k)): _jsonable(v) for k, v in x.items()}
    if isinstance(x, list | tuple | set):
        return [_jsonable(v) for v in x]
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


def _f(x: Any) -> float | None:
    try:
        return None if x is None else float(Decimal(str(x)))
    except (ArithmeticError, ValueError):
        return None


@dataclass
class RunFunding:
    """What ``compute`` gives the publication."""

    status: str
    block: dict[str, Any]
    """``run_reference`` kind ``funding``."""
    per_position: dict[str, dict[str, Any]] = field(default_factory=dict)
    """position_id -> ``fact.extra.funding``."""
    pnl_funding: dict[str, float] = field(default_factory=dict)
    """position_id -> the ``funding`` component of its flash PnL (RUB); absent: none (a problem, not configured)."""
    log: list[tuple[str, str]] = field(default_factory=list)

    def lineage(self) -> dict[str, Any]:
        b = self.block
        return {"status": self.status, "period": b.get("period"), "problems": len(b.get("problems") or []),
                "total_rub": (b.get("totals") or {}).get("total"), "rules": len(b.get("rules") or []),
                "policy_source": (b.get("policy") or {}).get("source")}

    def apply(self, rows: list[dict[str, Any]], reference: dict[str, Any], log: list[tuple[str, str]]) -> None:
        """Into the run's content: ``extra.funding`` of each row, the block as reference kind ``funding``, the log
        lines before the run log's last line (the number of positions published)."""
        for r in rows:
            if (x := self.per_position.get(r["position_id"])) is not None:
                r.setdefault("extra", {})["funding"] = x
        reference["funding"] = self.block
        at = len(log) - 1 if log else 0
        log[at:at] = self.log


def period_start(con: psycopg.Connection, on: date, prev: MarketView | None) -> date:
    """The day before the period: the previous market's date, else the previous default run's, else the weekday
    before (as the compat adapter's ``accrualDays``)."""
    if prev is not None and prev.business_date < on:
        return prev.business_date
    row = con.execute(f"SELECT max(business_date) FROM run WHERE run_id IN ({store.DEFAULT_RUNS})"
                      " AND business_date < %s", (on,)).fetchone()
    if row and row[0]:
        return row[0]
    d = on - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def fixing_table(markets: Iterable[MarketView | None]) -> FixingTable:
    """The percent fixings of the markets (``fixings`` of the snapshots, an older market first: a later one's row of
    the same date wins) as decimals."""
    series: dict[str, dict[date, float]] = {}
    for m in markets:
        for f in (m.fixings if m is not None else ()):
            if f.get("unit") != "pct" or f.get("value") is None:
                continue
            series.setdefault(f["index_code"], {})[date.fromisoformat(f["fixing_date"])] = float(Decimal(f["value"])) / 100
    return FixingTable(series, max_age_days=FIXING_MAX_AGE_DAYS)


def fx_table(markets: Iterable[MarketView | None]) -> FxTable:
    """The CBR official rates of the markets by effective date (RUB per unit)."""
    series: dict[str, dict[date, float]] = {}
    for m in markets:
        for r in (m.fx_rates if m is not None else ()):
            if r.get("kind") == "cbr_official" and r.get("rate_rub_per_unit") and r.get("effective_date"):
                series.setdefault(r["ccy"], {})[date.fromisoformat(r["effective_date"])] = float(
                    Decimal(r["rate_rub_per_unit"]))
    return FxTable(REPORTING_CCY, series, max_age_days=FX_MAX_AGE_DAYS)


def _rules_in_period(con: psycopg.Connection, period: F.AccrualPeriod, known_at: Any) -> list[dict]:
    """Every ``funding_rules`` revision in force on some day of the period (one query per day)."""
    out: dict[tuple, dict] = {}
    for d in period.days:
        for r in store.current_reference(con, "funding_rules", d, known_at):
            out.setdefault((r["rule_id"], r.get("valid_from") or ""), r)
    return [out[k] for k in sorted(out)]


def _position(row: dict[str, Any], books: dict[str, dict]) -> tuple[F.FundingPosition | None, str | None]:
    """A bond row (``tdm_run.position_row`` / ``direct_row`` shape) as an engine position, and why not (or a note).
    MV and accrued in the row's currency, signed."""
    if row.get("instr_type") != "BOND":
        return None, "не облигация: фондируется по правилам книги, а не по MV"
    b = books.get(row["book_id"]) or {}
    ccy = row.get("ccy") or REPORTING_CCY
    mv = _f(row.get("mv"))
    if mv is None:
        return None, "нет MV"
    ex = row.get("extra") or {}
    acc: float | None
    if row.get("coupon_type") is not None and row.get("accrued") is not None:  # core: per bond, in RUB
        fx = _f(ex.get("fx_rate")) or 1.0
        acc = (_f(ex.get("quantity_bonds")) or 0.0) * float(row["accrued"]) / fx
    else:
        acc = _f((ex.get("reported") or {}).get("accrued_ccy"))
    pos = F.FundingPosition(row["position_id"], row["book_id"], b.get("book_type") or "", b.get("entity") or "—", ccy,
                            mv - (acc or 0.0), acc or 0.0, instrument_id=row.get("instrument_id"))
    return pos, None if acc is not None else "accrued unknown"


@dataclass
class _Inputs:
    current: list[F.FundingPosition]
    previous: list[F.FundingPosition] | None
    unknown_accrued: set[str]
    lots: list[F.HtmLot]
    cash: list[F.CashBalance]
    previous_cash: list[F.CashBalance] | None
    problems: list[F.FundingProblem]
    notes: list[str]
    cash_units: list[dict]
    previous_run_id: int | None


def _cash(con: psycopg.Connection, d: date, known_at: Any, books: dict[str, dict], problems: list[F.FundingProblem]
          ) -> tuple[list[F.CashBalance] | None, list[dict]]:
    """``cash_balances`` in force on ``d``, per (book, currency) summed over its accounts and compared across ledgers
    (``desk.cash``: a break is a problem, not a balance); None without any unit."""
    bals, units_read = cash.balances(con, d, known_at=known_at)
    if not units_read:
        return None, []
    out = []
    for b in bals:
        ref = f"{b.book_id}|{b.ccy}"
        if b.known is None:  # a break between ledgers: no balance is known (desk.cash, the conservative default)
            problems.append(F.FundingProblem(
                F.LineKind.CASH, ref, f"расхождение остатка между учётными системами ({', '.join(b.ledgers())}) "
                f"на {_f(b.diff):,.2f} {b.ccy}: какая основная — не задано, не фондируется (экран C15)"))
            continue
        bt = (books.get(b.book_id) or {}).get("book_type")
        if not bt:
            problems.append(F.FundingProblem(F.LineKind.CASH, ref, f"книга {b.book_id} без book_type"))
            continue
        out.append(F.CashBalance(b.book_id, bt, b.entity, b.ccy, float(b.known)))
    units = [{"scope": u["scope"], "valid_date": _jsonable(u["valid_date"]), "set_id": u["set_id"],
              "row_count": u["row_count"]} for u in units_read]
    return out, units


def _lots(rows: list[dict[str, Any]], inputs: store.PositionInputs, terms: dict[str, Any], books: dict[str, dict],
          policy: F.FundingPolicy, problems: list[F.FundingProblem]) -> list[F.HtmLot]:
    lots = []
    for r in rows:
        bt = (books.get(r["book_id"]) or {}).get("book_type")
        if r.get("instr_type") != "BOND" or policy.base_by_book_type.get(bt or "") is not F.FundingBase.COST:
            continue
        pid, x = r["position_id"], inputs.extra.get(r["position_id"]) or {}
        missing = [k for k in ("purchase_date", "purchase_price_pct") if x.get(k) is None]
        if missing:
            problems.append(F.FundingProblem(F.LineKind.HTM, pid, "нет " + ", ".join(missing) + " в отчёте"))
            continue
        acc = _f(x.get("purchase_accrued_ccy"))
        if policy.htm_base is F.HtmBase.CASH_SPENT and acc is None:
            problems.append(F.FundingProblem(F.LineKind.HTM, pid, "нет purchase_accrued_ccy (htm_base cash_spent)"))
            continue
        pdate = x["purchase_date"] if isinstance(x["purchase_date"], date) else date.fromisoformat(x["purchase_date"])
        t = terms.get(pid)
        coupons = sorted(c.pay_date for c in (t.coupons if t is not None else ()) if c.pay_date > pdate)
        mat = (t.maturity_date if t is not None else None) or x.get("maturity_date")
        rate = _f(x.get("contract_rate_pct"))
        b = books.get(r["book_id"]) or {}
        lots.append(F.HtmLot(pid, pid, r["book_id"], b.get("entity") or "—", r.get("ccy") or REPORTING_CCY, pdate,
                             (_f(r.get("notional")) or 0.0) * float(x["purchase_price_pct"]) / 100,
                             None if rate is None else rate / 100, purchase_accrued=acc or 0.0,
                             accrued_until=coupons[0] if coupons else None,
                             maturity=mat if isinstance(mat, date) or mat is None else date.fromisoformat(mat),
                             book_type=bt or "HTM"))
    return lots


def _previous_rows(con: psycopg.Connection, start: date) -> tuple[int | None, list[dict], dict[str, dict]]:
    run = con.execute(f"SELECT run_id FROM run WHERE run_id IN ({store.DEFAULT_RUNS}) AND business_date = %s",
                      (start,)).fetchone()
    if not run:
        return None, [], {}
    rid = run[0]
    facts = [f for (f,) in con.execute("SELECT fact FROM run_position WHERE run_id = %s AND fact IS NOT NULL"
                                       " ORDER BY ord", (rid,))]
    books = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'books'", (rid,)).fetchone()
    return rid, facts, {b["book_id"]: b for b in (books[0] if books else [])}


def _gather(con: psycopg.Connection, period: F.AccrualPeriod, content_rows: list[dict[str, Any]],
            inputs: store.PositionInputs, terms: dict[str, Any], books: dict[str, dict], policy: F.FundingPolicy
            ) -> _Inputs:
    problems: list[F.FundingProblem] = []
    notes: list[str] = []
    current, unknown = [], set()
    for r in content_rows:
        p, why = _position(r, books)
        if p is not None:
            current.append(p)
            if why:
                unknown.add(p.position_id)
    prev_id, prev_facts, prev_books = _previous_rows(con, period.start)
    previous: list[F.FundingPosition] | None = None
    if prev_id is None:
        notes.append(f"нет прогона за {period.start}: предыдущего среза позиций нет")
    else:
        previous = []
        for r in prev_facts:
            p, why = _position(r, {**books, **{k: v for k, v in prev_books.items() if v.get("book_type")}})
            if p is not None:
                previous.append(p)
                if why:
                    unknown.add(p.position_id)
    lots = _lots(content_rows, inputs, terms, books, policy, problems)
    on_prev = policy.cash_base_date is F.BaseDate.PREVIOUS  # the snapshot the engine reads says its problems
    cash, units = _cash(con, period.end, inputs.known_at, books, [] if on_prev else problems)
    prev_cash, _ = _cash(con, period.start, inputs.known_at, books, problems if on_prev else []) \
        if cash is not None else (None, [])
    notes.append("РЕПО: условия сделок РЕПО (trade_terms repo, контракт private/1.4) движок пока не читает: не фондируются")
    return _Inputs(current, previous, unknown, lots, cash or [], prev_cash, problems, notes, units, prev_id)


def _line_json(x: F.FundingLine, in_position: bool, note: str = "") -> dict[str, Any]:
    last = x.days[-1]
    return {"key": x.key, "kind": x.kind.value, "ref": x.ref, "book_id": x.book_id, "book_type": x.book_type,
            "entity": x.entity, "ccy": x.ccy, "position_id": x.position_id, "base": x.base.value,
            "rate_expr": x.rate_expr, "rate_pct": last.rate * 100, "day_basis": last.basis.value,
            "body_ccy": last.body, "body_rub": last.body * last.fx, "days": len(x.days),
            "first_day": x.days[0].day.isoformat(), "last_day": last.day.isoformat(),
            "interest_ccy": x.interest, "interest_rub": x.interest_reporting, "in_position": in_position,
            "note": "; ".join(n for n in (x.note, note) if n)}


def _since_write_off(con: psycopg.Connection, on: date, books: dict[str, date | None]) -> dict[str, dict[str, float]]:
    """Per book the daily funding (RUB, by day) of the earlier default runs' blocks after the earliest write-off date
    asked for (``books``: book -> its last write-off, None: none)."""
    lows = [d for d in books.values() if d is not None]
    if not lows:
        return {}
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for (daily,) in con.execute(
            "SELECT r.data->'daily_by_book' FROM run_reference r JOIN run USING (run_id)"
            f" WHERE r.kind = 'funding' AND run.run_id IN ({store.DEFAULT_RUNS})"
            " AND run.business_date > %s AND run.business_date < %s ORDER BY run.business_date",
            (min(lows), on)):
        for b, days in (daily or {}).items():
            if b in books:
                out[b].update(days)
    return out


def _empty_block(status: str, text: str, period: F.AccrualPeriod | None, **kw: Any) -> dict[str, Any]:
    return {"version": BLOCK_VERSION, "status": status, "text": text, "reporting_ccy": REPORTING_CCY,
            "period": None if period is None else {"start": period.start.isoformat(), "end": period.end.isoformat(),
                                                   "days": len(period.days)},
            "totals": None, "by_book": {}, "by_entity": {}, "lines": [], "problems": [], "notes": [],
            "policy": None, "rules": [], "daily_by_book": {}, **kw}


def compute(con: psycopg.Connection, on: date, prev: MarketView | None, market: MarketView,
            content_rows: list[dict[str, Any]], run_books: list[dict[str, Any]], inputs: store.PositionInputs,
            terms: dict[str, Any] | None = None) -> RunFunding:
    """Funding of the run of ``on`` (see the module docstring). ``content_rows`` and ``run_books``:
    ``RunContent.rows`` and ``RunContent.reference["books"]``; ``terms``: position_id -> the bond's ``BondTerms``
    (an HTM lot's coupon date and maturity)."""
    try:
        return _compute(con, on, prev, market, content_rows, run_books, inputs, terms or {})
    except Exception as e:  # noqa: BLE001 - funding never fails the run; it says why it has none
        text = f"{STATUS_TEXT[FAILED]}: {type(e).__name__}: {e}"
        pids = {r["position_id"] for r in content_rows}
        return RunFunding(FAILED, _empty_block(FAILED, text, None),
                          {p: {"state": PROBLEM, "amount_rub": None, "reason": STATUS_TEXT[FAILED]} for p in pids},
                          log=[("WARN", text[:2000])])


def _compute(con: psycopg.Connection, on: date, prev: MarketView | None, market: MarketView,
             content_rows: list[dict[str, Any]], run_books: list[dict[str, Any]], inputs: store.PositionInputs,
             terms: dict[str, Any]) -> RunFunding:
    period = F.AccrualPeriod(period_start(con, on, prev), on)
    known_at = inputs.known_at
    rule_rows = _rules_in_period(con, period, known_at)
    pids = [r["position_id"] for r in content_rows]
    if not any(r.get("active", True) is not False for r in rule_rows):
        text = STATUS_TEXT[NOT_CONFIGURED]
        return RunFunding(NOT_CONFIGURED, _empty_block(NOT_CONFIGURED, text, period),
                          {p: {"state": NOT_CONFIGURED, "amount_rub": None, "reason": text} for p in pids},
                          log=[("WARN", text + "; фондирование не посчитано, в PnL его нет (не 0)")])
    entity_rows = store.current_reference(con, "entities", on, known_at)
    policy_rows = store.current_reference(con, "funding_policy", on, known_at)
    try:
        base_policy = policy_from_reference(rule_rows, entity_rows)
    except ValueError as e:
        text = f"{STATUS_TEXT[INVALID]}: {e}"
        return RunFunding(INVALID, _empty_block(INVALID, text, period),
                          {p: {"state": PROBLEM, "amount_rub": None, "reason": text} for p in pids},
                          log=[("WARN", text)])
    settings = apply_funding_policy(policy_rows, base_policy)
    pol = settings.policy
    rules = funding_rules_from_reference(rule_rows)
    types, _ = entities_from_reference(entity_rows)
    entities = types or None
    books = {b["book_id"]: b for b in store.current_books(con, on, known_at)}  # also books with only cash
    books.update({b["book_id"]: b for b in run_books})  # the run's own reference (store metadata, else demo books)
    run_book_ids = {r["book_id"] for r in content_rows}
    inp = _gather(con, period, content_rows, inputs, terms, books, pol)
    problems = list(inp.problems)
    problems += [F.FundingProblem(F.LineKind.POS, "*", c) for c in settings.conflicts]
    # a clean-MV base needs the accrued: a position without it is not funded on a guess
    clean = {p.position_id for p in [*inp.current, *(inp.previous or [])] if p.position_id in inp.unknown_accrued
             and pol.base_by_book_type.get(p.book_type) is F.FundingBase.MV_CLEAN}
    for pid in sorted(clean):
        problems.append(F.FundingProblem(F.LineKind.POS, pid, "НКД неизвестен, база — чистая MV"))
    cur = [p for p in inp.current if p.position_id not in clean]
    prv = None if inp.previous is None else [p for p in inp.previous if p.position_id not in clean]
    fixings = fixing_table([prev, market])
    res = F.compute_funding(period, rules, fixings, fx_table([prev, market]), positions=cur,
                            previous_positions=prv, cash=inp.cash, previous_cash=inp.previous_cash,
                            htm_lots=inp.lots, entities=entities, policy=pol)
    current_ids = {p.position_id for p in inp.current}
    ours = {(p.kind, p.ref) for p in problems}
    for p in res.problems:  # the engine's "cost book without a lot" of a closed position, or one we already said
        if p.kind is F.LineKind.HTM and p.ref not in current_ids:
            continue
        if p.kind is F.LineKind.HTM and (p.kind, p.ref) in ours:
            continue
        problems.append(p)
    # books funded on their cash that have no balance in the snapshot the policy reads
    snap = inp.previous_cash if pol.cash_base_date is F.BaseDate.PREVIOUS else inp.cash
    with_cash = {c.book_id for c in snap or ()}
    with_cash |= {p.ref.split("|", 1)[0] for p in problems if p.kind is F.LineKind.CASH and "|" in p.ref}  # said
    for b in sorted(run_book_ids | {c.book_id for c in inp.cash}):
        m = books.get(b) or {}
        etype = (entities or {}).get(m.get("entity") or "", F.EntityType.MAIN) if entities else F.EntityType.MAIN
        cash_funded = etype is F.EntityType.OTHER or pol.base_by_book_type.get(m.get("book_type") or "") \
            is F.FundingBase.CASH
        if cash_funded and b not in with_cash and not (snap is None and inp.cash):  # no snapshot: the engine says
            problems.append(F.FundingProblem(F.LineKind.CASH, b, "книга фондируется по остатку ДС, а остатков "
                                                                 "cash_balances нет"))
    # per position
    has_pnl = {r["position_id"] for r in content_rows if r.get("pnl")}
    lines_json, per_pos, pnl_funding = [], {}, {}
    by_pos: dict[str, list[F.FundingLine]] = defaultdict(list)
    for x in res.lines:
        in_pos = x.position_id in current_ids and x.position_id in has_pnl
        note = "" if in_pos or x.position_id is None else (
            "позиция без флэш-PnL: строкой книги" if x.position_id in current_ids else "позиция закрыта: строкой книги")
        lines_json.append(_line_json(x, in_pos, note))
        if x.position_id in current_ids:
            by_pos[x.position_id].append(x)
    prob_by_ref: dict[str, list[str]] = defaultdict(list)
    for p in problems:
        prob_by_ref[p.ref].append(p.reason)
    prev_ids = None if inp.previous is None else {p.position_id for p in inp.previous}
    for r in content_rows:
        pid = r["position_id"]
        m = books.get(r["book_id"]) or {}
        bt = m.get("book_type") or ""
        base = pol.base_by_book_type.get(bt)
        if by_pos.get(pid):
            amt = math.fsum(x.interest_reporting for x in by_pos[pid])
            st = {"state": FUNDED, "amount_rub": amt, "in_pnl": pid in has_pnl,
                  "lines": [{"kind": x.kind.value, "base": x.base.value, "rate_expr": x.rate_expr,
                             "rate_pct": x.days[-1].rate * 100, "ccy": x.ccy, "body_ccy": x.days[-1].body,
                             "days": len(x.days), "interest_ccy": x.interest, "interest_rub": x.interest_reporting}
                            for x in by_pos[pid]]}
            if pid in has_pnl:
                pnl_funding[pid] = amt
        elif pid in prob_by_ref:
            st = {"state": PROBLEM, "amount_rub": None, "reason": "; ".join(prob_by_ref[pid])}
        else:
            etype = (entities or {}).get(m.get("entity") or "") if entities else F.EntityType.MAIN
            mv_base = base in (F.FundingBase.MV_CLEAN, F.FundingBase.MV_DIRTY)
            if r.get("instr_type") == "BOND" and mv_base and etype is F.EntityType.MAIN \
                    and pol.new_positions is F.NewPosition.NEXT_PERIOD and (prev_ids is None or pid not in prev_ids):
                st = {"state": DEFERRED, "amount_rub": 0.0,
                      "reason": "новая позиция: фондируется со следующего периода (new_positions=next_period)"}
            else:
                why = ("юрлицо фондируется по всему остатку ДС" if etype is F.EntityType.OTHER
                       else f"тип книги {bt or '—'}: база {base.value if base else '—'}" if r.get("instr_type") == "BOND"
                       else "не облигация: фондируется по правилам книги (остатки ДС), а не по MV")
                st = {"state": NOT_FUNDED, "amount_rub": 0.0, "reason": why}
            if pid in has_pnl:
                pnl_funding[pid] = 0.0
        per_pos[pid] = st
    # by book, entity, day
    by_book: dict[str, dict[str, Any]] = {}
    daily: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for x, j in zip(res.lines, lines_json, strict=True):
        b = by_book.setdefault(x.book_id, {"total": 0.0, "in_positions": 0.0, "book_level": 0.0, "lines": 0,
                                           "by_kind": {}})
        b["total"] += x.interest_reporting
        b["in_positions" if j["in_position"] else "book_level"] += x.interest_reporting
        b["lines"] += 1
        b["by_kind"][x.kind.value] = b["by_kind"].get(x.kind.value, 0.0) + x.interest_reporting
        for d in x.days:
            daily[x.book_id][d.day.isoformat()] += d.interest_reporting
    for b in run_book_ids:
        by_book.setdefault(b, {"total": 0.0, "in_positions": 0.0, "book_level": 0.0, "lines": 0, "by_kind": {}})
    book_of = {r["position_id"]: r["book_id"] for r in content_rows}
    nprob = Counter(_problem_book(p, book_of) for p in problems)
    last_wo: dict[str, date | None] = {}
    for b, v in by_book.items():
        m = books.get(b) or {}
        etype = (entities or {}).get(m.get("entity") or "", F.EntityType.MAIN) if entities else F.EntityType.MAIN
        per = pol.write_off.periodicity(m.get("book_type") or "", m.get("entity") or "", etype)
        last = F.last_write_off(on, per, pol.write_off.date_rule) if per is not None else None
        last_wo[b] = last
        v.update({"entity": m.get("entity"), "book_type": m.get("book_type"),
                  "base": (pol.base_by_book_type.get(m.get("book_type") or "") or None),
                  "problems": nprob.get(b, 0),
                  "write_off": {"periodicity": per, "last": last, "since_write_off": None}})
    hist = _since_write_off(con, on, {b: d for b, d in last_wo.items() if d is not None})
    for b, v in by_book.items():
        if (last := last_wo.get(b)) is None:
            continue
        days = {**hist.get(b, {}), **daily.get(b, {})}
        v["write_off"]["since_write_off"] = math.fsum(a for d, a in days.items() if d > last.isoformat())
    by_entity: dict[str, float] = defaultdict(float)
    for x in res.lines:
        by_entity[x.entity] += x.interest_reporting
    total = math.fsum(x.interest_reporting for x in res.lines)
    in_pos = math.fsum(j["interest_rub"] for j in lines_json if j["in_position"])
    source = dict(settings.source)
    for bt in pol.base_by_book_type:
        source.setdefault(f"base_by_book_type[{bt}]", "funding_rules" if bt in {
            r.get("book_type") for r in rule_rows if r.get("basis")} else "default")
    provisional = _provisional(source, rule_rows, entity_rows)
    block = _empty_block(
        COMPUTED, STATUS_TEXT[COMPUTED], period,
        totals={"total": total, "in_positions": in_pos, "book_level": total - in_pos, "lines": len(res.lines),
                "positions_funded": sum(1 for s in per_pos.values() if s["state"] == FUNDED)},
        by_book=by_book, by_entity=dict(by_entity), lines=lines_json,
        problems=[{"kind": p.kind.value, "ref": p.ref, "reason": p.reason,
                   "book_id": _problem_book(p, book_of)} for p in problems],
        notes=[*inp.notes, *res.notes],
        policy={**pol.lineage(), "source": source, "provisional": provisional},
        rules=rule_rows, daily_by_book={b: dict(d) for b, d in daily.items()},
        inputs={"previous_run_id": inp.previous_run_id, "cash_units": inp.cash_units, "htm_lots": len(inp.lots),
                "entities": len(entity_rows), "funding_policy": len(policy_rows),
                "fixings": list(fixings.indices)})
    block = _jsonable(block)
    log = [("INFO", f"фондирование {period.start} -> {on} ({len(period.days)} дн.): итог {total:,.0f} ₽, в PnL позиций "
                    f"{in_pos:,.0f} ₽ ({len(pnl_funding)} поз.), строками книг {total - in_pos:,.0f} ₽; строк "
                    f"{len(res.lines)}, проблем {len(problems)}; по умолчанию (provisional): "
                    + (", ".join(provisional) or "нет"))]
    log += [("WARN", f"фондирование: {p.kind.value} {p.ref}: {p.reason}") for p in problems[:LOG_PROBLEMS]]
    if len(problems) > LOG_PROBLEMS:
        log.append(("WARN", f"фондирование: ещё {len(problems) - LOG_PROBLEMS} проблем (в блоке funding прогона)"))
    log += [("INFO", f"фондирование: {n}") for n in block["notes"]]
    return RunFunding(COMPUTED, block, _jsonable(per_pos), pnl_funding, log)


def _problem_book(p: F.FundingProblem, book_of: dict[str, str]) -> str | None:
    """The book a problem is about: its position's, its cash line's, else none."""
    if p.ref in book_of:
        return book_of[p.ref]
    if "|" in p.ref:
        return p.ref.split("|", 1)[0]
    return p.ref if p.kind is F.LineKind.CASH and p.ref != "*" else None


def _provisional(source: dict[str, str], rule_rows: list[dict], entity_rows: list[dict]) -> list[str]:
    """The ``OPEN_DECISIONS`` fields the run took at the engine's provisional default (or left unset): not given by
    ``funding_policy``, the rules' ``basis`` / ``day_count`` or the entities' ``pnl_sweep``."""
    out = []
    active = [r for r in rule_rows if r.get("active", True) is not False]
    for d in F.OPEN_DECISIONS:
        f = d.field
        if f == "base_by_book_type['TRADING']":
            given = source.get("base_by_book_type[TRADING]") == "funding_rules"
        elif f == "base_by_book_type":
            given = all(v == "funding_rules" for k, v in source.items() if k.startswith("base_by_book_type["))
        elif f == "day_count":
            given = all(r.get("day_count") for r in active if r.get("rate_kind") in ("index_spread", "fixed"))
        elif f == "write_off":
            given = any(k.startswith("write_off.") for k in source) or any(e.get("pnl_sweep") for e in entity_rows)
        else:
            given = f in source
        if not given:
            out.append(f)
    return out
