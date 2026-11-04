"""Derivatives outside the positions report (TDM#468, TDM#503): the switch ``off_report_derivatives``.

A ``trade_terms`` trade (a forward, NDF, FX swap, IRS, OIS, CCS or REPO) that no positions row names is *off the
report*: a trade made after the report's cut, or of a book the positions report does not cover. The owner's decision
of 04.11.2026 is one switch: ``exclude`` (the default, the trades are not in risk, PnL or OCP, and the screens list
them) or ``include`` (each is valued from its terms as a position of its own, marked «вне отчёта позиций», and goes
into risk, PnL and OCP as any other position).

``off_report_trades`` is the one selection, a pure function of the run's inputs and date; the plan
(``positions._derivatives``), the screens (lineage) and the inputs hash (``stamp``) all read it:

- the trades of ``LINKED_KINDS`` the linking of ``derivatives._link`` leaves, grouped by (book, ``trade_id``), from the
  books with positions and, with ``BOOKS_WITHOUT_REPORT``, from the books of ``PositionInputs.other_book_terms``;
- a trade whose last date is more than ``FLOW_DAYS`` before the date is dropped (counted as ``matured``): its last day
  is a row (PV 0) for ``FLOW_DAYS`` after it, as for a linked trade (``derivatives._settled_row``);
- with ``exclude`` a trade whose last date is before the date (closed within ``FLOW_DAYS``) is ``closed``: not a gap, only
  counted; with ``include`` it is ``included`` (its last day has a PnL);
- state ``invalid`` when its terms cannot be read (the reason in ``why``); ``suspect`` when it looks like a deal the
  positions already have: its number is the ``deal_id`` or ``position_id`` of a row of another book, or a row of the
  same book of the same kind (FX / other), currency and date has no terms (it is not included, to count nothing
  twice, unless ``SUSPECT_ARE_INCLUDED``); else ``included`` or ``excluded`` by the switch.

The three choices the owner has not answered yet (questions of 04.11.2026) are the constants below.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from . import derivatives as dv

if TYPE_CHECKING:
    from ..store import PositionInputs

BOOKS_WITHOUT_REPORT = True
"""Question 3: count the trades of a book that has no positions set when the books reference lists it and has not
switched it off (``active``). False: only the books with a positions set."""
SUSPECT_ARE_INCLUDED = False
"""Question 2: include a trade that looks like one the positions already have. False: not included, listed as «possibly
already in the positions report»."""
LIST_MAX = 200
"""The longest list of trades in the run's lineage."""
STATES = ("excluded", "included", "suspect", "invalid", "closed")
LISTED_FIRST = ("suspect", "invalid", "excluded")
"""The states a screen lists, first when the list is cut."""
GAP_KINDS = {"fx_forward": "fx_forward", "fx_swap": "fx_swap", "ccs": "swap"}
"""Kinds of trades whose nominal the OCP takes out of the report's base when it gives the kind apart (``ocp.derivatives_ccy``
of the report) -> that kind in the report."""
SAME_AMOUNT = 1e-6
MODES = ("exclude", "include")
LABELS = {"irs": "IRS", "ois": "OIS", "ccs": "CCS", "repo": "REPO", "fx_forward": "FX forward", "fx_swap": "FX swap"}


@dataclass(frozen=True)
class OffReportTrade:
    book: str
    trade_id: str
    kinds: tuple[str, ...]
    last: date | None
    """The trade's last date: an FX leg's value date, a swap's, CCS' or REPO's ``maturity_date`` (None: an open REPO)."""
    trade_date: date | None
    """The earliest ``trade_date`` of its legs."""
    state: str
    why: str = ""
    other_book: bool = False
    """The book has no positions set (its terms are ``PositionInputs.other_book_terms``)."""
    terms: tuple[dict[str, Any], ...] = field(default=(), compare=False, repr=False)

    @property
    def key(self) -> str:
        return f"{self.book}|{self.trade_id}"

    @property
    def kind(self) -> str:
        return "+".join(self.kinds)

    @property
    def is_fx(self) -> bool:
        return set(self.kinds) <= set(dv.FX_KINDS)

    def entry(self) -> dict[str, Any]:
        return {"book": self.book, "trade_id": self.trade_id, "kind": self.kind,
                "maturity": self.last.isoformat() if self.last else None, "state": self.state,
                **({"why": self.why} if self.why else {})}


def _row_class(x: dict[str, Any]) -> str:
    return "fx" if str(x.get("asset_class") or "").lower() == "fx" else "otc"


def _leg_end(t: dict[str, Any]) -> date | None:
    try:
        return dv._day(t.get("value_date") if t.get("product_kind") in dv.FX_KINDS else t.get("maturity_date"))
    except (TypeError, ValueError):
        return None


def _leg_ccys(ts: list[dict[str, Any]]) -> set[str]:
    """The currencies of a trade's legs, RUB only when it has no other."""
    out = {c for t in ts for k in ("base_ccy", "quote_ccy", "ccy") if (c := dv._ccy(t.get(k)))}
    return (out - {"RUB"}) or out


