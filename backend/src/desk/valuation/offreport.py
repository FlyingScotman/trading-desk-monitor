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
- state ``invalid`` when its terms cannot be read (the reason in ``why``); ``suspect`` when it looks like a deal the
  positions already have: its number is the ``deal_id`` or ``position_id`` of a row of another book, or a row of the
  same book of the same kind (FX / other), currency and date has no terms (it is not included, to count nothing
  twice, unless ``SUSPECT_ARE_INCLUDED``); else ``included`` or ``excluded`` by the switch.

The three choices the owner has not answered yet (questions of 04.11.2026) are the constants below.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
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
STATES = ("excluded", "included", "suspect", "invalid")
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


def _suspect(book: str, trade: str, ts: list[dict[str, Any]], is_fx: bool, last: date | None,
             rows: list[tuple[str, str, dict[str, Any]]], linked: dict[str, Any], ids: dict[str, set[str]],
             ccys: set[str]) -> str:
    if ids.get(trade, set()) - {book}:
        return f"the number {trade} is a deal or position of the positions report in another book"
    ends = {e for t in ts if (e := _leg_end(t)) is not None} or ({last} if last else set())
    for pid, rbook, x in rows:
        if rbook != book or pid in linked or (_row_class(x) == "fx") != is_fx:
            continue
        end = x.get("value_date") if is_fx else x.get("maturity_date")
        c = dv._ccy(x.get("ccy"))
        if end is not None and end in ends and c in ccys:
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
        state, why = ("included" if include else "excluded"), ""
        try:
            dv.request_of(f"{book}|{trade}", book, {}, ts)
        except dv.NotValued as e:
            state, why = "invalid", str(e)
        else:
            if why := _suspect(book, trade, ts, is_fx, last, rows, linked, ids, _leg_ccys(ts)):
                state = "suspect" if not SUSPECT_ARE_INCLUDED else state
        out.append(OffReportTrade(book, trade, kinds, last, min(dates) if dates else None, state, why,
                                  book not in inputs.valid_dates, tuple(ts)))
    return out, matured


def row_of(t: OffReportTrade) -> dict[str, Any]:
    """The positions row the plan gives an included trade (``positions._derivatives``): a synthetic row, which core
    values from the trade's terms (``derivatives.request_of``). ``off_report`` and ``trade_date`` say where it comes from."""
    ccys = _leg_ccys(list(t.terms))
    ccy = sorted(ccys)[0] if ccys else "RUB"
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
    """``lineage.positions.off_report``: the switch, the counts and the list (at most ``LIST_MAX``)."""
    by_book: dict[str, int] = defaultdict(int)
    by_kind: dict[str, int] = defaultdict(int)
    for t in trades:
        by_book[t.book] += 1
        by_kind[t.kind] += 1
    count = lambda s: sum(t.state == s for t in trades)  # noqa: E731
    return {"mode": mode, "trades": len(trades), "books": dict(sorted(by_book.items())),
            "by_kind": dict(sorted(by_kind.items())), "included": count("included"), "valued": valued,
            "suspect": count("suspect"), "invalid": count("invalid"), "excluded": count("excluded"),
            "matured": matured, "list": [t.entry() for t in trades[:LIST_MAX]],
            **({"list_truncated": len(trades)} if len(trades) > LIST_MAX else {})}


def canon_terms(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(json.dumps({k: v for k, v in r.items() if not k.startswith("_") and k != "x"},
                             sort_keys=True, default=str) for r in rows)


def stamp(inputs: PositionInputs, on: date, mode: str) -> PositionInputs:
    """``inputs`` with the off-report switch in ``private_inputs_hash`` (``trades.intraday_inputs``, the one place every
    run and every comparison of a run's hash goes through): with ``include`` and trades included or suspect on ``on``,
    the hash also covers the mode, their keys and the terms of those of books without positions, so a switch recomputes
    the dates it changes. ``exclude``, or no such trades: unchanged."""
    if mode != "include":
        return inputs
    trades, _ = off_report_trades(inputs, on, True)
    live = [t for t in trades if t.state in ("included", "suspect")]
    if not live:
        return inputs
    extra_terms = [r for r in inputs.other_book_terms if any(
        str(r.get("book_id")) == t.book and str(r.get("trade_id")) == t.trade_id for t in live if t.other_book)]
    canon = json.dumps([inputs.private_inputs_hash, "off_report:include", sorted(t.key for t in live),
                        canon_terms(extra_terms)], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    from dataclasses import replace
    return replace(inputs, private_inputs_hash=hashlib.sha256(canon.encode()).hexdigest())
