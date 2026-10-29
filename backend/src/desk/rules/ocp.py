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
- ``fx_legs`` — PV of the deliverable currency legs: an FX derivative valued by core from its terms (forward, swap,
  cross) at its ``fx_delta`` (core's spot equivalent) or, with ``amount_basis`` ``nominal``, at its
  ``extra.ocp_amount``; an FX row without terms and a later value date at its amount (basis ``notional``);
- ``ndf`` — an NDF (core's ``fx_ndf``, the terms' ``ndf``, the report's product group ``fx_ndf``), amounted as the FX
  legs; only with ``include_ndf``;
- ``futures`` — an FX future (core's ``fx_future`` or the report's product group ``fx_future``) at its FX delta;
- ``options`` — an FX option (``option`` in core's product or the product group), at its FX delta or, without core,
  the report's amount;
- ``bonds`` — a foreign-currency bond: its FX delta. A run since TDM#54 publishes it net of the bond's currency
  OCP liability (``publish.fx_liability``, OQ-04, OQ-17, TDM#266; basis ``mv_net``): the dirty MV of today less the
  dirty MV at the last OCP transfer (the previous business day) plus the day's coupons, in the currency: the FX PnL
  since that transfer, what the bond book holds before today's transfer. A bond without a liability (funding not
  configured, a problem, a book not funded on its MV) keeps its dirty MV (basis ``mv``);
- ``funding`` — the currency funding liability as a component of its own (``funding.funding_liabilities``;
  ``bond_funding_liability``, off by default): only for rows published gross (a run before TDM#54). A row already net
  of its liability (``extra.fx_liability`` computed) never gets it again: the liability is subtracted once;
- ``other`` — any other row with an FX delta (an OTC or other row in a foreign currency at the report's value);
- ``transfers`` — an OCP transfer deal (``transfer_rule``): its amount, apart from the FX legs. It is its own
  component only, never in another one, so a bond book's OCP is Σ its bonds' FX deltas + its transfer deals (a
  transfer out is negative): the day's transfer is counted once and the book keeps the residual.

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

**Published OCP** (owner, 28.10.2026, TDM#416, ``published``): where a usable report unit covers a book or an entity,
the report's figure plus the components the report does not hold (``report_components``, from the adapter's
``ocp_policy``), derived from positions; a unit older than ``report_max_age_days`` and a book no unit covers are
derived. Overlapping units publish each book once. ``gaps`` say what is missing for it to be trusted, among them a
component added from positions that the reconciliation says the report already holds (``outside_double_count``).

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

from .funding import FundingLiability, OpenDecision

COMPONENTS = ("cash", "fx_legs", "ndf", "futures", "options", "bonds", "funding", "other", "transfers")
COMPONENT_TEXT = {
    "cash": "кэш и к расчёту", "fx_legs": "PV валютных ног", "ndf": "NDF", "futures": "фьючерсы",
    "options": "опционы", "bonds": "облигации", "funding": "пассив фондирования", "other": "прочее",
    "transfers": "перенос ОВП",
}
"""Component -> its name on the pages and in the run log."""

REPORT, DERIVED, DERIVED_STALE = "report", "derived", "derived_stale_report"
SOURCE_TEXT = {REPORT: "отчёт", DERIVED: "из позиций", DERIVED_STALE: "из позиций: отчёт устарел"}
"""The source of a published OCP figure (``published``) -> its name on the pages."""


class AmountBasis(str, Enum):
    SPOT_EQUIVALENT = "spot_equivalent"  # core's fx_delta: the legs' PV in the currency (discounted)
    NOMINAL = "nominal"  # extra.ocp_amount: the legs' nominal per currency


class TransferRule(str, Enum):
    INTERNAL_COUNTERPARTY = "internal_counterparty"  # the deal's counterparty is the book's ocp_to (or its source)
    NONE = "none"  # no deal is a transfer


class TransferTarget(str, Enum):
    FULL = "full"  # the transfer takes the book's whole OCP: expected = -before
    NONE = "none"  # no expectation: before, transferred and after only


class PublishedBasis(str, Enum):
    REPORT_PLUS_OUTSIDE = "report_plus_outside"  # the report where it covers + the components it does not hold
    DERIVED = "derived"  # every figure from positions (before TDM#416): the report only reconciled


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
    published: PublishedBasis = PublishedBasis.REPORT_PLUS_OUTSIDE
    """What the run publishes as the OCP (owner, 28.10.2026, TDM#416; ``DECIDED``): the bank's report plus the
    components outside it, derived where no report unit covers."""
    report_max_age_days: int = 0
    """A report unit carried forward from a ``valid_date`` more than this many calendar days before the run's date is
    not published: its books fall back to the derived OCP (``derived_stale_report``)."""

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
    OpenDecision("report_max_age_days", "TDM#416", "How old a carried-forward OCP report may be and still be published",
                 "0 (only the report of the date; an older one: the derived OCP, said)", ("any number of days",)),
)

DECIDED: dict[str, str] = {
    "published": "OQ-18 (владелец, 28.10.2026, TDM#416)",
    "bond_funding_liability": "OQ-04 (владелец, 23.10.2026, TDM#54)",
}
"""Policy fields an owner decision sets (``docs/program/DECISIONS.md``): never provisional, the private adapter's
``ocp_policy`` may still give the organisation's own value."""


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


def product_group(rep: Any) -> str:
    """The report's ``x_product_group`` of a row (``extra.reported``, its ``x_*`` flat or under ``x``), lower case;
    "" without one (as ``publish.summary.product_group``)."""
    if not isinstance(rep, Mapping):
        return ""
    pg = rep.get("x_product_group")
    if pg is None and isinstance(rep.get("x"), Mapping):
        pg = rep["x"].get("x_product_group")
    return str(pg or "").lower()


def classify(row: Mapping[str, Any], terms: Sequence[Mapping[str, Any]], books: Mapping[str, Mapping[str, Any]],
             on: date, policy: OcpPolicy) -> tuple[str | None, dict[str, float], str, str]:
    """(component, amount per currency, basis, note) of a published row; component None: no OCP (a RUB row, an
    NDF left out). An NDF (core's ``fx_ndf``, the terms' ``ndf``, the report's product group ``fx_ndf``), an option
    (``option`` in core's product or the product group) and an FX future (core's ``fx_future`` or the product group
    ``fx_future``) are components of their own, so that the adapter's ``ocp_policy`` can place each inside or outside
    the report on its own (TDM#416)."""
    ex = row.get("extra") or {}
    rep = ex.get("reported") or {}
    delta = _amounts(row.get("fx_delta"), policy)
    core = ex.get("core") or {}
    product, group = str(core.get("product") or "").lower(), product_group(rep)
    ndf = "fx_ndf" in product or group == "fx_ndf" or any(_truthy(t.get("ndf")) for t in terms)
    own = "options" if "option" in product or "option" in group else \
        "futures" if "fx_future" in product or group == "fx_future" else None
    kind = str(rep.get("asset_class") or "").lower()
    fx_like = row.get("instr_type") == "FX" or kind == "fx"
    vd = _day(rep.get("value_date"))
    later = vd is not None and vd > add_business_days(on, policy.settle_window_bd)
    if core:
        basis = policy.amount_basis.value
        if policy.amount_basis is AmountBasis.NOMINAL:
            delta = _amounts(ex.get("ocp_amount"), policy)
    elif row.get("instr_type") == "BOND":
        basis = "mv_net" if _net_of_liability(row) else "mv"
    else:
        basis = "notional" if ex.get("fx_delta_basis") or (fx_like and later and (ndf or own)) else "amount"
    if not delta:
        return None, {}, basis, ""
    if is_transfer(row, terms, books, policy):
        return "transfers", delta, basis, "сделка переноса ОВП: контрагент — книга переноса (ocp_to)"
    if ndf and not policy.include_ndf:
        return None, {}, basis, "NDF не входит в ОВП (include_ndf = false)"
    if row.get("instr_type") == "BOND":
        return "bonds", delta, basis, ("грязная MV за вычетом валютного пассива фондирования" if basis == "mv_net"
                                       else "")
    note = "" if core or basis != "notional" else "без условий сделки: номинал, не PV"
    if ndf:
        return "ndf", delta, basis, note
    if own:
        return own, delta, basis, note
    if core:
        return "fx_legs", delta, basis, ""
    if fx_like:
        if not later:
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
    # here, not at the top: desk.cash -> store -> settings -> funding_settings -> rules.reference imports this module
    # (a cycle when desk.settings is imported first)
    from .. import cash

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
        at_of = {b: b if (book or b in named or (named and not entity_rows)) else None for b in books_in}
        books_at: dict[str | None, set[str]] = defaultdict(set)
        for b in books_in:
            books_at[at_of[b]].add(b)
        parts_by: dict[tuple[str | None, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for (_, b, ccy), parts in derived.items():
            if b in books_in:
                for c, v in parts.items():
                    parts_by[(at_of[b], ccy)][c] += v
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
                "set_id": u.get("set_id"), "valid_date": u.get("valid_date"), "scope": dict(scope),
                "books": sorted(books_at.get(b, set()) | ({b} if b else set())),
            })
    outside = []
    for (e, b, ccy), parts in sorted(derived.items()):
        if b not in covered and abs(_sum(parts)) > 0:
            outside.append({"entity": e, "book_id": b, "ccy": ccy, "derived": _sum(parts), "components": parts})
    return out, outside


def _stale(valid_date: Any, on: date, policy: OcpPolicy) -> bool:
    """A report unit too old to publish: carried forward from more than ``report_max_age_days`` before ``on``."""
    vd = _day(valid_date)
    return vd is not None and (on - vd).days > policy.report_max_age_days


def _outside(parts: Mapping[str, float], policy: OcpPolicy) -> dict[str, float]:
    return {c: v for c, v in parts.items() if c not in policy.report_components and v}


def published(comparisons: Sequence[Mapping[str, Any]], outside_report: Sequence[Mapping[str, Any]],
              lines: Sequence[OcpLine], on: date, policy: OcpPolicy = PROVISIONAL_POLICY
              ) -> tuple[list[dict[str, Any]], list[str]]:
    """The published OCP (owner, 28.10.2026, TDM#416): (lines, notes). A line per (entity, book or None for an
    entity-level report unit, currency): ``amount``, ``source`` and ``components``, the components outside the report
    added to it, with ``reported`` (the report's figure, or None) and ``books`` (the books whose OCP it holds).

    - A report scope (a comparison of ``reconcile``) whose unit is usable: ``reported`` + the components the report
      does not hold (``report_components``), ``source`` ``report``. A unit older than ``report_max_age_days``:
      the scope's derived OCP, ``derived_stale_report``.
    - Overlapping units (an entity unit and a unit of one of its books, TDM#214): each book is published once, by the
      scope that wins it: a usable unit before a stale one, a book's own scope before the entity's, then the newer
      ``valid_date`` and the later set. An entity-level figure is taken as the entity's total: the report figure of a
      book another scope wins is subtracted from it (said in the notes).
    - A book no report unit covers (``outside_report``): its derived OCP, ``derived``.
    - ``published`` ``derived``: every line derived (the reconciliation is unchanged)."""
    derived = derive(lines)
    out: list[dict[str, Any]] = []
    notes: list[str] = []

    def line(entity: str, book: str | None, books: Iterable[str], ccy: str, amount: float, source: str,
             reported: float | None = None, added: Mapping[str, float] | None = None,
             valid_date: Any = None) -> None:
        if ccy != policy.reporting_ccy and (amount or reported is not None):  # the reporting currency has no OCP
            out.append({"entity": entity, "book_id": book, "books": sorted(books), "ccy": ccy, "amount": amount,
                        "source": source, "components": [c for c in COMPONENTS if (added or {}).get(c)],
                        "outside": {c: v for c, v in (added or {}).items() if v}, "reported": reported,
                        "valid_date": valid_date})

    if policy.published is PublishedBasis.DERIVED:
        for (e, b, ccy), parts in sorted(derived.items()):
            line(e, b, [b], ccy, _sum(parts), DERIVED)
        return out, notes
    # the scopes: (set_id, book or None) -> its comparisons per currency, its books, its unit's state
    scopes: dict[tuple[Any, str | None], dict[str, Any]] = {}
    for c in comparisons:
        sc = scopes.setdefault((c.get("set_id"), c.get("book_id")), {
            "entity": c["entity"], "book_id": c.get("book_id"), "books": set(), "by_ccy": {},
            "stale": _stale(c.get("valid_date"), on, policy), "valid_date": c.get("valid_date"),
            "set_id": c.get("set_id"), "scope": c.get("scope") or {}})
        sc["books"] |= set(c.get("books") or ([c["book_id"]] if c.get("book_id") else []))
        sc["by_ccy"][c["ccy"]] = c

    def rank(key: tuple[Any, str | None]) -> tuple:
        sc = scopes[key]
        sid = sc["set_id"] if isinstance(sc["set_id"], int) else -1
        return (not sc["stale"], sc["book_id"] is not None, _day(sc["valid_date"]) or date.min, sid)

    winner: dict[str, tuple[Any, str | None]] = {}
    for key, sc in scopes.items():
        for b in sc["books"]:
            if b not in winner or rank(key) > rank(winner[b]):
                winner[b] = key
    entity_of = {b: e for (e, b, _) in derived}
    for key, sc in sorted(scopes.items(), key=lambda kv: (str(kv[0][1]), str(kv[0][0]))):
        won = {b for b in sc["books"] if winner.get(b) == key}
        lost = sc["books"] - won
        if sc["book_id"] is not None and not won:
            continue  # the book is published by another scope
        ccys = set(sc["by_ccy"]) | {c for (_, b, c) in derived if b in won}
        if not sc["stale"]:
            ccys |= {c for b in lost if scopes[winner[b]]["book_id"] is not None for c in scopes[winner[b]]["by_ccy"]}
        if sc["stale"]:
            for ccy in sorted(ccys):
                line(sc["entity"], sc["book_id"], won, ccy,
                     math.fsum(_sum(derived.get((entity_of.get(b, sc["entity"]), b, ccy), {})) for b in won),
                     DERIVED_STALE, valid_date=sc["valid_date"])
            continue
        if lost:
            notes.append(f"часть отчёта ОВП по юрлицу {sc['entity']} считается итогом юрлица: ОВП книг "
                         + ", ".join(sorted(lost)) + " взята из их части отчёта и из итога юрлица вычтена")
        for ccy in sorted(ccys):
            c = sc["by_ccy"].get(ccy)
            reported = c["reported"] if c else 0.0
            minus = math.fsum(scopes[winner[b]]["by_ccy"][ccy]["reported"] for b in lost
                              if ccy in scopes[winner[b]]["by_ccy"] and scopes[winner[b]]["book_id"] is not None)
            added: dict[str, float] = defaultdict(float)
            for b in won:
                for k, v in _outside(derived.get((entity_of.get(b, sc["entity"]), b, ccy), {}), policy).items():
                    added[k] += v
            line(sc["entity"], sc["book_id"], won, ccy, reported - minus + math.fsum(added.values()), REPORT,
                 reported - minus if (c or minus) else None, added, sc["valid_date"])
    for x in outside_report:
        line(x["entity"], x["book_id"], [x["book_id"]], x["ccy"], x["derived"], DERIVED)
    return out, notes


def published_by_currency(lines: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """currency -> {total, by_source: {source: amount}} of the published lines."""
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for x in lines:
        out[x["ccy"]][x["source"]] += x["amount"]
    return {c: {"total": math.fsum(v.values()), "by_source": dict(v)} for c, v in sorted(out.items())}


def _scope_text(scope: Mapping[str, Any] | None) -> str:
    s = scope or {}
    return f"книга {s['book_id']}" if s.get("book_id") else f"юрлицо {s['entity']}" if s.get("entity") else "весь отчёт"


GAP_LIST = 50


def gaps(lines: Sequence[OcpLine], report_units: Sequence[Mapping[str, Any]],
         outside_report: Sequence[Mapping[str, Any]], on: date, policy: OcpPolicy = PROVISIONAL_POLICY,
         comparisons: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """What is missing for the published OCP to be trusted, each ``{code, text, items}``: no report unit in force
    (``no_report``), a unit too old (``stale_report``), positions in components outside the report without trade terms
    (``outside_without_terms``: their amount is a nominal, not a PV), books with an FX exposure outside every report
    unit (``books_outside_report``), and a scope whose break a component outside the report would close
    (``outside_double_count``: the report seems to hold it, so the published figure counts it twice; the composition
    in the adapter's settings is likely wrong). Nothing under ``published`` ``derived``."""
    if policy.published is PublishedBasis.DERIVED:
        return []
    out: list[dict[str, Any]] = []
    if not report_units:
        out.append({"code": "no_report", "items": [],
                    "text": "отчёта ОВП на дату нет: публикуемая ОВП — из позиций"})
    for u in report_units:
        if _stale(u.get("valid_date"), on, policy):
            vd = _day(u.get("valid_date"))
            out.append({"code": "stale_report", "items": [_scope_text(u.get("scope"))],
                        "text": f"отчёт ОВП ({_scope_text(u.get('scope'))}) от {vd:%d.%m.%Y}: старше "
                                f"{policy.report_max_age_days} дн., ОВП его книг — из позиций" if vd else
                                f"отчёт ОВП ({_scope_text(u.get('scope'))}) устарел: ОВП его книг — из позиций"})
    no_terms = sorted({x.position_id for x in lines if x.position_id and x.component not in policy.report_components
                       and x.basis in ("notional", "amount")})
    if no_terms:
        out.append({"code": "outside_without_terms", "items": no_terms[:GAP_LIST],
                    "text": f"позиций вне отчёта ОВП без условий сделки: {len(no_terms)} (их сумма — номинал или "
                            "сумма отчёта, не PV по условиям сделки)"})
    twice = []
    for c in comparisons:
        if not c.get("over") or _stale(c.get("valid_date"), on, policy):
            continue  # a stale unit is not published: nothing is added to it
        for k, v in (c.get("not_in_report") or {}).items():
            if abs(v) > policy.tolerance_ccy and abs(c["break"] - v) <= policy.tolerance_ccy:
                twice.append(f"{_scope_text(c.get('scope') or {'book_id': c.get('book_id'), 'entity': c.get('entity')})}"
                             f" {c['ccy']}: {COMPONENT_TEXT[k]}")
    if twice:
        out.append({"code": "outside_double_count", "items": twice[:GAP_LIST],
                    "text": f"слагаемое вне отчёта, похоже, уже есть в отчёте ОВП: {len(twice)} ("
                            + "; ".join(twice[:5]) + (", …" if len(twice) > 5 else "") + "): в публикуемой ОВП оно "
                            "посчитано дважды — проверьте состав отчёта в настройках адаптера"})
    books = sorted({str(x["book_id"]) for x in outside_report})
    if report_units and books:
        out.append({"code": "books_outside_report", "items": books[:GAP_LIST],
                    "text": f"книг с валютной позицией вне отчёта ОВП: {len(books)} ({', '.join(books[:10])}"
                            + (", …" if len(books) > 10 else "") + "): их ОВП — из позиций"})
    return out


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
    published: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)


def compute_ocp(rows: Sequence[Mapping[str, Any]], books: Mapping[str, Mapping[str, Any]], on: date, *,
                terms: Iterable[Mapping[str, Any]] = (), cash: Iterable[Mapping[str, Any]] = (),
                liabilities: Iterable[FundingLiability] = (), report_rows: Sequence[Mapping[str, Any]] = (),
                report_units: Sequence[Mapping[str, Any]] = (), policy: OcpPolicy = PROVISIONAL_POLICY) -> OcpResult:
    """The whole OCP of a run: lines, per position, the reconciliation with the report, the transfers, the published
    OCP and its gaps."""
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
    pub, pub_notes = published(comps, outside, lines, on, policy)
    return OcpResult(lines, per_pos, comps, outside, transfers(lines, books, tb, policy),
                     notes + cash_notes + pub_notes, pub, gaps(lines, report_units, outside, on, policy, comps))
