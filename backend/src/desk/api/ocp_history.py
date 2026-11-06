"""The published OCP by day (TDM#446): what the saved runs published, no new OCP computation.

One day per date of the period: its default EOD run, then the anchor run on its date (``pnl_history.period_runs``,
as the PnL period routes). Per day the published OCP of the run's ``ocp`` block (``publish.ocp_run`` v2, TDM#416:
the bank's OCP report + the components outside it, derived from positions where no report unit covers) as its lines
per book or report part, and the run's rates of the currencies (``fx``: the market rate the run used, MOEX 19:00
with its fallbacks, and the CBR rate). A line's ``book`` is null for an entity-level report unit: its figure is the
part's, ``books`` the books it holds; nothing is spread over them. A run without the block (before B2), with a v1
block (no ``published``) or a failed one: the day has no lines and says why in ``state`` / ``basis``.

The report's own rates (``reportFx``, owner, 30.10.2026): ₽ per unit of each currency by the bank's OCP report, from
the block's reconciliation per report scope and currency (``published.by_book`` does not carry them): the pages value
a currency the run has no rate of at it. Per scope the report's ``rate_rub_per_unit`` (``reported_rate``), checked
against ``|reported_rub| / |reported|`` (₽ per unit as the report actually converts; ``ocp_rub`` may come unsigned):
their ratio outside ``RATE_BAND`` (a rate quoted per 10 or 100 units, as JPY or KZT can be): no rate. Without a rate,
that implied one (the figure without the derivatives when the report gives them apart, private/1.7: ``ocp_rub``
follows ``ocp_ccy``). Only the units the day publishes as the report: none under ``published`` ``derived``, none of a
unit older than the policy's ``report_max_age_days`` (``report_plus_outside``; published from positions,
``derived_stale_report``); under ``report_base_plus_day`` the report sets the day's published lines start from
(their ``set_id``, as the run chose its base on its own calendar). Two scopes: the newer ``valid_date``, then a book's scope before an entity's,
then the first book. A block without them (no report, before B2): ``{}``.

A currency of a day's lines the run has no rate of (a run before TDM#602 published rates of its rows' currencies only:
JPY, KZT of the OCP report had none), and USD for the USD equivalent: the CBR rate per unit in force on the date from
the stored hub history (``md_hub_fx``, ``cbr_official``; the latest of the ``HUB_FX_DAYS`` calendar days up to the
date), as both its market and CBR rate, ``source`` ``HUB_CBR_SOURCE`` (TDM#534). No stored history: no rate, as before.

The books of the period (``books``): name, entity, desk and book type (``type``) of each book from the latest run of
the period that has it, for the pages' filter and labels when a book left the current run's reference; a book of the
OCP without positions in a run (TDM#556: only the OCP report knows it) from the run's OCP block (``books``, marked
``ocpOnly``). A book without a recorded type has the type derived once for the whole period (a bond in any run of
the period: ``TRADING``, else ``DERIVS``; marked ``typeDerived``), so it does not jump between days. A day's ``books``
holds only what differs from the period's ``books``; the pages merge it over them (TDM#567).
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from ..publish import ocp_run
from ..rules import ocp as O
from .ocp_line import book_meta, book_type, method_note, ocp_line
from .pnl_history import period_runs

DEFAULT_DAYS = 92
"""The period when ``from`` is not given: three months back from ``to``."""
MAX_PERIOD_DAYS = 400

RATE_BAND = (0.5, 2.0)
"""``rate_rub_per_unit`` / (``ocp_rub`` / ``ocp_ccy``) of a report scope outside this: the scope gives no rate."""

HUB_FX_DAYS = 10
"""How many calendar days back the stored CBR rate of a day may be (the CBR sets one every business day)."""
HUB_CBR_SOURCE = "CBR (история хаба)"
"""``source`` of a day's rate taken from the stored hub history, not from the run."""

LINE_KEYS = ("entity", "book_id", "books", "ccy", "amount", "source", "components", "reported", "valid_date",
             "base_date", "set_id", "base_kind", "parts", "base_older")
"""The keys of a published line the history reads: never its ``contributions`` (block v3), which stay in the run's
``/ocp``; the SQL picks them, so a long period does not carry them out of the store."""

