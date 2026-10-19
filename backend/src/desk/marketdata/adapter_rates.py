"""Adapter ``mdhub_v1``, rates inputs: swap quote sets, fixings, CBR meetings and bounded calendars.

Slice S4 part; it is merged into ``adapter.py`` later (ADR-003: one module knows the wire). Decimal
strings are converted once here, and percent becomes a decimal rate (0.14 = 14 %) in ``_rate`` only.
Nothing is filled: a missing value stays ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from rub_fixed_income import BusinessCalendar

from .adapter import core_calendar
from .bundle import Snapshot, SnapshotError


def _rate(pct: Any) -> float | None:
    return None if pct is None else float(Decimal(pct) / 100)


def _date(v: Any) -> date | None:
    return None if v is None else date.fromisoformat(v)


def _ts(v: Any) -> datetime | None:
    return None if v is None else datetime.fromisoformat(v)


@dataclass(frozen=True)
class SwapQuote:
    tenor: str
    bid: float | None
    mid: float | None
    ask: float | None


@dataclass(frozen=True)
class QuoteSet:
    quote_set_id: int
    dataset: str
    source: str
    quote_basis: str
    snapshot_date: date
    fetched_at: datetime
    labels: tuple[str, ...]
    stale_reasons: tuple[str, ...]
    """The hub's dataset-level stale reasons that name this ``source/dataset``."""
    quotes: tuple[SwapQuote, ...]


@dataclass(frozen=True)
class Fixing:
    index_code: str
    fixing_date: date
    rate: float
    source: str
    published_on: date | None
    published_at: datetime | None


@dataclass(frozen=True)
class Meeting:
    meeting_date: date
    effective_date: date
    is_estimated: bool
    rate: float | None


def _market(snap: Snapshot) -> dict[str, Any]:
    if snap.kind != "market":
        raise SnapshotError(f"{snap.id}: not a market snapshot")
    return snap.data


def quote_sets(snap: Snapshot, dataset: str) -> list[QuoteSet]:
    """Every quote set of ``dataset`` (``ruonia_ois``, ``key_rate_swap``, ...) in the snapshot."""
    stale = [r for ds in snap.doc["datasets"] if ds["name"] == "curve_quote_sets" for r in ds["stale_reasons"]]
    out = []
    for qs in _market(snap)["curve_quote_sets"]:
        if qs["dataset"] != dataset:
            continue
        key = f"{qs['source']}/{dataset}:"
        out.append(QuoteSet(
            int(qs["quote_set_id"]), dataset, qs["source"], qs["quote_basis"], date.fromisoformat(qs["snapshot_date"]),
            _ts(qs["fetched_at"]), tuple(qs.get("labels") or ()), tuple(r for r in stale if r.startswith(key)),
            tuple(SwapQuote(q["tenor"], _rate(q.get("bid_pct")), _rate(q.get("mid_pct")), _rate(q.get("ask_pct")))
                  for q in sorted(qs["quotes"], key=lambda q: q["position"]))))
    return out


def fixings(snap: Snapshot, index_code: str) -> list[Fixing]:
    """Rows of one index with a value, by fixing date. Only percent rows are rates."""
    rows = []
    for f in _market(snap)["fixings"]:
        if f["index_code"] != index_code or f["value"] is None:
            continue
        if f["unit"] != "pct":
            raise SnapshotError(f"{snap.id}: {index_code} fixing {f['fixing_date']} in {f['unit']!r}, need pct")
        rows.append(Fixing(index_code, date.fromisoformat(f["fixing_date"]), _rate(f["value"]), f["source"],
                           _date(f.get("published_on")), _ts(f.get("published_at"))))
    rows.sort(key=lambda r: r.fixing_date)
    if len({r.fixing_date for r in rows}) != len(rows):
        raise SnapshotError(f"{snap.id}: duplicate {index_code} fixing dates")
    return rows


def meetings(snap: Snapshot) -> list[Meeting]:
    return sorted((Meeting(date.fromisoformat(m["meeting_date"]), date.fromisoformat(m["effective_date"]),
                           bool(m["is_estimated"]), _rate(m.get("decision_rate_pct")))
                   for m in _market(snap)["cbr_meetings"]), key=lambda m: m.effective_date)


def known_at(snap: Snapshot) -> datetime:
    return _ts(snap.doc["known_at"])


def calendar(static: Snapshot, name: str) -> tuple[BusinessCalendar, date | None]:
    """Bounded core calendar (``adapter.core_calendar``: the hub's days, extended by fixed rules to 10Y) and its
    ``provisional_from`` (days from it on are an estimate, not an official list)."""
    if static.kind != "static":
        raise SnapshotError(f"{static.id}: not a static snapshot")
    c = next((c for c in static.data["calendars"] if c["calendar"] == name), None)
    if c is None:
        raise SnapshotError(f"{static.id}: no calendar {name}")
    return core_calendar(c)
