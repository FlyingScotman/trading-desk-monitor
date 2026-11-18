"""The OCP of one saved date with what each figure consists of (TDM#767): the history's day, plus per line the terms
the run saved for it and, where a term was not saved, a named absence instead of a guess.

The day is the history's own (``ocp_history.history`` over the one date: the date's default run, the anchor on its
date), so a figure here is the figure of the history's chart for that date, line by line. Nothing is recomputed by
today's rules: the terms are read from the date's run (its ``ocp`` block, ``published.by_book``) as it saved them, and
what the block does not hold is said so, with the reason:

- ``report_base_plus_day`` (block v3): ``parts`` (base, balances, derivatives, bonds, trades, outside; they sum to the
  line's amount) and per part its bounded ``contributions`` (the largest items and the rest; they sum to the part). A
  part with an amount and no contributions, or contributions that do not sum to it, is named: the run did not save
  what it consists of (``no_contributions``, ``contributions_mismatch``).
- ``report_plus_outside`` (block v2): the report's whole figure and the components outside it (``reported`` and
  ``outside``). The terms of the current methodology (the base, the PV of the derivatives, the day's changes) are not
  in such a run: ``old_methodology``.
- ``derived`` / ``derived_stale_report``: the run saves the sum only; its items (positions, cash balances, funding
  liabilities) are read from what the store fixed for the run (``ocp_origin``, at request time, no new rules). Not
  restored, or not summing to the figure: ``no_origin``, ``origin_mismatch``.
- a run without the published OCP (before B2, v1 block, failed): no lines, ``absent`` says why.

``breakdown.rows`` sum to the line's amount (``sum``, ``matches``); a line whose terms do not (any saved block that
breaks the rule) says ``sum_mismatch`` and is never shown as complete.
"""

from __future__ import annotations

import logging
import math
from datetime import date
from typing import Any

from ..publish import ocp_run
from ..rules import ocp as O
from . import ocp_origin
from .ocp_history import history, period

log = logging.getLogger(__name__)

PART_ORDER = ("base", "balances", "derivatives", "bonds", "trades", "outside")
"""The terms of ``parts`` in the order the pages show them; a key the block adds later follows them."""
PART_NAMES = {"base": "база: отчёт ОВП без деривативов", "balances": "остатки по счетам: книги нет в отчёте ОВП",
              "derivatives": "PV деривативов из позиций", "bonds": "переоценка облигаций",
              "trades": "сделки после базы", "outside": "вне отчёта (из позиций)",
              "report": "отчёт ОВП целиком, с деривативами", "cash": "остатки на счетах",
              "position": "позиции", "liability": "пассив фондирования", "rest": "остальное"}
"""Row names for a day whose block gives none of its own."""

FULL, PARTIAL, NONE = "full", "partial", "none"

NO_OCP_REASON = {
    ocp_run.NOT_COMPUTED: "прогон этой даты сделан до расчёта ОВП: слагаемых в нём нет",
    ocp_run.FAILED: "расчёт ОВП в прогоне этой даты завершился ошибкой: слагаемых нет",
}
NO_BLOCK_REASON = "в прогоне этой даты нет публикуемой ОВП (прогон до её публикации): слагаемых в нём нет"


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.01, 1e-9 * max(abs(a), abs(b)))


def _num(x: Any) -> float | None:
    return float(x) if isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x) else None


def _n(v: float) -> str:
    return f"{v:,.2f}".replace(",", " ")


def _gap(code: str, part: str | None, reason: str) -> dict[str, Any]:
    return {"code": code, "part": part, "reason": reason}


