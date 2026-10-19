"""Static data for the inbox, standard library only: the instruments index the adapter's ``ctx.instruments`` reads
(08 §7) and the business calendar of the time semantics (08 §6.5).

Both come from cached static snapshots (``<sha256>.json.gz`` bundles, plus the cache's ``index.json`` when
``desk sync`` wrote one). A bundle's identity is checked on read like ``desk.marketdata.bundle`` does; that
module is not imported so the conformance kit stays free of the rest of the backend.

Index record per instrument, from the static ``instruments`` rows: ``instrument_id`` and ``moex_secid`` are the
``secid`` (the backend's instrument key), ``isin``, ``regnum`` (``regnumber``), ``name``, ``currency``
(``currency_id``), ``kind`` (``bond_class``: ofz_pd, corporate, ...) and ``initial_face``
(``initial_face_value``, a decimal string). An isin or regnum shared by two secids resolves to None: the
backend never guesses an instrument.

Calendar: the ``RU_PRODUCTION`` row (07 §0.1: Russian holidays incl. transfers, transferred working Saturdays
as working days; MOEX weekend sessions are not business days, which the MOEX_* rows would count), only within
its ``valid_from``..``valid_to``: past that the date is unknown, never assumed Mon-Fri.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from functools import cache
from pathlib import Path
from typing import Any

INDEX_FORMAT = "desk.inbox.instruments/1"
CALENDAR = "RU_PRODUCTION"
LOOKUP_KINDS = ("isin", "regnum", "moex_secid")


class CalendarUnavailable(LookupError):
    """No cached static snapshot covers the date: the file waits (``waiting_calendar``, 08 §6.5)."""


@dataclass(frozen=True)
class StaticRef:
    id: str
    business_date: str
    path: Path


def static_snapshots(folder: str | Path) -> list[StaticRef]:
    """Static snapshots in a cache folder, oldest first (by business date, then id)."""
    folder = Path(folder)
    out: list[StaticRef] = []
    idx = folder / "index.json"
    if idx.is_file():
        for e in json.loads(idx.read_text(encoding="utf-8")):
            p = folder / f"{e['id']}.json.gz"
            if e.get("kind") == "static" and p.is_file():
                out.append(StaticRef(e["id"], e["business_date"], p))
    else:
        for p in folder.glob("*.json.gz"):
            st = p.stat()
            kind, business_date = _head(str(p), st.st_mtime_ns, st.st_size)
            if kind == "static":
                out.append(StaticRef(p.name.split(".")[0], business_date, p))
    # shortcut: two static snapshots of one business date are ordered by id, not by supersession; upgrade
    # when the hub publishes static corrections.
    return sorted(out, key=lambda r: (r.business_date, r.id))


@cache
def _head(path: str, mtime_ns: int, size: int) -> tuple[Any, Any]:
    """(kind, business_date) of a bundle, read once per file version (a folder scan must not decompress all)."""
    with gzip.open(path) as f:
        doc = json.load(f)
    return doc.get("kind"), doc.get("business_date")


def covering(refs: list[StaticRef], on: date | str | None) -> StaticRef | None:
    """The newest snapshot whose business date is <= ``on``, else the newest (None: the newest)."""
    if not refs:
        return None
    if on is None:
        return refs[-1]
    day = on if isinstance(on, str) else on.isoformat()
    older = [r for r in refs if r.business_date <= day]
    return older[-1] if older else refs[-1]


def _data(ref: StaticRef) -> dict[str, Any]:
    raw = gzip.decompress(ref.path.read_bytes())
    if hashlib.sha256(raw).hexdigest() != ref.id:
        raise ValueError(f"{ref.path.name}: sha256 does not match the file name")
    return json.loads(raw)["data"]


def _instruments(data: dict[str, Any]) -> dict[str, Any]:
    by_secid: dict[str, dict] = {}
    by: dict[str, dict[str, str | None]] = {"isin": {}, "regnum": {}}
    for i in data["instruments"]:
        rec = {"instrument_id": i["secid"], "isin": i.get("isin"), "regnum": i.get("regnumber"),
               "moex_secid": i["secid"], "name": i.get("name"), "currency": i.get("currency_id"),
               "kind": i.get("bond_class"), "initial_face": i.get("initial_face_value")}
        by_secid[i["secid"]] = rec
        for kind in ("isin", "regnum"):
            if rec[kind]:
                seen = by[kind].get(rec[kind], "")
                by[kind][rec[kind]] = i["secid"] if seen == "" else None  # None: ambiguous
    return {"secid": by_secid, **by}


def build_index(folder: str | Path, out_path: str | Path, dates: list[date | str] | None = None) -> dict:
    """Write the instruments index for ``ctx.instruments``: the newest static snapshot plus those covering
    ``dates``. Returns the written document. With no static snapshot the index is empty (every lookup None)."""
    refs = static_snapshots(folder)
    chosen = {r.id: r for r in [covering(refs, None)] + [covering(refs, d) for d in dates or []] if r}
    doc = {"format": INDEX_FORMAT, "snapshots": [
        {"id": r.id, "business_date": r.business_date, **_instruments(_data(r))}
        for r in sorted(chosen.values(), key=lambda r: (r.business_date, r.id))]}
    Path(out_path).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return doc


class Instruments:
    """``ctx.instruments`` over an index document; ``Instruments(None)`` is the stub (every lookup None)."""

    def __init__(self, doc: dict | None) -> None:
        self._snaps = sorted((doc or {}).get("snapshots", []), key=lambda s: (s["business_date"], s["id"]))
        self.snapshot_id: str | None = self._snaps[-1]["id"] if self._snaps else None

    @classmethod
    def from_file(cls, path: str | Path | None) -> Instruments:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")) if path else None)

    def lookup(self, kind: str, value: str, on: date | str | None = None) -> dict | None:
        if kind not in LOOKUP_KINDS:
            raise ValueError(f"lookup kind must be one of {LOOKUP_KINDS}")
        if not self._snaps or not isinstance(value, str):
            return None
        day = None if on is None else on if isinstance(on, str) else on.isoformat()
        older = [s for s in self._snaps if day is None or s["business_date"] <= day]
        snap = older[-1] if older else self._snaps[-1]
        secid = value if kind == "moex_secid" else snap[kind].get(value)
        rec = snap["secid"].get(secid) if secid else None
        return dict(rec) if rec else None


@dataclass(frozen=True)
class RuCalendar:
    """Russian business days of one static snapshot's calendar row, valid only inside its range."""

    snapshot_id: str
    version: str
    valid_from: date
    valid_to: date
    holidays: frozenset[date]
    working_days: frozenset[date]

    def is_business_day(self, d: date) -> bool:
        if not self.valid_from <= d <= self.valid_to:
            raise CalendarUnavailable(f"{d} is outside the calendar {self.version} ({self.valid_from}..{self.valid_to})")
        return d in self.working_days or (d.weekday() < 5 and d not in self.holidays)

    def prev_business_day(self, d: date) -> date:
        d -= timedelta(days=1)
        while not self.is_business_day(d):
            d -= timedelta(days=1)
        return d


