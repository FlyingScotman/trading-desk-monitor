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
  and its ``valid_date`` said). No unit: status ``no_report``, the derived OCP is still published.

**Output** (``RunOcp``): the block stored as ``run_reference`` kind ``ocp`` (``BLOCK_VERSION``): status, policy (with
the provisional fields: every one until the owner answers TDM#60), the derived OCP by currency and component and by
book, the lines, the reconciliation per report scope and currency with the components outside the report, the
derived OCP outside any report unit, the transfers per book and currency, the report's units, totals and notes;
per position ``fact.extra.ocp`` (component, amounts, basis) and, for a transfer deal, ``extra.ocp_transfer`` true
(the pages mark it «Перенос ОВП»). The run log gets a summary line and a WARN line per break.

The run never fails on the OCP: an error is status ``failed`` with its type and message in the log.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import psycopg

from .. import store
from ..marketdata.adapter import MarketView
from ..rules import funding as F
from ..rules import ocp as O
from ..rules.reference import apply_funding_policy, entities_from_reference, policy_from_reference
from . import funding_run

KIND = "ocp"
BLOCK_VERSION = 1
COMPUTED, NO_REPORT, FAILED, NOT_COMPUTED = "computed", "no_report", "failed", "not_computed"
STATUS_TEXT = {
    COMPUTED: "ОВП из позиций сверено с отчётом ОВП",
    NO_REPORT: "отчёта ОВП (ocp) на дату нет: ОВП посчитано только из позиций, сверки нет",
    FAILED: "ОВП не посчитано: ошибка расчёта",
    NOT_COMPUTED: "прогон без расчёта ОВП (до B2)",
}
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
            "policy": None, "by_ccy": {}, "by_book": [], "lines": [], "reconciliation": [], "outside_report": [],
            "transfers": [], "report": {"units": [], "rows": 0}, "totals": None, "notes": [], **kw}


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


def compute(con: psycopg.Connection, on: date, market: MarketView | None, content_rows: list[dict[str, Any]],
            reference: dict[str, Any], inputs: store.PositionInputs,
            policy: O.OcpPolicy = O.PROVISIONAL_POLICY) -> RunOcp:
    """The OCP of the run of ``on`` (see the module docstring). ``reference``: ``RunContent.reference`` (books, fx)."""
    try:
        return _compute(con, on, market, content_rows, reference, inputs, policy)
    except Exception as e:  # noqa: BLE001 - the OCP never fails the run; it says why it has none
        text = f"{STATUS_TEXT[FAILED]}: {type(e).__name__}: {e}"
        return RunOcp(FAILED, empty_block(FAILED, text, on), log=[("WARN", text[:2000])])


def _compute(con: psycopg.Connection, on: date, market: MarketView | None, content_rows: list[dict[str, Any]],
             reference: dict[str, Any], inputs: store.PositionInputs, policy: O.OcpPolicy) -> RunOcp:
    known_at = inputs.known_at
    books = {b["book_id"]: b for b in store.current_books(con, on, known_at)}
    books.update({b["book_id"]: b for b in reference.get("books") or []})
    notes: list[str] = []
    cash = store.dataset_rows(con, "cash_balances", on, known_at=known_at) if policy.cash_from_balances else None
    liabs = _liabilities(con, on, known_at, content_rows, books, market, notes) \
        if policy.bond_funding_liability else []
    report = store.dataset_rows(con, KIND, on, known_at=known_at)
    res = O.compute_ocp(content_rows, books, on, terms=inputs.trade_terms, cash=cash.rows if cash else (),
                        liabilities=liabs, report_rows=report.rows, report_units=report.units, policy=policy)
    notes += res.notes
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
    prov = O.provisional(policy)
    block = empty_block(
        status, STATUS_TEXT[status], on,
        policy={**policy.lineage(), "provisional": prov,
                "decisions": [{"field": d.field, "ref": d.ref, "question": d.question, "provisional": d.provisional}
                              for d in O.OPEN_DECISIONS if d.field in prov]},
        by_ccy=by_ccy, by_book=by_book, lines=[x.as_json() for x in res.lines], reconciliation=res.comparisons,
        outside_report=res.outside_report, transfers=res.transfers,
        report={"units": [{k: (v.isoformat() if isinstance(v, date) else v) for k, v in u.items()}
                          for u in report.units], "rows": len(report.rows)},
        totals=totals, notes=notes,
        inputs={"cash_units": len(cash.units) if cash else 0, "liabilities": len(liabs),
                "trade_terms": len(inputs.trade_terms)})
    block = funding_run._jsonable(block)
    log = [("INFO", _summary(block))]
    breaks = [c for c in res.comparisons if c["over"]]
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


def _scope(s: dict[str, Any] | None) -> str:
    return ", ".join(f"{k} {v}" for k, v in (s or {}).items()) or "весь файл"


def _summary(block: dict[str, Any]) -> str:
    parts = []
    for ccy, x in block["by_ccy"].items():
        parts.append(f"{ccy} {x['total']:,.0f} (" + ", ".join(
            f"{O.COMPONENT_TEXT[c]} {v:,.0f}" for c, v in x["components"].items()) + ")")
    t = block["totals"] or {}
    return ("ОВП из позиций: " + ("; ".join(parts) or "нет валютных позиций") + "; "
            + (f"сверка с отчётом: сравнений {t.get('comparisons', 0)}, брейков {t.get('breaks', 0)}"
               if block["status"] == COMPUTED else STATUS_TEXT[block["status"]])
            + "; по умолчанию (provisional): " + (", ".join(block["policy"]["provisional"]) or "нет"))
