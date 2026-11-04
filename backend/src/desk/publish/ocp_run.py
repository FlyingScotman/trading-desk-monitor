"""The OCP (ОВП) in the run (wave B2, TDM#59): the generic engine ``desk.rules.ocp`` on the run's inputs, its result
in the run's publication.

**Inputs**, as the run sees them (its date ``D``, its ``known_at``):

- the run's published rows (``tdm_run.run_content``, after funding): FX delta per currency, core's
  ``extra.core`` and ``extra.ocp_amount`` of an FX derivative, the report's row (value date, asset class);
- the ``trade_terms`` in force (``PositionInputs.trade_terms``): which deal is an OCP transfer (its
  ``counterparty_id``), whether it is an NDF, its rate and value date;
- the books in force with their ``ocp_to`` (the run's reference books over ``store.current_books``);
- ``cash_balances`` in force on ``D`` (a stock, carried forward);
- the bonds' FX delta is already net of their currency OCP liability (``publish.fx_liability``, TDM#54, TDM#266: the
  FX PnL since the last OCP transfer), so the ``funding`` component is off (``OcpPolicy.bond_funding_liability``).
  Only when it is on: the liability of the bonds funded by their MV (``rules.funding.funding_liabilities`` on the
  adapter's ``funding_rules``, ``entities`` and ``funding_policy`` in force on ``D``), for the rows not net of it yet;
  without the previous day's rows here it is today's dirty MV (the bond's OCP 0, said in the notes). No funding rule:
  no liability, said in the notes;
- the day's OCP transfer deals of a book (``ocp_to``, DEC06) are their own component ``transfers``, never in the
  bonds: the bond book's OCP = Σ of its bonds' FX PnL since the last transfer + the transfer deals (signed: a transfer
  out is negative), counted once, ≈ the residual when the transfer matched;
- the reported OCP: ``store.dataset_rows("ocp", D)`` (08 §10, a stock; a unit older than ``D`` is carried forward
  and its ``valid_date`` said). No unit: status ``no_report``, the derived OCP is still published;
- the policy: ``rules.ocp.PROVISIONAL_POLICY`` with the adapter's ``ocp_policy`` in force on ``D`` (private/1.5,
  TDM#180; ``run_policy``): which components the report holds, what is published, NDFs, cash, the settlement
  window, the tolerance, the report's age. A conflict of two rows keeps the default and is said in the notes.

**Report base + day** (TDM#531, ``published`` ``report_base_plus_day``, the default): per base date B before ``D``
(``base_days``) the default run of B (its bond rows: their dirty MV on B; its OCP lines: the derivatives' PV on B),
the bonds' coupons and redemptions in (B, D] (the FX liability's coupons of the day when its last transfer is B, else
the bonds' terms), and the ``trades`` of every date in (B, D]; the age of a base in business days of the run's
calendar. Block v3 adds ``published.base_date``, per line ``parts`` / ``contributions`` / ``base_date`` / ``set_id``
/ ``base_kind``, ``derivatives_check`` and ``parts`` (their names).

**Output** (``RunOcp``): the block stored as ``run_reference`` kind ``ocp`` (``BLOCK_VERSION`` 3): status, policy (its
values, ``source`` per field, the ``provisional`` fields neither decided nor given by the adapter, the conflicts),
**the published OCP** (``published``, TDM#416: the report + the components outside it where a report unit covers, the
derived OCP elsewhere; ``by_ccy`` {ccy: total, rate, total_rub, by_source}, ``by_book`` the lines of
``rules.ocp.published``, ``basis``), the ``gaps`` (what is missing for it to be trusted: no report, a stale report,
positions outside the report without trade terms, books outside every report unit), the derived OCP by currency and
component and by book, the lines, the reconciliation per report scope and currency with the components outside the
report, the derived OCP outside any report unit, the transfers per book and currency, the report's units, totals and
notes; ``books`` (TDM#556): name, desk, book type and entity of the published OCP's books that the run's reference books
leave out (no positions in the run: a book only the OCP report knows, ``report_only_books``);
per position ``fact.extra.ocp`` (component, amounts, basis) and, for a transfer deal, ``extra.ocp_transfer`` true
(the pages mark it «Перенос ОВП»). The run log gets a summary line (the published OCP and its sources, the derived
OCP, the reconciliation) and a WARN line per break. The per-position FX delta and FX PnL are not changed by any of it.

The run never fails on the OCP: an error is status ``failed`` with its type and message in the log; the computation
runs in a savepoint (TDM#215), so an SQL error inside does not abort the run's transaction.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import date, timedelta
from typing import Any

import psycopg

from .. import store
from ..marketdata.adapter import MarketView
from ..rules import funding as F
from ..rules import ocp as O
from ..rules.reference import (
    apply_funding_policy,
    apply_ocp_policy,
    entities_from_reference,
    policy_from_reference,
)
from . import funding_run, fx_liability

KIND = "ocp"
BLOCK_VERSION = 3
COMPUTED, NO_REPORT, FAILED, NOT_COMPUTED = "computed", "no_report", "failed", "not_computed"
STATUS_TEXT = {
    COMPUTED: "ОВП — отчёт ОВП и слагаемые вне отчёта из позиций; сверено с ОВП из позиций",
    NO_REPORT: "отчёта ОВП (ocp) на дату нет: ОВП посчитано только из позиций, сверки нет",
    FAILED: "ОВП не посчитано: ошибка расчёта",
    NOT_COMPUTED: "прогон без расчёта ОВП (до B2)",
}
COMPUTED_TEXT = {
    O.PublishedBasis.REPORT_BASE_PLUS_DAY: "ОВП — отчёт ОВП юрлица за последний рабочий день без деривативов, которые "
                                           "отчёт даёт отдельно, плюс их PV, переоценка облигаций и сделки после даты "
                                           "отчёта, плюс слагаемые вне отчёта; сверено с ОВП из позиций",
    O.PublishedBasis.REPORT_PLUS_OUTSIDE: STATUS_TEXT[COMPUTED],
    O.PublishedBasis.DERIVED: "ОВП из позиций; отчёт ОВП только сверен",
}
"""The text of a computed block by the basis the run published on (``policy.published``)."""
LOG_BREAKS = 50


@dataclass
class RunOcp:
    status: str
    block: dict[str, Any]
    per_position: dict[str, dict[str, Any]] = field(default_factory=dict)
    log: list[tuple[str, str]] = field(default_factory=list)

    def lineage(self) -> dict[str, Any]:
        b = self.block
        return {"status": self.status, "breaks": (b.get("totals") or {}).get("breaks"),
                "published": {c: x.get("total") for c, x in ((b.get("published") or {}).get("by_ccy") or {}).items()},
                "report_units": [{"scope": u.get("scope"), "set_id": u.get("set_id"), "valid_date": u.get("valid_date")}
                                 for u in (b.get("report") or {}).get("units") or []],
                "provisional": (b.get("policy") or {}).get("provisional")}

    def apply(self, rows: list[dict[str, Any]], reference: dict[str, Any], log: list[tuple[str, str]]) -> None:
        """Into the run's content: ``extra.ocp`` (and ``extra.ocp_transfer``) of each row, the block as reference
        kind ``ocp``, the log lines before the run log's last line."""
        for r in rows:
            if (x := self.per_position.get(r["position_id"])) is not None:
                ex = r.setdefault("extra", {})
                ex["ocp"] = x
                if x.get("transfer"):
                    ex["ocp_transfer"] = True
        reference[KIND] = self.block
        at = len(log) - 1 if log else 0
        log[at:at] = self.log