def _calendar(ref: StaticRef, row: dict) -> RuCalendar:
    return RuCalendar(
        snapshot_id=ref.id, version=f"{row['calendar']}@{row['fingerprint'][:12]}",
        valid_from=date.fromisoformat(row["valid_from"]), valid_to=date.fromisoformat(row["valid_to"]),
        holidays=frozenset(date.fromisoformat(x) for x in row["holidays"]),
        working_days=frozenset(date.fromisoformat(x) for x in row["working_weekends"]))


@cache
def _calendar_of(ref: StaticRef) -> RuCalendar | None:
    """The snapshot's RU_PRODUCTION calendar; a ref is content-addressed, so it is read once per process."""
    row = next((c for c in _data(ref)["calendars"] if c["calendar"] == CALENDAR), None)
    return _calendar(ref, row) if row else None


def business_calendar(folder: str | Path, on: date) -> RuCalendar:
    """The calendar of the newest cached static snapshot whose range covers ``on`` and the business day before
    it (08 §6.5: ``start_of_day`` needs that one), so a report early in January takes an older snapshot whose
    range still reaches back into December."""
    for ref in reversed(static_snapshots(folder)):
        cal = _calendar_of(ref)
        if cal is None or not cal.valid_from <= on <= cal.valid_to:
            continue
        try:
            cal.prev_business_day(on)
        except CalendarUnavailable:
            continue
        return cal
    raise CalendarUnavailable(f"no cached static snapshot has a {CALENDAR} calendar covering {on} and the day before")
