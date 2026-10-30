"""The published OCP by day (TDM#446): what the saved runs published, no new OCP computation.

One day per date of the period: its default EOD run, then the anchor run on its date (``pnl_history.period_runs``,
as the PnL period routes). Per day the published OCP of the run's ``ocp`` block (``publish.ocp_run`` v2, TDM#416:
the bank's OCP report + the components outside it, derived from positions where no report unit covers) as its lines
per book or report part, and the run's rates of the currencies (``fx``: the market rate the run used, MOEX 19:00
with its fallbacks, and the CBR rate). A line's ``book`` is null for an entity-level report unit: its figure is the
part's, ``books`` the books it holds; nothing is spread over them. A run without the block (before B2), with a v1
block (no ``published``) or a failed one: the day has no lines and says why in ``state`` / ``basis``.

The report's own rates (``reportFx``, owner, 30.10.2026): ₽ per unit of each currency as the bank's OCP report gives
it (``rate_rub_per_unit`` of the ``ocp`` rows), from the block's reconciliation (``reported_rate`` per report scope and
currency; ``published.by_book`` does not carry it): the pages value a currency the run has no rate of at it. Two
scopes with different rates: the one of the newer ``valid_date``. A block without them (no report, before B2): ``{}``.

The books of the period (``books``): name, entity and desk of each book from the latest run of the period that has
it, for the pages' filter and labels when a book left the current run's reference.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ..publish import ocp_run
from .pnl_history import period_runs

DEFAULT_DAYS = 92
"""The period when ``from`` is not given: three months back from ``to``."""
MAX_PERIOD_DAYS = 400

SQL = ("SELECT run_id, kind, CASE WHEN kind = 'ocp' THEN jsonb_build_object("
       "'status', data->'status', 'basis', data->'published'->'basis', 'lines', data->'published'->'by_book',"
       " 'rates', (SELECT jsonb_agg(jsonb_build_object('ccy', c->'ccy', 'rate', c->'reported_rate',"
       " 'valid', c->'valid_date')) FROM jsonb_array_elements(CASE WHEN jsonb_typeof(data->'reconciliation')"
       " = 'array' THEN data->'reconciliation' ELSE '[]' END) c WHERE jsonb_typeof(c->'reported_rate') = 'number'))"
       " ELSE data END AS data FROM run_reference WHERE run_id = ANY(%s) AND kind IN ('ocp', 'fx', 'books')")


def period(anchor: dict[str, Any], start: date | None, end: date | None) -> tuple[date, date]:
    """(from, to) of a request: ``to`` defaults to the anchor's date, ``from`` to ``DEFAULT_DAYS`` before ``to``.
    ValueError: from > to, to after the anchor's date, longer than ``MAX_PERIOD_DAYS``."""
    end = end or anchor["business_date"]
    start = start or end - timedelta(days=DEFAULT_DAYS)
    if start > end or end > anchor["business_date"]:
        raise ValueError("Expected from <= to <= selected run date")
    if (end - start).days + 1 > MAX_PERIOD_DAYS:
        raise ValueError(f"Period must not exceed {MAX_PERIOD_DAYS} calendar days inclusive")
    return start, end


def _line(x: dict[str, Any]) -> dict[str, Any]:
    return {"entity": x.get("entity"), "book": x.get("book_id"), "books": x.get("books") or [], "ccy": x.get("ccy"),
            "amount": x.get("amount"), "source": x.get("source"), "reported": x.get("reported"),
            "validDate": x.get("valid_date")}


def _fx(rows: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """currency -> {market, cbr, source}. A run with no CBR rate at all (before R26) valued on the CBR rate: its
    rates are the CBR ones too, as ``concepts_run``'s ``FX_CBR``. A run with CBR rates for some currencies only: a
    currency without one has ``cbr`` null, never its market rate (the USD equivalent would mix the two sources)."""
    rows = [r for r in rows or [] if r.get("ccy") and r.get("ccy") != "RUB" and r.get("rate") is not None]
    before_cbr = not any(r.get("rate_cbr") is not None for r in rows)
    return {r["ccy"]: {"market": r["rate"], "cbr": r["rate"] if before_cbr else r.get("rate_cbr"),
                       "source": r.get("rate_source")} for r in rows}


def _report_fx(rows: list[dict[str, Any]] | None) -> dict[str, float]:
    """currency -> the report's rate, ₽ per unit (the module docstring): of the newest ``valid_date``, a positive one."""
    out: dict[str, tuple[str, float]] = {}
    for r in rows or []:
        ccy, rate, valid = r.get("ccy"), r.get("rate"), str(r.get("valid") or "")
        if ccy and ccy != "RUB" and rate and rate > 0 and (ccy not in out or valid > out[ccy][0]):
            out[ccy] = (valid, rate)
    return {c: v for c, (_, v) in sorted(out.items())}


def history(con, anchor: dict[str, Any], start: date, end: date) -> dict[str, Any]:
    """The published OCP of each day of ``start``..``end`` (see the module docstring)."""
    runs = period_runs(con, anchor, start, end)["runs"]
    ref: dict[int, dict[str, Any]] = {r["run_id"]: {} for r in runs}
    for r in con.execute(SQL, ([r["run_id"] for r in runs],)).fetchall():
        ref[r["run_id"]][r["kind"]] = r["data"]
    days, books = [], {}
    for r in runs:
        x = ref[r["run_id"]]
        block = x.get("ocp") or {}
        for b in x.get("books") or []:
            books[b["book_id"]] = {"name": b.get("name"), "entity": b.get("entity"), "desk": b.get("desk")}
        days.append({"date": r["business_date"], "runId": r["run_id"], "runType": r["run_type"],
                     "state": block.get("status") or ocp_run.NOT_COMPUTED, "basis": block.get("basis"),
                     "fx": _fx(x.get("fx")), "reportFx": _report_fx(block.get("rates")),
                     "lines": [_line(y) for y in block.get("lines") or []]})
    return {"runId": anchor["run_id"], "asOf": anchor["business_date"], "from": start, "to": end,
            "days": days, "books": books}