def _rows(inputs: PositionInputs) -> list[tuple[str, str, dict[str, Any]]]:
    """(position_id, book, row) of the rows a trade's terms can link to, as ``positions._derivatives`` takes them: the
    non-bond rows, and a row without ``asset_class`` but with a ``deal_id`` (TDM#501). Dates as date."""
    out = []
    for pid, book, _, _ in inputs.rows:
        x = inputs.extra.get(pid)
        if x is None:
            continue
        cls = x.get("asset_class")
        if (cls or "bond").lower() != "bond" or (not cls and x.get("deal_id")):
            out.append((pid, book, {**x, **{k: dv._day(x[k]) for k in ("value_date", "maturity_date")
                                              if isinstance(x.get(k), str)}}))
    return out


def _amounts(ts: list[dict[str, Any]], ccy: str) -> list[float]:
    """The unsigned amounts of a trade's legs in ``ccy`` (and their sum): what a row's ``amount_ccy`` could be."""
    out = []
    for t in ts:
        for k, a in (("base_ccy", "notional_base"), ("quote_ccy", "notional_quote"), ("ccy", "notional_ccy")):
            if dv._ccy(t.get(k)) == ccy and (v := dv._num(t.get(a))) is not None:
                out.append(abs(v))
    return [*out, math.fsum(out)] if out else out


def _suspect(book: str, trade: str, ts: list[dict[str, Any]], is_fx: bool, last: date | None,
             rows: list[tuple[str, str, dict[str, Any]]], linked: dict[str, Any], ids: dict[str, set[str]],
             ccys: set[str]) -> str:
    """Why the trade looks like one the positions report has, else "". The amount is compared with a row of the same book,
    kind, currency and date, so a row is taken to be one deal: a summary row per currency and date does not mark a
    trade that is inside it."""
    if ids.get(trade):
        where = "another book" if ids[trade] - {book} else "this book"
        return f"the number {trade} is a deal or position of the positions report in {where}"
    ends = {e for t in ts if (e := _leg_end(t)) is not None} or ({last} if last else set())
    for pid, rbook, x in rows:
        if rbook != book or pid in linked or (_row_class(x) == "fx") != is_fx:
            continue
        end = x.get("value_date") if is_fx else x.get("maturity_date")
        c = dv._ccy(x.get("ccy"))
        if end is None or end not in ends or c not in ccys:
            continue
        amount = x.get("amount_ccy")
        if amount is not None and not any(abs(abs(float(amount)) - a) <= SAME_AMOUNT * max(a, 1.0)
                                          for a in _amounts(ts, c or "")):
            continue  # the same kind, currency and date, but another amount: another deal
        return f"the positions report has a row {pid} of the same kind, currency {c} and date {end} without terms"
    return ""


def off_report_trades(inputs: PositionInputs, on: date, include: bool = True) -> tuple[list[OffReportTrade], int]:
    """The trades off the positions report on ``on`` and how many matured trades were left out: see the module
    docstring. ``include``: whether the switch is ``include`` (decides ``included`` against ``excluded``). A pure
    function of the inputs and the date. Sorted by (book, trade_id)."""
    rows = _rows(inputs)
    pool = [*inputs.trade_terms, *(inputs.other_book_terms if BOOKS_WITHOUT_REPORT else ())]
    linked, left, _ = dv._link(rows, pool)
    ids: dict[str, set[str]] = defaultdict(set)
    for pid, book, _, _ in inputs.rows:
        ids[pid].add(book)
        if (d := (inputs.extra.get(pid) or {}).get("deal_id")):
            ids[str(d)].add(book)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for t in left:
        groups[(str(t.get("book_id")), str(t.get("trade_id")))].append(t)
    out, matured = [], 0
    for (book, trade), ts in sorted(groups.items()):
        ends = [_leg_end(t) for t in ts]
        last = None if None in ends else max(e for e in ends if e is not None)
        if last is not None and last < on - timedelta(days=dv.FLOW_DAYS):
            matured += 1
            continue
        kinds = tuple(sorted({str(t.get("product_kind")) for t in ts}))
        dates = []
        for t in ts:
            try:
                if (d := dv._day(t.get("trade_date"))) is not None:
                    dates.append(d)
            except (TypeError, ValueError):
                pass
        is_fx = set(kinds) <= set(dv.FX_KINDS)
        state, why = ("included" if include else "closed" if last is not None and last < on else "excluded"), ""
        try:
            dv.request_of(f"{book}|{trade}", book, {}, ts)
        except dv.NotValued as e:
            state, why = "invalid", str(e)
        else:
            if why := _suspect(book, trade, ts, is_fx, last, rows, linked, ids, _leg_ccys(ts)):
                if state != "closed" and not SUSPECT_ARE_INCLUDED:
                    state = "suspect"
                else:
                    why = ""
        out.append(OffReportTrade(book, trade, kinds, last, min(dates) if dates else None, state, why,
                                  book not in inputs.valid_dates, tuple(ts)))
    return out, matured


