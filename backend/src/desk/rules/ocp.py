"""The open currency position (ОВП, OCP) from positions, its reconciliation with the reported OCP, and the OCP
transfers (wave B2, TDM#59; ROADMAP §0 step 3). Generic mechanics only: which components the bank's OCP report
holds, the amount basis, NDFs and the transfer convention are owner decisions (DEC06, OQ-11, DEC12; OQ-04 is
decided, TDM#54) and the organization's answers live in the private adapter. Until they arrive, every such choice is a field of
``OcpPolicy`` with a **provisional** default listed in ``OPEN_DECISIONS``; the run says which ones it took.

**Lines.** Each published row of the run with an FX exposure gives one ``OcpLine`` per foreign currency (the
reporting currency has no OCP), in one component (``COMPONENTS``):

- ``cash`` — cash and to be settled: the ``cash_balances`` in force (``OcpPolicy.cash_from_balances``; the known
  balance of ``desk.cash`` per book and currency, a ledger break left out with a note) and the FX
  rows valued without core whose value date is none or within ``settle_window_bd`` business days (a balance, a
  TOD/TOM/spot deal not settled yet): the row's amount;
- ``fx_legs`` — PV of the currency legs: an FX derivative valued by core from its terms (forward, swap, NDF,
  cross) at its ``fx_delta`` (core's spot equivalent) or, with ``amount_basis`` ``nominal``, at its
  ``extra.ocp_amount``; an FX row without terms and a later value date at its amount (basis ``notional``);
  an NDF only with ``include_ndf``;
- ``futures`` — an FX future valued by core (product ``fx_future``) at its FX delta;
- ``bonds`` — a foreign-currency bond: its FX delta. A run since TDM#54 publishes it net of the bond's currency
  funding liability (``publish.fx_liability``: the dirty MV in the currency less the clean MV and the accrued paid at
  the purchase, OQ-04, DEC01, owner's rule of 26.10.2026; basis ``mv_net``): what the bond book keeps of FX before the
  OCP transfer. A bond without a liability (funding not configured, a problem, a book not funded on its MV) keeps its
  dirty MV (basis ``mv``);
- ``funding`` — the currency funding liability as a component of its own (``funding.funding_liabilities``;
  ``bond_funding_liability``, off by default): only for rows published gross (a run before TDM#54). A row already net
  of its liability (``extra.fx_liability`` computed) never gets it again: the liability is subtracted once;
- ``other`` — any other row with an FX delta (an OTC or other row in a foreign currency at the report's value);
- ``transfers`` — an OCP transfer deal (``transfer_rule``): its amount, apart from the FX legs.

**Derived OCP** per (entity, book, currency) is the sum of its lines, per component.

**Reconciliation** at the level the report has (08 §10 ``ocp``): a report unit whose scope names a book covers
that book; a unit of an entity covers the entity's books. Its rows with a ``book_id`` are compared per book, rows
without per entity over the entity's books no row names (an entity unit naming books without such rows: its other
books per book too). A currency on one side only is 0 on the other (``full_snapshot``; a covered unit without rows
is an OCP of 0 in every currency). For each (scope, currency): ``reported`` (``ocp_ccy``, signed, ``+`` long),
``derived`` by component, ``in_report`` = the components ``report_components`` says the report holds, and
``break`` = reported - in_report, ``over`` when ``|break| > tolerance_ccy``. The components outside the report are
the explained part of reported vs the full derived OCP; ``hints`` name a component (or two) whose omission or
inclusion would close a break, and a sign flip. Derived OCP of books no report unit covers is listed apart
(``outside_report``), never a break.

**Transfers** (DEC06): a deal is an OCP transfer under ``TransferRule.INTERNAL_COUNTERPARTY`` when its trade terms
name as ``counterparty_id`` the book's ``ocp_to`` or a book whose ``ocp_to`` is this book. Per book that has an
``ocp_to`` or a transfer deal, per currency: ``before`` (the book's derived OCP without the transfers),
``transferred`` (the deals), ``after`` = before + transferred and, under ``transfer_target`` ``full``, ``expected``
= -before and ``gap`` = after (what the transfer left). The monitor never invents a transfer: rate and value date
are the deals' own (``contract_rate``, ``value_date``), none when the terms do not give them.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from itertools import combinations
from typing import Any

from .. import cash
from .funding import FundingLiability, OpenDecision

COMPONENTS = ("cash", "fx_legs", "futures", "bonds", "funding", "other", "transfers")
COMPONENT_TEXT = {
    "cash": "кэш и к расчёту", "fx_legs": "PV валютных ног", "futures": "фьючерсы", "bonds": "облигации",
    "funding": "пассив фондирования", "other": "прочее", "transfers": "перенос ОВП",
}
"""Component -> its name on the pages and in the run log."""


class AmountBasis(str, Enum):
    SPOT_EQUIVALENT = "spot_equivalent"  # core's fx_delta: the legs' PV in the currency (discounted)
    NOMINAL = "nominal"  # extra.ocp_amount: the legs' nominal per currency


class TransferRule(str, Enum):
    INTERNAL_COUNTERPARTY = "internal_counterparty"  # the deal's counterparty is the book's ocp_to (or its source)
    NONE = "none"  # no deal is a transfer


class TransferTarget(str, Enum):
    FULL = "full"  # the transfer takes the book's whole OCP: expected = -before
    NONE = "none"  # no expectation: before, transferred and after only


@dataclass(frozen=True)
class OcpPolicy:
    """Every convention of the OCP. A default is **provisional** (``OPEN_DECISIONS``): a neutral generic choice
    standing for an owner decision not taken yet; the decision (and later the private adapter) replaces it."""

    amount_basis: AmountBasis = AmountBasis.SPOT_EQUIVALENT
    include_ndf: bool = True
    report_components: frozenset[str] = frozenset(COMPONENTS)
    cash_from_balances: bool = True
    settle_window_bd: int = 2
    bond_funding_liability: bool = False
    """The liability as the ``funding`` component. Off: the run's bond rows carry it already (``publish.fx_liability``,
    TDM#54); a row net of it is skipped even when on (``compute_ocp``), so it is never subtracted twice."""
    transfer_rule: TransferRule = TransferRule.INTERNAL_COUNTERPARTY
    transfer_target: TransferTarget = TransferTarget.FULL
    tolerance_ccy: float = 1000.0
    reporting_ccy: str = "RUB"

    def __post_init__(self) -> None:
        bad = set(self.report_components) - set(COMPONENTS)
        if bad:
            raise ValueError("unknown OCP components: " + ", ".join(sorted(bad)))

    def lineage(self) -> dict[str, Any]:
        d = asdict(self)
        d["report_components"] = [c for c in COMPONENTS if c in self.report_components]
        return {k: v.value if isinstance(v, Enum) else v for k, v in d.items()}