def empty_block(status: str, text: str, on: date | None = None, **kw: Any) -> dict[str, Any]:
    return {"version": BLOCK_VERSION, "status": status, "text": text, "as_of": on.isoformat() if on else None,
            "reporting_ccy": O.PROVISIONAL_POLICY.reporting_ccy,
            "components": [{"key": c, "name": O.COMPONENT_TEXT[c]} for c in O.COMPONENTS],
            "sources": [{"key": k, "name": v} for k, v in O.SOURCE_TEXT.items()],
            "parts": [{"key": p, "name": O.PART_TEXT[p]} for p in O.PARTS],
            "policy": None, "published": {"basis": None, "base_date": None, "by_ccy": {}, "by_book": []}, "gaps": [],
            "by_ccy": {}, "by_book": [], "lines": [], "reconciliation": [], "derivatives_check": [],
            "derivatives_pv": [],
            "outside_report": [], "transfers": [], "report": {"units": [], "rows": 0}, "totals": None, "notes": [],
            "books": {}, **kw}


def report_only_books(published: Sequence[Mapping[str, Any]], books: Mapping[str, Mapping[str, Any]],
                      listed: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """The books of the published OCP (its lines' ``book_id`` and ``books``) that the run's reference books (``listed``:
    the books with positions in the run) leave out, with their row in force (``books``; TDM#556: a book only the OCP
    report knows, such as an old book without positions): ``{book_id: {name, desk, book_type, entity}}``, so the pages
    group and filter its OCP by entity and book type as the owner set them. A book without a row in force is left out
    (the pages know nothing more of it than the line)."""
    have = {b.get("book_id") for b in listed}
    ids = {x["book_id"] for x in published if x.get("book_id")} | {b for x in published for b in x.get("books") or ()}
    out: dict[str, dict[str, Any]] = {}
    for b in sorted(ids - have):
        if (m := books.get(b)) is not None:
            out[b] = {"name": m.get("name") or b, "desk": m.get("desk") or "—", "book_type": m.get("book_type"),
                      "entity": m.get("entity") or "—"}
    return out


def _liabilities(con: psycopg.Connection, on: date, known_at: Any, rows: list[dict[str, Any]],
                 books: dict[str, dict], market: MarketView | None, notes: list[str]) -> list[F.FundingLiability]:
    """The currency funding liabilities of the run's bonds, on the funding policy the run's funding applies."""
    rules = store.current_reference(con, "funding_rules", on, known_at)
    if not any(r.get("active", True) is not False for r in rules):
        notes.append("пассив фондирования не вычтен: фондирование не настроено (нет funding_rules)")
        return []
    entity_rows = store.current_reference(con, "entities", on, known_at)
    try:
        base = policy_from_reference(rules, entity_rows)
    except ValueError as e:
        notes.append(f"пассив фондирования не вычтен: правила фондирования противоречат друг другу ({e})")
        return []
    pol = apply_funding_policy(store.current_reference(con, "funding_policy", on, known_at), base).policy
    types, _ = entities_from_reference(entity_rows)
    positions = []
    for r in rows:
        p, _ = funding_run._position(r, books)
        if p is None or p.ccy == pol.reporting_ccy or O._net_of_liability(r):  # net already: never twice
            continue
        positions.append(p)
    if positions:
        notes.append("пассив фондирования — грязная MV дня (без строк прошлого дня): ОВП облигаций с пассивом — 0")
    liabs, problems = F.funding_liabilities(positions, policy=pol, entities=types or None,
                                            fx=funding_run.fx_table([market]), on=on)
    notes += [f"{p.ref}: пассив фондирования не определён ({p.reason})" for p in problems]
    return list(liabs)


def _rates(reference_fx: list[dict[str, Any]]) -> dict[str, float]:
    return {r["ccy"]: float(r["rate"]) for r in reference_fx if r.get("rate")}


def run_policy(con: psycopg.Connection, on: date, known_at: Any, base: O.OcpPolicy = O.PROVISIONAL_POLICY
               ) -> tuple[O.OcpPolicy, dict[str, str], tuple[str, ...]]:
    """The OCP policy of the run of ``on``: ``base`` with the adapter's ``ocp_policy`` in force (private/1.5,
    ``rules.reference.apply_ocp_policy``); (policy, source per field, conflicts). A field's source:
    ``ocp_policy:<id>``, an owner decision (``rules.ocp.DECIDED``), ``provisional`` (``OPEN_DECISIONS``) or
    ``default``. ``published`` no row in force gives follows the date (``rules.ocp.default_published``): before
    ``BASIS_SINCE`` the basis those dates were published on, ``report_plus_outside``, so a date recomputed later keeps
    it."""
    s = apply_ocp_policy(store.current_reference(con, "ocp_policy", on, known_at), base)
    policy = s.policy
    open_fields = {d.field for d in O.OPEN_DECISIONS}
    source = {f.name: s.source.get(f.name) or O.DECIDED.get(f.name)
              or ("provisional" if f.name in open_fields else "default") for f in fields(O.OcpPolicy)}
    if "published" not in s.source and base.published is O.PROVISIONAL_POLICY.published \
            and (by_date := O.default_published(on)) is not policy.published:
        policy = replace(policy, published=by_date)
        source["published"] = (f"OQ-18 (владелец, 28.10.2026, TDM#416): дата до "
                               f"{O.BASIS_SINCE:%d.%m.%Y}, ocp_policy базис не задаёт")
    return policy, source, s.conflicts


def compute(con: psycopg.Connection, on: date, market: MarketView | None, content_rows: list[dict[str, Any]],
            reference: dict[str, Any], inputs: store.PositionInputs,
            policy: O.OcpPolicy = O.PROVISIONAL_POLICY, *, terms: Mapping[str, Any] | None = None,
            calendar: Any = None, off_report: Mapping[str, Any] | None = None) -> RunOcp:
    """The OCP of the run of ``on`` (see the module docstring). ``off_report``: the run's derivatives off the positions
    report (``valuation.offreport.summary``, TDM#468): with the switch on ``exclude`` the FX trades and CCS among them
    are a gap of the OCP. ``reference``: ``RunContent.reference`` (books, fx);
    ``policy``: the base the adapter's ``ocp_policy`` applies over; ``terms``: position_id -> ``BondTerms`` (the bonds'
    coupons and redemptions since the base); ``calendar``: the run's business calendar (core's ``BusinessCalendar``)
    for the age of a base, weekends outside its window or without it. In a savepoint (TDM#215): an SQL error inside
    rolls back only the block's reads, so the run still publishes, with the block ``failed``."""
    try:
        with funding_run.savepoint(con):
            return _compute(con, on, market, content_rows, reference, inputs, policy, terms or {}, calendar, off_report)
    except Exception as e:  # noqa: BLE001 - the OCP never fails the run; it says why it has none
        text = f"{STATUS_TEXT[FAILED]}: {type(e).__name__}: {e}"
        return RunOcp(FAILED, empty_block(FAILED, text, on), log=[("WARN", text[:2000])])


OFF_REPORT_KINDS = ("fx_forward", "fx_swap", "ccs")
"""Kinds of trades off the positions report whose nominal the OCP takes out of the report's base (they are derivatives
of the kinds it publishes apart) and whose PV is not added while they are excluded."""


def _off_report_gap(off_report: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Gap ``off_report_excluded`` (switch ``exclude``): FX trades and CCS off the positions report are in no OCP term,
    though their nominal is out of the base: the books and the number of trades."""
    if not off_report or off_report.get("mode") != "exclude":
        return []
    ex = [t for t in off_report.get("list") or [] if t.get("state") == "excluded"
          and any(k in OFF_REPORT_KINDS for k in str(t.get("kind")).split("+"))]
    if not ex:
        return []
    by_book = Counter(str(t["book"]) for t in ex)
    return [{"code": "off_report_excluded",
             "items": [f"{b} ({n})" for b, n in sorted(by_book.items())][:20],
             "text": f"сделки вне отчёта позиций исключены настройкой: {len(ex)} (книги "
                     + ", ".join(sorted(by_book)[:10]) + "): их номинал вынесен из базы ОВП, а PV не добавлен"}]


def _compute(con: psycopg.Connection, on: date, market: MarketView | None, content_rows: list[dict[str, Any]],
             reference: dict[str, Any], inputs: store.PositionInputs, policy: O.OcpPolicy,
             terms: Mapping[str, Any] | None = None, calendar: Any = None,
             off_report: Mapping[str, Any] | None = None) -> RunOcp:
    known_at = inputs.known_at
    policy, source, conflicts = run_policy(con, on, known_at, policy)
    books = {b["book_id"]: b for b in store.current_books(con, on, known_at)}
    books.update({b["book_id"]: b for b in reference.get("books") or []})
    notes: list[str] = list(conflicts)
    cash = store.dataset_rows(con, "cash_balances", on, known_at=known_at) if policy.cash_from_balances else None
    liabs = _liabilities(con, on, known_at, content_rows, books, market, notes) \
        if policy.bond_funding_liability else []
    report = store.dataset_rows(con, KIND, on, known_at=known_at)
    is_bd = business_day(calendar)
    days = base_days(con, on, known_at, report.units, policy, is_bd, content_rows, books, terms or {})
    res = O.compute_ocp(content_rows, books, on, terms=inputs.trade_terms, cash=cash.rows if cash else (),
                        liabilities=liabs, report_rows=report.rows, report_units=report.units, policy=policy,
                        days=days, is_business_day=is_bd, valid_dates=inputs.valid_dates)
    notes += res.notes
    res.gaps.extend(_off_report_gap(off_report))
    rates = _rates(reference.get("fx") or [])
    status = COMPUTED if report.units else NO_REPORT
    by_ccy = {}
    for ccy, parts in O.by_currency(res.lines).items():
        total = math.fsum(parts.values())
        by_ccy[ccy] = {"components": parts, "total": total, "rate": rates.get(ccy),
                       "total_rub": total * rates[ccy] if ccy in rates else None}
    by_book = [{"entity": e, "book_id": b, "ccy": c, "components": parts, "total": math.fsum(parts.values()),
                "ocp_to": (books.get(b) or {}).get("ocp_to")}
               for (e, b, c), parts in sorted(O.derive(res.lines).items())]
    totals: dict[str, Any] = {"breaks": sum(1 for c in res.comparisons if c["over"]),
                              "comparisons": len(res.comparisons), "by_ccy": {}}
    agg: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for c in res.comparisons:
        t = agg[c["ccy"]]
        for k in ("reported", "derived", "in_report", "break"):
            t[k] += c[k]
        t["breaks"] += 1 if c["over"] else 0
    totals["by_ccy"] = {k: dict(v) for k, v in sorted(agg.items())}
    for c in res.comparisons:
        if c["ccy"] in rates:
            c["break_rub"] = c["break"] * rates[c["ccy"]]
    if cash is None:
        notes.append("остатки на счетах в ОВП не включены (cash_from_balances = false)")
    elif not cash.units:
        notes.append("остатков на счетах (cash_balances) на дату нет: слагаемое «кэш» — только FX-строки позиций")
    stale = [u for u in report.units if u["valid_date"] < on]
    if stale:
        notes.append("отчёт ОВП перенесён с прошлой даты: " + ", ".join(
            f"{_scope(u['scope'])} от {u['valid_date']}" for u in stale))
    prov = O.provisional(policy, [f for f, src in source.items() if src.startswith("ocp_policy:")])
    pub_ccy = {}
    for ccy, x in O.published_by_currency(res.published).items():
        pub_ccy[ccy] = {**x, "rate": rates.get(ccy),
                        "total_rub": x["total"] * rates[ccy] if ccy in rates else None}
    block = empty_block(
        status, COMPUTED_TEXT[policy.published] if status == COMPUTED else STATUS_TEXT[status], on,
        policy={**policy.lineage(), "provisional": prov, "source": source, "conflicts": list(conflicts),
                "decisions": [{"field": d.field, "ref": d.ref, "question": d.question, "provisional": d.provisional}
                              for d in O.OPEN_DECISIONS if d.field in prov]},
        published={"basis": policy.published.value,
                   "base_date": max((x["base_date"] for x in res.published if x.get("base_date")), default=None),
                   "by_ccy": pub_ccy, "by_book": res.published},
        gaps=res.gaps,
        by_ccy=by_ccy, by_book=by_book, lines=[x.as_json() for x in res.lines], reconciliation=res.comparisons,
        derivatives_check=res.derivatives_check, derivatives_pv=res.derivatives_pv,
        outside_report=res.outside_report, transfers=res.transfers,
        books=report_only_books(res.published, books, reference.get("books") or []),
        report={"units": [{k: (v.isoformat() if isinstance(v, date) else v) for k, v in u.items()}
                          for u in report.units], "rows": len(report.rows)},
        totals=totals, notes=notes,
        inputs={"cash_units": len(cash.units) if cash else 0, "liabilities": len(liabs),
                "trade_terms": len(inputs.trade_terms),
                "base_days": {b.isoformat(): {"run_id": d.run_id, "bonds": len(d.bonds),
                                              "trades": None if d.trade_rows is None else len(d.trade_rows)}
                              for b, d in sorted(days.items())}})
    block = funding_run._jsonable(block)
    log = [("INFO", _summary(block))]
    breaks = [c for c in res.comparisons if c["over"]]
    for c in [c for c in res.derivatives_check if c["over"]][:LOG_BREAKS]:
        log.append(("WARN", (f"ОВП: номинал деривативов отчёта против их PV на {c['pv_date']}: "
                            f"{c['book_id'] or c['entity']} {c['ccy']} {O.KIND_TEXT[c['kind']]}: номинал "
                            f"{c['nominal']:,.0f}, PV {c['pv']:,.0f}, разница {c['diff']:,.0f}")))
    for c in breaks[:LOG_BREAKS]:
        where = c["book_id"] or c["entity"]
        log.append(("WARN", f"ОВП: брейк {where} {c['ccy']}: отчёт {c['reported']:,.0f}, из позиций "
                            f"{c['in_report']:,.0f}, разница {c['break']:,.0f}"
                            + (f" ({'; '.join(c['hints'])})" if c["hints"] else "")))
    if len(breaks) > LOG_BREAKS:
        log.append(("WARN", f"ОВП: ещё {len(breaks) - LOG_BREAKS} брейков (в блоке ocp прогона)"))
    for t in res.transfers:
        if t["over"]:
            log.append(("WARN", (f"ОВП: перенос {t['book_id']} -> {t['ocp_to']} {t['ccy']}: до переноса "
                                f"{t['before']:,.0f}, перенесено {t['transferred']:,.0f}, осталось {t['after']:,.0f}")))
    log += [("INFO", f"ОВП: {n}") for n in notes[:LOG_BREAKS]]
    return RunOcp(status, block, funding_run._jsonable(res.per_position), log)


def business_day(calendar: Any) -> Callable[[date], bool] | None:
    """``is_business_day`` of the run's calendar (core's ``BusinessCalendar``): weekdays outside its window; None
    without one (``rules.ocp`` then counts weekdays)."""
    if calendar is None:
        return None

    def f(d: date) -> bool:
        try:
            return bool(calendar.is_business_day(d))
        except Exception:  # noqa: BLE001 - outside the calendar's window: weekdays
            return d.weekday() < 5
    return f


def _f(x: Any) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _foreign_bond(r: Mapping[str, Any]) -> bool:
    return r.get("instr_type") == "BOND" and (r.get("ccy") or O.PROVISIONAL_POLICY.reporting_ccy) \
        != O.PROVISIONAL_POLICY.reporting_ccy


NO_TERMS_COUPONS = "условий бумаги нет: купоны дня валютного пассива, погашения не учтены"


def paid_since(now: Mapping[str, Any] | None, before: Mapping[str, Any], terms: Any, b0: date, on: date,
               trades: Sequence[Mapping[str, Any]] = ()) -> tuple[float, str]:
    """(the coupons and principal a bond received in (``b0``, ``on``], in its currency, signed as the position; a note
    when they are not fully known). From the bond's schedule: per payment date in (``b0``, ``on``] its coupon
    (``amount_per_bond``) and its redemption or amortisation (``value_per_bond``) x the quantity held that day: the
    quantity on ``b0`` + the bonds bought − sold by the trades of the position after ``b0`` dated before the payment.
    Without the bond's terms: the FX liability's coupons of the day (``publish.fx_liability``) when its last transfer is
    ``b0``, said: principal not known."""
    q0 = fx_liability._qty(before)
    if terms is None:
        fl = ((now or {}).get("extra") or {}).get("fx_liability") or {}
        if fl.get("state") == fx_liability.COMPUTED and fl.get("reference") == fx_liability.PREV \
                and fl.get("reference_date") == b0.isoformat():
            return float(fl.get("coupons_ccy") or 0.0), NO_TERMS_COUPONS
        return 0.0, "условий бумаги нет: купоны и погашения после базы не учтены"
    flows: list[tuple[date, float]] = []
    missing = []
    for c in getattr(terms, "coupons", ()):
        if b0 < c.pay_date <= on:
            if getattr(c, "amount_per_bond", None) is None:
                missing.append(c.pay_date.isoformat())
            else:
                flows.append((c.pay_date, float(c.amount_per_bond)))
    for r in getattr(terms, "redemptions", ()):
        if b0 < r.date <= on and getattr(r, "value_per_bond", None) is not None:
            flows.append((r.date, float(r.value_per_bond)))
    why = ("купон " + ", ".join(missing) + " без суммы: не учтён") if missing else ""
    if not flows:
        return 0.0, why
    if q0 is None:
        return 0.0, "количество бумаг на дату базы неизвестно: выплаты после базы не учтены"
    moves = []
    for t in trades:
        q, td = _f(t.get("quantity")), O._day(t.get("trade_date"))
        if q is not None and td is not None and str(t.get("status") or "live") == "live":
            moves.append((td, -abs(q) if str(t.get("side")) == "sell" else abs(q)))
    total = math.fsum(per * (q0 + math.fsum(m for td, m in moves if td < d)) for d, per in flows)
    return total, why


def bond_moves(rows: list[dict[str, Any]], before: Mapping[str, Mapping[str, Any]], books: Mapping[str, Mapping],
               terms: Mapping[str, Any], b0: date, on: date,
               trades: Sequence[Mapping[str, Any]] = ()) -> list[O.BondMove]:
    """The foreign-currency bonds' revaluation since ``b0`` (``rules.ocp.BondMove``): the run's rows against the rows
    of the default run of ``b0`` (``before``, position_id -> its fact), the positions of either; ``trades``: the trades
    after ``b0`` (``rules.ocp.current_trades``), for the quantity held on each payment date (``paid_since``)."""
    now = {r["position_id"]: r for r in rows if _foreign_bond(r)}
    then = {pid: r for pid, r in before.items() if _foreign_bond(r)}
    by_pos: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for t in trades:
        if str(t.get("asset_class") or "").lower() == "bond":
            by_pos[str(t.get("position_id"))].append(t)
    out = []
    for pid in sorted(set(now) | set(then)):
        r1, r0 = now.get(pid), then.get(pid)
        r = r1 or r0
        mv1 = _f(r1.get("mv")) if r1 else None
        mv0 = _f(r0.get("mv")) if r0 else None
        note = ""
        if r1 and r0 and (r0.get("ccy") or "") != (r1.get("ccy") or ""):
            mv0, note = None, f"на {b0} позиция в другой валюте: MV на дату базы не взята"
        paid, why = paid_since(r1, r0, terms.get(pid), b0, on, by_pos.get(pid, ())) \
            if r0 and mv0 is not None else (0.0, "")
        book = str(r["book_id"])
        out.append(O.BondMove(pid, book, str((books.get(book) or {}).get("entity") or "—"), str(r.get("ccy")),
                              mv0, mv1, paid, "; ".join(x for x in (note, why) if x)))
    return out


def _pv_base(con: psycopg.Connection, run_id: int | None) -> dict[tuple[str, str, str], float] | None:
    """(book, currency, kind) -> the derivatives' PV on a stored run's date (its ``ocp`` block's ``derivatives_pv``,
    v3; always the PV whatever ``amount_basis``); None without the run, for an older block or for a run published on
    another basis than ``report_base_plus_day`` (its ``derivatives_pv`` is empty: the check says the PV on B is
    unknown, never a diff of the whole nominal)."""
    if run_id is None:
        return None
    row = con.execute("SELECT data->'derivatives_pv', data->'published'->>'basis' FROM run_reference"
                      " WHERE run_id = %s AND kind = %s", (run_id, KIND)).fetchone()
    # a run published on another basis (the first base day after the switch) has no PV of its own: unknown, not 0
    if not row or not isinstance(row[0], list) or row[1] != O.PublishedBasis.REPORT_BASE_PLUS_DAY.value:
        return None
    out: dict[tuple[str, str, str], float] = defaultdict(float)
    for x in row[0]:
        if (a := _f(x.get("amount"))) is not None:
            out[(str(x.get("book_id")), str(x.get("ccy")), str(x.get("kind")))] += a
    return dict(out)


def base_days(con: psycopg.Connection, on: date, known_at: Any, units: list[dict[str, Any]], policy: O.OcpPolicy,
              is_bd: Callable[[date], bool] | None, rows: list[dict[str, Any]], books: Mapping[str, Mapping],
              terms: Mapping[str, Any]) -> dict[date, O.BaseDay]:
    """Per base date B before ``on`` of a usable report unit (``report_base_plus_day``, TDM#531): B's default run, the
    ``trades`` of every date of (B, ``on``] (``trade_rows``; none stored on any of them: not loaded), the bonds'
    revaluation since B (``bond_moves``, the quantity on each payment date from those trades) and the derivatives' PV
    on B (B's block ``derivatives_pv``)."""
    out: dict[date, O.BaseDay] = {}
    if policy.published is not O.PublishedBasis.REPORT_BASE_PLUS_DAY:
        return out
    for b0 in sorted({u["valid_date"] for u in units}):
        if b0 >= on or O._stale(b0, on, policy, is_bd):
            continue
        trade_rows: list[dict[str, Any]] = []
        loaded, d = False, b0
        while d < on:
            d += timedelta(days=1)
            t = store.dataset_rows(con, "trades", d, known_at=known_at)
            loaded = loaded or bool(t.units)
            trade_rows += t.rows
        rid, facts, _ = funding_run._previous_rows(con, b0)
        moves = bond_moves(rows, {f["position_id"]: f for f in facts}, books, terms, b0, on,
                           O.current_trades(trade_rows, b0)) if rid else []
        out[b0] = O.BaseDay(b0, rid, moves, None, [], _pv_base(con, rid), trade_rows if loaded else None,
                            base_positions={f["position_id"] for f in facts} if rid else None)
    return out


def _scope(s: dict[str, Any] | None) -> str:
    return ", ".join(f"{k} {v}" for k, v in (s or {}).items()) or "весь файл"


def _summary(block: dict[str, Any]) -> str:
    pub = []
    for ccy, x in (block.get("published") or {}).get("by_ccy", {}).items():
        pub.append(f"{ccy} {x['total']:,.0f} (" + ", ".join(
            f"{O.SOURCE_TEXT.get(k, k)} {v:,.0f}" for k, v in x["by_source"].items()) + ")")
    parts = []
    for ccy, x in block["by_ccy"].items():
        parts.append(f"{ccy} {x['total']:,.0f} (" + ", ".join(
            f"{O.COMPONENT_TEXT[c]} {v:,.0f}" for c, v in x["components"].items()) + ")")
    t = block["totals"] or {}
    return ("ОВП: " + ("; ".join(pub) or "нет валютных позиций") + "; "
            + "ОВП из позиций: " + ("; ".join(parts) or "нет валютных позиций") + "; "
            + (f"сверка с отчётом: сравнений {t.get('comparisons', 0)}, брейков {t.get('breaks', 0)}"
               if block["status"] == COMPUTED else STATUS_TEXT[block["status"]])
            + (f"; не хватает: {len(block['gaps'])}" if block.get("gaps") else "")
            + "; по умолчанию (provisional): " + (", ".join(block["policy"]["provisional"]) or "нет"))