SQL = ("SELECT run_id, kind, CASE WHEN kind = 'ocp' THEN jsonb_build_object("
       "'status', data->'status', 'basis', data->'published'->'basis',"
       " 'lines', (SELECT jsonb_agg(jsonb_build_object(" + ", ".join(f"'{k}', x->'{k}'" for k in LINE_KEYS) + "))"
       " FROM jsonb_array_elements(CASE WHEN jsonb_typeof(data->'published'->'by_book') = 'array'"
       " THEN data->'published'->'by_book' ELSE '[]' END) x),"
       " 'max_age', data->'policy'->'report_max_age_days', 'books', data->'books',"
       " 'rates', (SELECT jsonb_agg(jsonb_build_object('ccy', c->'ccy', 'rate', c->'reported_rate',"
       " 'rub', c->'reported_rub', 'amount', COALESCE(c->'reported_base', c->'reported'), 'valid', c->'valid_date',"
       " 'book', c->'book_id', 'set', c->'set_id'))"
       " FROM jsonb_array_elements(CASE WHEN jsonb_typeof(data->'reconciliation') = 'array'"
       " THEN data->'reconciliation' ELSE '[]' END) c WHERE jsonb_typeof(c->'reported_rate') = 'number'"
       " OR jsonb_typeof(c->'reported_rub') = 'number'))"
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


def _fx(rows: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """currency -> {market, cbr, source}. A run with no CBR rate at all (before R26) valued on the CBR rate: its
    rates are the CBR ones too, as ``concepts_run``'s ``FX_CBR``. A run with CBR rates for some currencies only: a
    currency without one has ``cbr`` null, never its market rate (the USD equivalent would mix the two sources)."""
    rows = [r for r in rows or [] if r.get("ccy") and r.get("ccy") != "RUB" and r.get("rate") is not None]
    before_cbr = not any(r.get("rate_cbr") is not None for r in rows)
    return {r["ccy"]: {"market": r["rate"], "cbr": r["rate"] if before_cbr else r.get("rate_cbr"),
                       "source": r.get("rate_source")} for r in rows}


def _hub_cbr(con, wanted: dict[date, set[str]]) -> dict[date, dict[str, float]]:
    """day -> currency -> the CBR rate per unit in force on it from the stored hub history (the module docstring), for
    the (day, currency) of ``wanted``; nothing found: the day or currency is left out."""
    ccys = sorted({c for cs in wanted.values() for c in cs})
    if not ccys:
        return {}
    lo, hi = min(wanted) - timedelta(days=HUB_FX_DAYS), max(wanted)
    series: dict[str, list[tuple[date, float]]] = {}
    for r in con.execute("SELECT pair, date, per_unit FROM md_hub_fx WHERE kind = 'cbr_official' AND pair = ANY(%s)"
                         " AND date BETWEEN %s AND %s AND per_unit IS NOT NULL AND per_unit > 0 ORDER BY date",
                         ([f"{c}/RUB" for c in ccys], lo, hi)).fetchall():
        series.setdefault(r["pair"][:3], []).append((r["date"], float(r["per_unit"])))
    out: dict[date, dict[str, float]] = {}
    for d, cs in wanted.items():
        for c in cs:
            v = next((v for x, v in reversed(series.get(c, [])) if x <= d and (d - x).days <= HUB_FX_DAYS), None)
            if v is not None:
                out.setdefault(d, {})[c] = v
    return out


def _num(x: Any) -> float | None:
    return float(x) if isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x) else None


def _day(x: Any) -> date | None:
    try:
        return date.fromisoformat(str(x)[:10]) if x else None
    except ValueError:
        return None


def _unit_rate(r: dict[str, Any]) -> float | None:
    """₽ per unit of one report scope (the module docstring), or None: the stated rate when its scale agrees with the
    report's own figures (or they are not given), else the implied one when no rate is stated."""
    rate, rub, amount = _num(r.get("rate")), _num(r.get("rub")), _num(r.get("amount"))
    implied = abs(rub) / abs(amount) if rub and amount else None
    if rate is None or rate <= 0:
        return implied
    if implied is not None and not RATE_BAND[0] <= rate / implied <= RATE_BAND[1]:
        return None
    return rate


def _report_fx(block: dict[str, Any], on: date) -> dict[str, float]:
    """currency -> the report's rate, ₽ per unit, of the units the day ``on`` publishes (the module docstring)."""
    basis = block.get("basis")
    if basis not in (O.PublishedBasis.REPORT_PLUS_OUTSIDE.value, O.PublishedBasis.REPORT_BASE_PLUS_DAY.value):
        return {}
    # report_base_plus_day: the report sets its published lines start from (their set_id), as the run decided them;
    # report_plus_outside: the units no older than the policy's report_max_age_days
    sets = {x.get("set_id") for x in block.get("lines") or [] if x.get("source") == O.REPORT and x.get("set_id")}
    age = block.get("max_age")
    if not isinstance(age, int) or isinstance(age, bool):
        age = O.PROVISIONAL_POLICY.report_max_age_days
    by: dict[str, list[tuple[str, bool, str, float]]] = {}
    for r in block.get("rates") or []:
        ccy, valid = r.get("ccy"), _day(r.get("valid"))
        old = r.get("set") not in sets if basis == O.PublishedBasis.REPORT_BASE_PLUS_DAY.value else \
            valid is not None and (on - valid).days > age
        if not ccy or ccy == O.PROVISIONAL_POLICY.reporting_ccy or old:
            continue
        if (rate := _unit_rate(r)) is not None:
            by.setdefault(ccy, []).append((str(valid or ""), r.get("book") is not None, str(r.get("book") or ""), rate))
    out = {}
    for ccy, xs in sorted(by.items()):
        xs.sort(key=lambda x: x[2])
        xs.sort(key=lambda x: (x[0], x[1]), reverse=True)  # stable: the newer unit, a book's, the first book
        out[ccy] = xs[0][3]
    return out


def _bond_books(con, run_ids: list[int], books: set[str]) -> set[str]:
    """The books among ``books`` that hold a bond in any run of the period: a book without a recorded type is
    ``TRADING`` then, else ``DERIVS`` (the run's rule, applied once to the whole period so the type does not jump
    between days of the chart)."""
    if not books:
        return set()
    return {r["book_id"] for r in con.execute(
        "SELECT DISTINCT book_id FROM run_position WHERE run_id = ANY(%s) AND book_id = ANY(%s)"
        " AND fact->>'instr_type' = 'BOND'", (run_ids, sorted(books))).fetchall()}


def _day_books(x: dict[str, Any], block: dict[str, Any], bonds: set[str]) -> dict[str, dict[str, Any]]:
    """The books of one day's run as the run gave them (``book_meta``): an unrecorded type derived from the period's
    bonds (``typeDerived``); a book only the OCP knows (TDM#556) marked ``ocpOnly``, no type derived."""
    out = {}
    for b in x.get("books") or []:
        rec = b.get("book_type")
        out[b["book_id"]] = book_meta(b["book_id"], b, None if rec else book_type(None, b["book_id"] in bonds),
                                      derived=not rec)
    for b, m in (block.get("books") or {}).items():
        if isinstance(m, dict) and b not in out:
            out[b] = book_meta(b, m, ocp_only=True)
    return out


def history(con, anchor: dict[str, Any], start: date, end: date) -> dict[str, Any]:
    """The published OCP of each day of ``start``..``end`` (see the module docstring)."""
    runs = period_runs(con, anchor, start, end)["runs"]
    ref: dict[int, dict[str, Any]] = {r["run_id"]: {} for r in runs}
    for r in con.execute(SQL, ([r["run_id"] for r in runs],)).fetchall():
        ref[r["run_id"]][r["kind"]] = r["data"]
    untyped = {b["book_id"] for x in ref.values() for b in x.get("books") or [] if not b.get("book_type")}
    bonds = _bond_books(con, [r["run_id"] for r in runs], untyped)
    days, books, mine = [], {}, []
    # the currencies of a day's lines without the run's rate (and USD, the equivalent's): the hub's stored CBR rate
    wanted: dict[date, set[str]] = {}
    for r in runs:
        x = ref[r["run_id"]]
        have = set(_fx(x.get("fx")))
        need = {y.get("ccy") for y in (x.get("ocp") or {}).get("lines") or []} - {None, "RUB"}
        if need - have:
            wanted[r["business_date"]] = (need | {"USD"}) - have
    hub = _hub_cbr(con, wanted)
    for r in runs:
        x = ref[r["run_id"]]
        block = x.get("ocp") or {}
        mine.append(_day_books(x, block, bonds))
        books.update(mine[-1])  # the latest run of the period that has the book
    for r, m in zip(runs, mine, strict=True):
        x = ref[r["run_id"]]
        block = x.get("ocp") or {}
        # a day carries only its differences from the period's reference (the pages merge them over it)
        diff = {b: v for b, v in m.items() if books.get(b) != v}
        lines = [ocp_line(y) for y in block.get("lines") or []]
        fx = _fx(x.get("fx"))
        for c, v in hub.get(r["business_date"], {}).items():
            fx.setdefault(c, {"market": v, "cbr": v, "source": HUB_CBR_SOURCE})
        days.append({"date": r["business_date"], "runId": r["run_id"], "runType": r["run_type"],
                     "state": block.get("status") or ocp_run.NOT_COMPUTED, "basis": block.get("basis"),
                     "methodNote": method_note(block.get("basis"), lines), "books": diff,
                     "fx": fx, "reportFx": _report_fx(block, r["business_date"]), "lines": lines})
    return {"runId": anchor["run_id"], "asOf": anchor["business_date"], "from": start, "to": end,
            "days": days, "books": books}