PROVISIONAL_POLICY = OcpPolicy()

OPEN_DECISIONS: tuple[OpenDecision, ...] = (
    OpenDecision("amount_basis", "OQ-11, TDM#60", "The OCP of an FX derivative: nominal or spot equivalent",
                 "spot_equivalent (core's fx_delta, as the FX delta of the run)", ("nominal",)),
    OpenDecision("include_ndf", "OQ-11, TDM#60", "Does an NDF enter the OCP",
                 "true (as every FX derivative)", ("false",)),
    OpenDecision("report_components", "TDM#37 (private adapter)", "Which components the bank's OCP report holds",
                 "all of them: every difference is a break", ("any subset of COMPONENTS",)),
    OpenDecision("cash_from_balances", "TDM#36", "Do the cash balances enter the OCP beside the FX rows of positions",
                 "true", ("false: the positions' FX rows already hold the balances",)),
    OpenDecision("settle_window_bd", "TDM#36", "Up to which value date an FX row is cash to be settled, not a forward",
                 "2 business days (TOD, TOM, spot)", ("0", "1")),
    OpenDecision("transfer_rule", "DEC06, TDM#60", "Which deals are OCP transfers",
                 "internal_counterparty (the deal's counterparty is the book's ocp_to)", ("none",)),
    OpenDecision("transfer_target", "DEC06, TDM#60", "What the evening transfer should move",
                 "full (the book's whole OCP before the transfer)", ("none",)),
    OpenDecision("tolerance_ccy", "DEC12", "OCP break threshold, units of the currency",
                 "1000 (informational, not an approved limit)", ("any",)),
)