def row_of(t: OffReportTrade) -> dict[str, Any]:
    """The positions row the plan gives an included trade (``positions._derivatives``): a synthetic row, which core
    values from the trade's terms (``derivatives.request_of``). ``off_report`` and ``trade_date`` say where it comes from."""
    ccys = _leg_ccys(list(t.terms))
    ccy = min(ccys) if ccys else "RUB"
    ts = t.terms
    if t.is_fx:
        name = f"{ts[0].get('base_ccy')}/{ts[0].get('quote_ccy')} {LABELS.get(t.kinds[0], t.kinds[0])} {t.trade_id}"
    else:
        idx = next((str(x["index_code"]) for x in ts if x.get("index_code")), "")
        name = " ".join(p for p in (LABELS.get(t.kinds[0], t.kinds[0]), idx, ccy if ccy != "RUB" else "",
                                    f"{t.last:%d.%m.%Y}" if t.last else "", t.trade_id) if p)
    return {"asset_class": "fx" if t.is_fx else "otc", "ccy": ccy, "deal_id": t.trade_id,
            "source_instrument_id": t.trade_id, "source_instrument_name": name,
            **({"value_date" if t.is_fx else "maturity_date": t.last} if t.last else {}),
            "off_report": True, **({"trade_date": t.trade_date} if t.trade_date else {})}


def summary(trades: list[OffReportTrade], matured: int, mode: str, valued: int = 0) -> dict[str, Any]:
    """``lineage.positions.off_report``: the switch, the counts and the list (at most ``LIST_MAX``, the suspect, unreadable
    and excluded first). ``trades`` leaves out the closed ones (``closed``: a trade that ended within ``FLOW_DAYS`` and
    is not valued: not a gap). ``gap_trades``: (book, trade_id, kind) of the excluded FX trades and CCS, whole, for the
    OCP gap (``publish.ocp_run``); not for the lineage."""
    live = [t for t in trades if t.state != "closed"]
    by_book: dict[str, int] = defaultdict(int)
    by_kind: dict[str, int] = defaultdict(int)
    for t in live:
        by_book[t.book] += 1
        by_kind[t.kind] += 1

    def count(state: str) -> int:
        return sum(t.state == state for t in trades)

    first = sorted(live, key=lambda t: LISTED_FIRST.index(t.state) if t.state in LISTED_FIRST else len(LISTED_FIRST))
    return {"mode": mode, "trades": len(live), "books": dict(sorted(by_book.items())),
            "by_kind": dict(sorted(by_kind.items())), "included": count("included"), "valued": valued,
            "suspect": count("suspect"), "invalid": count("invalid"), "excluded": count("excluded"),
            "closed": count("closed"), "matured": matured, "list": [t.entry() for t in first[:LIST_MAX]],
            **({"list_truncated": len(live)} if len(live) > LIST_MAX else {}),
            "gap_trades": [[t.book, t.trade_id, k] for t in live if t.state == "excluded"
                           for k in t.kinds if k in GAP_KINDS]}


def canon_terms(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(json.dumps({k: v for k, v in r.items() if not k.startswith("_") and k != "x"},
                             sort_keys=True, default=str) for r in rows)


def _live_keys(inputs: PositionInputs, on: date) -> list[str]:
    trades, _ = off_report_trades(inputs, on, True)
    return sorted(t.key for t in trades if t.state in ("included", "suspect"))


def stamp(inputs: PositionInputs, on: date, mode: str, prev: PositionInputs | None = None,
          prev_on: date | None = None) -> PositionInputs:
    """``inputs`` with the off-report switch in ``private_inputs_hash`` (``trades.intraday_inputs``, the one place every
    run and every comparison of a run's hash goes through): with ``include`` and trades included or suspect on ``on``
    (or on the day before, ``prev``: the PnL of ``on`` is measured against its valuation, so a trade that was off the
    report then and is in it now changes the PnL), the hash also covers the mode, their keys and the terms of those of
    books without positions, so a switch recomputes the dates it changes. ``exclude``, or no such trades: unchanged."""
    if mode != "include":
        return inputs
    trades, _ = off_report_trades(inputs, on, True)
    live = [t for t in trades if t.state in ("included", "suspect")]
    before = _live_keys(prev, prev_on) if prev is not None and prev_on is not None else []
    if not live and not before:
        return inputs
    extra_terms = [r for r in inputs.other_book_terms if any(
        str(r.get("book_id")) == t.book and str(r.get("trade_id")) == t.trade_id for t in live if t.other_book)]
    parts: list[Any] = [inputs.private_inputs_hash, "off_report:include", sorted(t.key for t in live),
                        canon_terms(extra_terms)]
    if before:
        parts.append({"previous": before})
    canon = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return replace(inputs, private_inputs_hash=hashlib.sha256(canon.encode()).hexdigest())