def _row(key: str, amount: float, items: int = 0, rest: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    return {"key": key, "name": PART_NAMES.get(key, key), "amount": amount, "items": items, "rest": rest, **kw}


def _finish(rows: list[dict[str, Any]], amount: Any, absent: list[dict[str, Any]]) -> dict[str, Any]:
    """The breakdown of a line: its rows, their sum against the amount, what is absent, and the status."""
    total = math.fsum(r["amount"] for r in rows) if rows else None
    amt = _num(amount)
    matches = None if total is None or amt is None else _close(total, amt)
    if matches is False:
        absent = [*absent, _gap("sum_mismatch", None, f"слагаемые прогона не складываются в цифру строки: в них "
                                                      f"{_n(total)}, в строке {_n(amt)}")]
    status = NONE if not rows else FULL if not absent and matches else PARTIAL
    return {"status": status, "rows": rows, "sum": total, "matches": matches, "absent": absent}


def _parts_breakdown(x: dict[str, Any]) -> dict[str, Any]:
    """A ``report_base_plus_day`` line: ``parts`` with each part's contributions as the run saved them."""
    parts = {k: v for k, v in (x.get("parts") or {}).items() if _num(v) is not None}
    contrib = x.get("contributions") if isinstance(x.get("contributions"), dict) else {}
    rows, absent = [], []
    for k in [*(k for k in PART_ORDER if k in parts), *sorted(k for k in parts if k not in PART_ORDER)]:
        v = float(parts[k])
        c = contrib.get(k) if isinstance(contrib.get(k), dict) else None
        items = [i for i in (c or {}).get("items") or [] if isinstance(i, dict) and _num(i.get("amount")) is not None]
        rest = c.get("rest") if c and isinstance(c.get("rest"), dict) else None
        row = _row(k, v, len(items), {"count": rest.get("count"), "amount": rest.get("amount")} if rest else None)
        if k == "base":  # which report the base is: its set and date, its file as the base item names it
            files = sorted({str(i["file"]) for i in items if i.get("kind") == "report" and i.get("file")})
            row.update(setId=x.get("set_id"), date=x.get("base_date"), file=", ".join(files) or None)
        rows.append(row)
        if v and not items and not rest:
            absent.append(_gap("no_contributions", k, f"«{PART_NAMES.get(k, k)}»: из чего сложилась цифра, прогон не "
                                                      "сохранил (вкладов нет)"))
        elif c is not None:
            got = math.fsum([*(float(i["amount"]) for i in items), _num((rest or {}).get("amount")) or 0.0])
            if not _close(got, v):
                absent.append(_gap("contributions_mismatch", k, f"«{PART_NAMES.get(k, k)}»: вклады прогона не "
                                                                f"складываются в цифру слагаемого (в них {_n(got)})"))
    return _finish(rows, x.get("amount"), absent)


def _outside_breakdown(x: dict[str, Any]) -> dict[str, Any]:
    """A ``report_plus_outside`` line (the methodology before ``report_base_plus_day``): the report whole and what the
    run added outside it; the current methodology's terms are not in such a run."""
    rows = []
    if _num(x.get("reported")) is not None:
        rows.append(_row("report", float(x["reported"]), date=x.get("valid_date")))
    rows += [_row("outside:" + c, float(v)) | {"name": "вне отчёта: " + c} for c, v in sorted((x.get("outside") or {}).items())
             if _num(v) is not None]
    return _finish(rows, x.get("amount"), [_gap(
        "old_methodology", None, "прогон считан по прежней методике (отчёт целиком и слагаемые вне отчёта): базы "
                                  "отчёта без деривативов, PV деривативов, изменений дня и переноса в нём нет")])


def _derived_breakdown(x: dict[str, Any], origin: dict[str, Any] | None, failed: bool) -> dict[str, Any]:
    """A line published from positions: its items as ``ocp_origin`` reads them for the run, grouped by kind."""
    if origin is None:
        why = ("состав суммы из позиций не восстановлен: чтение данных прогона завершилось ошибкой" if failed else
               "состав суммы из позиций в прогоне не сохранён")
        return _finish([], x.get("amount"), [_gap("no_origin", None, why)])
    rows: list[dict[str, Any]] = []
    for kind in ("position", "cash", "liability"):  # the totals of all items, not of the largest listed
        if k := (origin.get("kinds") or {}).get(kind):
            rows.append(_row(kind, k["amount"], k["count"]))
    absent = [] if origin.get("matches") else [_gap(
        "origin_mismatch", None, f"разбивка по позициям не сходится с суммой строки: в ней {_n(origin['sum'])}")]
    return _finish(rows, x.get("amount"), absent)


def line_breakdown(x: dict[str, Any], origin: dict[str, Any] | None = None, *, origin_failed: bool = False
                   ) -> dict[str, Any]:
    """The breakdown of one published line of a saved block (the module docstring)."""
    if isinstance(x.get("parts"), dict):
        return _parts_breakdown(x)
    if x.get("source") in (O.DERIVED, O.DERIVED_STALE):
        return _derived_breakdown(x, origin, origin_failed)
    if x.get("source") == O.REPORT:
        return _outside_breakdown(x)
    return _finish([], x.get("amount"), [_gap("unknown_source", None, "источник строки в прогоне не распознан: "
                                                                       "слагаемых нет")])


def _origin_key(x: dict[str, Any]) -> tuple[Any, Any, Any]:
    return x.get("entity"), x.get("book_id"), x.get("ccy")


def day(con, anchor: dict[str, Any], on: date) -> dict[str, Any] | None:
    """The OCP of the date ``on`` as the history has it, each line with its ``breakdown``; None: the date has no saved
    run to show (no default run of the date, the anchor not being on it). ValueError: ``on`` after the anchor's date."""
    h = history(con, anchor, *period(anchor, on, on))
    if not h["days"]:
        return None
    d = h["days"][0]
    row = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'ocp'", (d["runId"],)).fetchone()
    block = row["data"] if row else None
    pub = (block or {}).get("published") or {}
    raw = pub.get("by_book") if isinstance(pub.get("by_book"), list) else []
    origins: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    failed = False
    if any(x.get("source") in (O.DERIVED, O.DERIVED_STALE) for x in raw):
        try:
            origins = {_origin_key(o): o for o in ocp_origin.origins(con, d["runId"], block, with_cash=False)}
        except Exception:  # the items only explain a figure: the day goes on without them, said
            log.exception("run %s: the OCP origins of the day failed", d["runId"])
            failed = True
    lines = []
    for x, y in zip(raw, d["lines"], strict=True):  # the history's lines are published.by_book one to one
        lines.append({**y, "breakdown": line_breakdown(x, origins.get(_origin_key(x)), origin_failed=failed)})
    state = d["state"]
    absent = None if lines else {"code": state if state in NO_OCP_REASON else "no_published_ocp",
                                 "reason": NO_OCP_REASON.get(state, NO_BLOCK_REASON)}
    kinds = [x["breakdown"]["status"] for x in lines]
    names = {p["key"]: p["name"] for p in (block or {}).get("parts") or [] if isinstance(p, dict) and p.get("key")}
    return {**d, "books": h["books"], "lines": lines, "anchorRunId": anchor["run_id"], "asOf": anchor["business_date"],
            "partNames": {**PART_NAMES, **names}, "absent": absent,
            "check": {"lines": len(lines), "full": kinds.count(FULL), "partial": kinds.count(PARTIAL),
                      "none": kinds.count(NONE), "sumsMatch": all(x["breakdown"]["matches"] is not False
                                                                  for x in lines)}}