@dataclass(frozen=True)
class OcpLine:
    """One currency amount of the derived OCP. ``basis``: spot_equivalent / nominal / notional / amount /
    balance / liability."""

    key: str
    component: str
    book_id: str
    entity: str
    ccy: str
    amount: float
    position_id: str | None = None
    basis: str = "amount"
    note: str = ""

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def _f(x: Any) -> float | None:
    try:
        return None if x is None or x == "" else float(Decimal(str(x)))
    except (ArithmeticError, ValueError):
        return None


def _day(x: Any) -> date | None:
    if x is None or isinstance(x, date):
        return x
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def add_business_days(d: date, n: int) -> date:
    """``d`` plus ``n`` weekdays (weekends only: the settlement calendars are the hub's, not needed for a window)."""
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _truthy(x: Any) -> bool:
    return x is True or str(x).strip().lower() in ("1", "true", "yes", "y")


def terms_by_position(rows: Sequence[Mapping[str, Any]], terms: Iterable[Mapping[str, Any]]
                      ) -> dict[str, list[Mapping[str, Any]]]:
    """position_id -> its ``trade_terms`` rows: by the terms' ``position_id``, the row's ``deal_id`` or the trades
    core valued it from (``extra.core.trades``)."""
    by_trade: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_pos: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for t in terms:
        by_trade[str(t.get("trade_id"))].append(t)
        if t.get("position_id"):
            by_pos[str(t["position_id"])].append(t)
    out: dict[str, list[Mapping[str, Any]]] = {}
    for r in rows:
        pid = r["position_id"]
        ex = r.get("extra") or {}
        ids = [*((ex.get("core") or {}).get("trades") or []), (ex.get("reported") or {}).get("deal_id")]
        found = list(by_pos.get(pid, []))
        for i in ids:
            if i is not None:
                found += [t for t in by_trade.get(str(i), []) if t not in found]
        if found:
            out[pid] = found
    return out


def transfer_partners(book: str, books: Mapping[str, Mapping[str, Any]]) -> set[str]:
    """The books an OCP transfer of ``book`` can face: its ``ocp_to`` and every book whose ``ocp_to`` it is."""
    out = {str(b) for b, m in books.items() if m.get("ocp_to") == book}
    if (to := (books.get(book) or {}).get("ocp_to")):
        out.add(str(to))
    return out


def is_transfer(row: Mapping[str, Any], terms: Sequence[Mapping[str, Any]], books: Mapping[str, Mapping[str, Any]],
                policy: OcpPolicy) -> bool:
    if policy.transfer_rule is TransferRule.NONE or not terms:
        return False
    partners = transfer_partners(row["book_id"], books)
    return bool(partners) and any(str(t.get("counterparty_id") or "") in partners for t in terms)


def _amounts(x: Mapping[str, Any] | None, policy: OcpPolicy) -> dict[str, float]:
    """A per-currency amount map without the reporting currency, none and non-finite values."""
    out = {}
    for c, v in (x or {}).items():
        f = _f(v)
        if c != policy.reporting_ccy and f is not None and math.isfinite(f):
            out[c] = f
    return out


def classify(row: Mapping[str, Any], terms: Sequence[Mapping[str, Any]], books: Mapping[str, Mapping[str, Any]],
             on: date, policy: OcpPolicy) -> tuple[str | None, dict[str, float], str, str]:
    """(component, amount per currency, basis, note) of a published row; component None: no OCP (a RUB row, an
    NDF left out)."""
    ex = row.get("extra") or {}
    delta = _amounts(row.get("fx_delta"), policy)
    core = ex.get("core") or {}
    product = str(core.get("product") or "")
    ndf = "fx_ndf" in product or any(_truthy(t.get("ndf")) for t in terms)
    if core:
        basis = policy.amount_basis.value
        if policy.amount_basis is AmountBasis.NOMINAL:
            delta = _amounts(ex.get("ocp_amount"), policy)
    elif row.get("instr_type") == "BOND":
        basis = "mv_net" if _net_of_liability(row) else "mv"
    else:
        basis = "notional" if ex.get("fx_delta_basis") else "amount"
    if not delta:
        return None, {}, basis, ""
    if is_transfer(row, terms, books, policy):
        return "transfers", delta, basis, "сделка переноса ОВП: контрагент — книга переноса (ocp_to)"
    if ndf and not policy.include_ndf:
        return None, {}, basis, "NDF не входит в ОВП (include_ndf = false)"
    if row.get("instr_type") == "BOND":
        return "bonds", delta, basis, ("грязная MV за вычетом валютного пассива фондирования" if basis == "mv_net"
                                       else "")
    if core:
        return ("futures" if "fx_future" in product else "fx_legs"), delta, basis, ""
    kind = str((ex.get("reported") or {}).get("asset_class") or "").lower()
    if row.get("instr_type") == "FX" or kind == "fx":
        vd = _day((ex.get("reported") or {}).get("value_date"))
        if vd is None or vd <= add_business_days(on, policy.settle_window_bd):
            return "cash", delta, "amount", ""
        return "fx_legs", delta, "notional", "без условий сделки: номинал, не PV"
    return "other", delta, basis, ""


def _net_of_liability(row: Mapping[str, Any]) -> bool:
    """The row's FX delta is net of its currency funding liability (``publish.fx_liability``)."""
    return ((row.get("extra") or {}).get("fx_liability") or {}).get("state") == "computed"


def position_lines(rows: Sequence[Mapping[str, Any]], books: Mapping[str, Mapping[str, Any]],
                   terms: Mapping[str, Sequence[Mapping[str, Any]]], on: date, policy: OcpPolicy = PROVISIONAL_POLICY
                   ) -> tuple[list[OcpLine], dict[str, dict[str, Any]], list[str]]:
    """The OCP lines of the run's rows, per position its ``extra.ocp`` and the notes (rows left out)."""
    lines, per_pos, notes = [], {}, []
    for r in rows:
        pid = r["position_id"]
        comp, amounts, basis, note = classify(r, terms.get(pid, ()), books, on, policy)
        if comp is None:
            if note:
                notes.append(f"{pid}: {note}")
            continue
        entity = str((books.get(r["book_id"]) or {}).get("entity") or "—")
        for ccy, a in sorted(amounts.items()):
            if a:
                lines.append(OcpLine(pid, comp, r["book_id"], entity, ccy, a, pid, basis, note))
        per_pos[pid] = {"component": comp, "amounts": amounts, "basis": basis,
                        **({"transfer": True} if comp == "transfers" else {})}
    return lines, per_pos, notes


def cash_lines(cash_rows: Iterable[Mapping[str, Any]], policy: OcpPolicy = PROVISIONAL_POLICY
               ) -> tuple[list[OcpLine], list[str]]:
    """The ``cash_balances`` rows in force in foreign currencies as one line per (entity, book, currency): its
    *known* balance by ``desk.cash.compare`` (the same balance reported by several ledgers counts once: their common
    value). A balance whose ledgers disagree (``break``) has no known value: it is left out and said in the notes
    (conservative default of ``desk.cash``, never the larger, the smaller or the first ledger)."""
    if not policy.cash_from_balances:
        return [], []
    rows = [r for r in cash_rows if r.get("ccy") and r.get("ccy") != policy.reporting_ccy
            and _f(r.get("balance_ccy")) is not None]
    lines, notes = [], []
    for b in cash.compare(rows):
        if (known := b.known) is None:
            notes.append(f"остаток {b.book_id} {b.ccy} не включён в ОВП: системы учёта расходятся ("
                         + ", ".join(b.ledgers()) + ")")
            continue
        if known:
            n = len(b.by_ledger)
            lines.append(OcpLine(f"CASH|{b.book_id}|{b.ccy}", "cash", b.book_id, b.entity, b.ccy, float(known), None,
                                 "balance", "остаток на счетах" + (f" ({n} системы учёта сходятся)" if n > 1 else "")))
    return lines, notes


def liability_lines(liabilities: Iterable[FundingLiability], policy: OcpPolicy = PROVISIONAL_POLICY
                    ) -> list[OcpLine]:
    if not policy.bond_funding_liability:
        return []
    return [OcpLine("FUND|" + x.position_id, "funding", x.book_id, x.entity, x.ccy, x.amount, x.position_id,
                    "liability", "валютный пассив фондирования бумаги")
            for x in liabilities if x.ccy != policy.reporting_ccy and x.amount]


def derive(lines: Iterable[OcpLine]) -> dict[tuple[str, str, str], dict[str, float]]:
    """(entity, book, currency) -> component -> amount."""
    out: dict[tuple[str, str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for x in lines:
        out[(x.entity, x.book_id, x.ccy)][x.component] += x.amount
    return {k: dict(v) for k, v in out.items()}


def _sum(parts: Mapping[str, float], comps: Iterable[str] | None = None) -> float:
    return math.fsum(v for c, v in parts.items() if comps is None or c in comps)


def _hints(brk: float, reported: float, in_report: float, parts: Mapping[str, float], policy: OcpPolicy
           ) -> list[str]:
    """Why a break could be: a component the report may leave out or hold, two of them, a sign flip."""
    tol = policy.tolerance_ccy
    out = []
    if in_report and abs(reported + in_report) <= tol:
        out.append("знак противоположный: отчёт, похоже, считает длинную позицию со знаком минус")
    inside = [c for c in COMPONENTS if c in policy.report_components and abs(parts.get(c, 0.0)) > tol]
    outside = [c for c in COMPONENTS if c not in policy.report_components and abs(parts.get(c, 0.0)) > tol]
    for c in inside:
        if abs(brk + parts[c]) <= tol:
            out.append(f"без слагаемого «{COMPONENT_TEXT[c]}» сходится: отчёт, похоже, его не включает")
    for c in outside:
        if abs(brk - parts[c]) <= tol:
            out.append(f"со слагаемым «{COMPONENT_TEXT[c]}» сходится: отчёт, похоже, его включает")
    if not out:
        for a, b in combinations(inside, 2):
            if abs(brk + parts[a] + parts[b]) <= tol:
                out.append(f"без слагаемых «{COMPONENT_TEXT[a]}» и «{COMPONENT_TEXT[b]}» сходится")
    return out


def reconcile(lines: Sequence[OcpLine], report_rows: Sequence[Mapping[str, Any]],
              report_units: Sequence[Mapping[str, Any]], books: Mapping[str, Mapping[str, Any]],
              policy: OcpPolicy = PROVISIONAL_POLICY) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(comparisons, outside_report): see the module docstring."""
    derived = derive(lines)
    entity_of = {str(b): str(m.get("entity") or "—") for b, m in books.items()}
    for e, b, _ in derived:
        entity_of.setdefault(b, e)
    rows_by_set: dict[Any, list[Mapping[str, Any]]] = defaultdict(list)
    for r in report_rows:
        rows_by_set[r.get("_set_id")].append(r)
    out: list[dict[str, Any]] = []
    covered: set[str] = set()
    for u in report_units:
        scope = u.get("scope") or {}
        entity, book = scope.get("entity"), scope.get("book_id")
        rows = rows_by_set.get(u.get("set_id"), [])
        books_in = ({str(book)} if book else {b for b, e in entity_of.items() if e == str(entity)})
        covered |= books_in
        # rows with a book are compared per book; a unit of an entity whose rows have no book, per entity. A unit of
        # an entity with both kinds: its books named by a row per book, the entity's other books with the rows
        # without a book, at the entity level
        named = {str(r["book_id"]) for r in rows if r.get("book_id")}
        entity_rows = not book and any(not r.get("book_id") for r in rows)
        reported: dict[tuple[str | None, str], dict[str, Any]] = {}
        for r in rows:
            ccy, a = str(r.get("ccy") or ""), _f(r.get("ocp_ccy"))
            if not ccy or a is None:
                continue
            rb = r.get("book_id") or book
            key = (str(rb) if rb else None, ccy)
            x = reported.setdefault(key, {"ocp_ccy": 0.0, "ocp_rub": None, "rate": None, "src": []})
            x["ocp_ccy"] += a
            if (rub := _f(r.get("ocp_rub"))) is not None:
                x["ocp_rub"] = (x["ocp_rub"] or 0.0) + rub
            x["rate"] = _f(r.get("rate_rub_per_unit")) or x["rate"]
            if r.get("_src"):
                x["src"].append(r["_src"])
        parts_by: dict[tuple[str | None, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for (_, b, ccy), parts in derived.items():
            if b in books_in:
                at = b if (book or b in named or (named and not entity_rows)) else None
                for c, v in parts.items():
                    parts_by[(at, ccy)][c] += v
        for key in sorted(set(reported) | set(parts_by), key=lambda k: (str(k[0]), k[1])):
            b, ccy = key
            parts = {c: parts_by[key][c] for c in COMPONENTS if abs(parts_by[key].get(c, 0.0)) > 0}
            rep = reported.get(key, {"ocp_ccy": 0.0, "ocp_rub": None, "rate": None, "src": []})
            in_report = _sum(parts, policy.report_components)
            brk = rep["ocp_ccy"] - in_report
            over = abs(brk) > policy.tolerance_ccy
            out.append({
                "level": "book" if b is not None else "entity", "entity": str(entity or entity_of.get(b or "", "—")),
                "book_id": b, "ccy": ccy, "reported": rep["ocp_ccy"], "reported_rub": rep["ocp_rub"],
                "reported_rate": rep["rate"], "in_report_file": key in reported, "src": rep["src"][:20],
                "derived": _sum(parts), "components": parts, "in_report": in_report,
                "not_in_report": {c: v for c, v in parts.items() if c not in policy.report_components},
                "break": brk, "over": over,
                "hints": _hints(brk, rep["ocp_ccy"], in_report, parts, policy) if over else [],
                "set_id": u.get("set_id"), "valid_date": u.get("valid_date"),
            })
    outside = []
    for (e, b, ccy), parts in sorted(derived.items()):
        if b not in covered and abs(_sum(parts)) > 0:
            outside.append({"entity": e, "book_id": b, "ccy": ccy, "derived": _sum(parts), "components": parts})
    return out, outside


def transfers(lines: Sequence[OcpLine], books: Mapping[str, Mapping[str, Any]],
              terms: Mapping[str, Sequence[Mapping[str, Any]]], policy: OcpPolicy = PROVISIONAL_POLICY
              ) -> list[dict[str, Any]]:
    """Per book with an ``ocp_to`` or a transfer deal, per currency: before, transferred, after, expected, gap and
    the deals (see the module docstring)."""
    by: dict[tuple[str, str], dict[str, Any]] = {}
    deal_books = {x.book_id for x in lines if x.component == "transfers"}
    for x in lines:
        if not ((books.get(x.book_id) or {}).get("ocp_to") or x.book_id in deal_books):
            continue
        r = by.setdefault((x.book_id, x.ccy), {"before": 0.0, "transferred": 0.0, "deals": []})
        if x.component == "transfers":
            r["transferred"] += x.amount
            t = list(terms.get(x.position_id or "", ()))
            r["deals"].append({
                "position_id": x.position_id, "amount": x.amount,
                "trade_ids": sorted({str(v["trade_id"]) for v in t if v.get("trade_id")}),
                "counterparty": next((str(v["counterparty_id"]) for v in t if v.get("counterparty_id")), None),
                "rate": next((f for v in t if (f := _f(v.get("contract_rate"))) is not None), None),
                "value_date": next((str(v["value_date"]) for v in t if v.get("value_date")), None)})
        else:
            r["before"] += x.amount
    out = []
    for (b, ccy), r in sorted(by.items()):
        m = books.get(b) or {}
        after = r["before"] + r["transferred"]
        expected = -r["before"] if policy.transfer_target is TransferTarget.FULL else None
        cps = {d["counterparty"] for d in r["deals"] if d["counterparty"]}
        out.append({"book_id": b, "entity": m.get("entity"), "ocp_to": m.get("ocp_to") or (
            next(iter(cps)) if len(cps) == 1 else None), "ccy": ccy, "before": r["before"],
            "transferred": r["transferred"], "after": after, "expected": expected,
            "gap": after if expected is not None else None, "source": bool(m.get("ocp_to")),
            "over": expected is not None and abs(after) > policy.tolerance_ccy and bool(m.get("ocp_to")),
            "rate": next((d["rate"] for d in r["deals"] if d["rate"] is not None), None),
            "value_date": next((d["value_date"] for d in r["deals"] if d["value_date"]), None),
            "deals": r["deals"]})
    return out


def by_currency(lines: Iterable[OcpLine]) -> dict[str, dict[str, float]]:
    """currency -> component -> amount, over every book (the desk's OCP)."""
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for x in lines:
        out[x.ccy][x.component] += x.amount
    return {c: dict(v) for c, v in sorted(out.items())}


def provisional(policy: OcpPolicy, given: Iterable[str] = ()) -> list[str]:
    """The ``OPEN_DECISIONS`` fields not given by a decision (``given``): all of them until TDM#60."""
    g = set(given)
    return [d.field for d in OPEN_DECISIONS if d.field not in g]


@dataclass
class OcpResult:
    lines: list[OcpLine] = field(default_factory=list)
    per_position: dict[str, dict[str, Any]] = field(default_factory=dict)
    comparisons: list[dict[str, Any]] = field(default_factory=list)
    outside_report: list[dict[str, Any]] = field(default_factory=list)
    transfers: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def compute_ocp(rows: Sequence[Mapping[str, Any]], books: Mapping[str, Mapping[str, Any]], on: date, *,
                terms: Iterable[Mapping[str, Any]] = (), cash: Iterable[Mapping[str, Any]] = (),
                liabilities: Iterable[FundingLiability] = (), report_rows: Sequence[Mapping[str, Any]] = (),
                report_units: Sequence[Mapping[str, Any]] = (), policy: OcpPolicy = PROVISIONAL_POLICY) -> OcpResult:
    """The whole OCP of a run: lines, per position, the reconciliation with the report and the transfers."""
    tb = terms_by_position(rows, terms)
    lines, per_pos, notes = position_lines(rows, books, tb, on, policy)
    cl, cash_notes = cash_lines(cash, policy)
    net = {r["position_id"] for r in rows if _net_of_liability(r)}
    liabilities = list(liabilities)
    if policy.bond_funding_liability and (twice := sorted(x.position_id for x in liabilities if x.position_id in net)):
        notes.append("пассив фондирования уже вычтен из FX-дельты облигаций, второй раз не вычитается: "
                     + ", ".join(twice))
    lines += cl + liability_lines([x for x in liabilities if x.position_id not in net], policy)
    comps, outside = reconcile(lines, report_rows, report_units, books, policy)
    return OcpResult(lines, per_pos, comps, outside, transfers(lines, books, tb, policy), notes + cash_notes)
