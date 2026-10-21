"""Static data for the inbox, standard library only: the instruments index the adapter's ``ctx.instruments`` reads
(08 §7) and the business calendar of the time semantics (08 §6.5).

Both come from cached static snapshots (``<sha256>.json.gz`` bundles, plus the cache's ``index.json`` when
``desk sync`` wrote one). A bundle's identity and schema (any ``market_snapshot/1.x``, D10) are checked on read like
``desk.marketdata.bundle`` does; that
module is not imported so the conformance kit stays free of the rest of the backend.

Index format ``desk.inbox.instruments/2`` (R22): the newest static snapshot in full, every older one as its
difference from it (records set or dropped per lookup map). Consecutive days differ by a few dozen issues, so an
index covering the five weeks of the inbox lookback costs little more than one snapshot to write, ship to the
adapter's workers and load. ``Instruments`` reads format 1 (every snapshot in full) as well.

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
import re
from dataclasses import dataclass
from datetime import date, timedelta
from functools import cache
from pathlib import Path
from typing import Any

INDEX_FORMAT = "desk.inbox.instruments/2"
LOOKUP_MAPS = ("secid", "isin", "regnum")
CALENDAR = "RU_PRODUCTION"
LOOKUP_KINDS = ("isin", "regnum", "moex_secid")
_SCHEMA = re.compile(r"^market_snapshot/1\.(0|[1-9][0-9]*)$")


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
            kind, business_date, schema = _head(str(p), st.st_mtime_ns, st.st_size)
            if kind == "static" and supported_schema(schema):  # a bundle of another major is not ours to read
                out.append(StaticRef(p.name.split(".")[0], business_date, p))
    # shortcut: two static snapshots of one business date are ordered by id, not by supersession; upgrade
    # when the hub publishes static corrections.
    return sorted(out, key=lambda r: (r.business_date, r.id))


@cache
def _head(path: str, mtime_ns: int, size: int) -> tuple[Any, Any, Any]:
    """(kind, business_date, schema_version) of a bundle, read once per file version (a folder scan must not
    decompress all)."""
    with gzip.open(path) as f:
        doc = json.load(f)
    return doc.get("kind"), doc.get("business_date"), doc.get("schema_version")


def covering(refs: list[StaticRef], on: date | str | None) -> StaticRef | None:
    """The newest snapshot whose business date is <= ``on``, else the newest (None: the newest)."""
    if not refs:
        return None
    if on is None:
        return refs[-1]
    day = on if isinstance(on, str) else on.isoformat()
    older = [r for r in refs if r.business_date <= day]
    return older[-1] if older else refs[-1]


def supported_schema(version: object) -> bool:
    """``market_snapshot/1.<minor>``: the minors are additive, unknown fields are ignored (as
    ``desk.marketdata.bundle.supported_schema``; D10). Another major is not read. A bundle without a version (the
    conformance kit's synthetic ones; the hub always sends one, and ``desk sync`` verifies it) is read as before."""
    return version is None or isinstance(version, str) and _SCHEMA.match(version) is not None


def _data(ref: StaticRef) -> dict[str, Any]:
    raw = gzip.decompress(ref.path.read_bytes())
    if hashlib.sha256(raw).hexdigest() != ref.id:
        raise ValueError(f"{ref.path.name}: sha256 does not match the file name")
    doc = json.loads(raw)
    if not supported_schema(doc.get("schema_version")):
        raise ValueError(f"{ref.path.name}: schema_version {doc.get('schema_version')!r}, need market_snapshot/1.x")
    return doc["data"]


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


def index_refs(folder: str | Path, dates: list[date | str] | None = None,
               ids: list[str] | tuple[str, ...] | set[str] = ()) -> list[StaticRef]:
    """The static snapshots an index for ``dates`` holds: the newest, those covering the dates, and the cached
    snapshots of ``ids`` (an index already written that is kept), oldest first."""
    refs = static_snapshots(folder)
    chosen = {r.id: r for r in [covering(refs, None)] + [covering(refs, d) for d in dates or []] if r}
    chosen.update({r.id: r for r in refs if r.id in set(ids)})
    return sorted(chosen.values(), key=lambda r: (r.business_date, r.id))


def _delta(base: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
    """Per lookup map, what differs from the newest snapshot: the keys set to another value (or added), the keys
    dropped."""
    out = {}
    for m in LOOKUP_MAPS:
        b, x = base[m], snap[m]
        out[m] = {"set": {k: v for k, v in x.items() if k not in b or b[k] != v},
                  "drop": [k for k in b if k not in x]}
    return out


def build_index(folder: str | Path, out_path: str | Path, dates: list[date | str] | None = None,
                ids: list[str] | tuple[str, ...] | set[str] = ()) -> dict:
    """Write the instruments index for ``ctx.instruments``: the newest static snapshot plus those covering
    ``dates`` (and the cached ones of ``ids``), the newest in full and the others as their differences from it.
    Returns the written document. With no static snapshot the index is empty (every lookup None)."""
    chosen = index_refs(folder, dates, ids)
    snaps: list[dict[str, Any]] = []
    if chosen:
        base = _instruments(_data(chosen[-1]))
        snaps = [{"id": r.id, "business_date": r.business_date, "delta": _delta(base, _instruments(_data(r)))}
                 for r in chosen[:-1]]
        snaps.append({"id": chosen[-1].id, "business_date": chosen[-1].business_date, **base})
    doc = {"format": INDEX_FORMAT, "snapshots": snaps}
    tmp = Path(out_path).with_name(Path(out_path).name + ".part")  # the workers never read half a file
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out_path)
    return doc


UNMAPPED_REASONS = {
    "ambiguous": "ISIN или рег. номер у двух выпусков справочника",
    "not_in_static": "нет в справочнике хаба (неторгуемый, погашенный или новый выпуск)",
    "not_on_moex": "ISIN не российский: вероятно, выпуска нет на MOEX",
    "no_identifier": "в отчёте нет ISIN, рег. номера и кода MOEX",
}
"""Why a bond of the report has no instrument (D6), in the order the codes are checked."""


@cache
def _keys(ref: StaticRef) -> dict[str, dict[str, str | None]]:
    """moex_secid / isin / regnum -> secid of one static snapshot (None: two instruments share the value)."""
    m = _instruments(_data(ref))
    return {"moex_secid": {s: s for s in m["secid"]}, "isin": m["isin"], "regnum": m["regnum"]}


def unmapped_reason(keys: dict[str, dict[str, str | None]], row: dict[str, Any], secid: str | None = None
                    ) -> str | None:
    """None when the bond resolves as ``valuation.positions`` resolves it (its secid, else ``moex_secid``, ``isin``,
    ``regnum`` naming one instrument); else a code of ``UNMAPPED_REASONS``. "Not on MOEX" is a guess from the ISIN's
    country prefix: only the hub knows the MOEX universe beyond its snapshot."""
    if secid and secid in keys["moex_secid"]:
        return None
    shared = False
    for kind in LOOKUP_KINDS:
        v = row.get(kind)
        if isinstance(v, str) and v in keys[kind]:
            if keys[kind][v] is not None:
                return None
            shared = True
    if shared:
        return "ambiguous"
    if not any(row.get(k) for k in LOOKUP_KINDS) and not secid:
        return "no_identifier"
    isin = row.get("isin")
    if isinstance(isin, str) and isin[:2].upper() != "RU":
        return "not_on_moex"
    return "not_in_static"


def unmapped_bonds(folder: str | Path, rows: list[tuple[str | None, dict[str, Any] | None]],
                   on: date | str | None) -> dict[str, Any]:
    """The bonds of ``rows`` (secid, report row fields or None for a legacy row) without an instrument in the static
    snapshot covering ``on``: counts by reason and the ISINs, no other position data (an inbox status is shared)."""
    ref = covering(static_snapshots(folder), on)
    out: dict[str, Any] = {"static_snapshot": ref.id if ref else None,
                           "static_date": ref.business_date if ref else None, "count": 0, "by_reason": {},
                           "isins": []}
    if ref is None:
        return out
    keys, isins = _keys(ref), {}
    for secid, x in rows:
        x = x or {}
        if (x.get("asset_class") or "bond").lower() != "bond":
            continue
        reason = unmapped_reason(keys, x, secid)
        if reason is None:
            continue
        out["count"] += 1
        out["by_reason"][reason] = out["by_reason"].get(reason, 0) + 1
        if isinstance(x.get("isin"), str):
            isins.setdefault(x["isin"], reason)
    out["isins"] = [{"isin": i, "reason": r} for i, r in sorted(isins.items())]
    return out


class _Overlay:
    """A lookup map of an older snapshot in a format 2 index: its own values over the newest snapshot's."""

    __slots__ = ("_base", "_drop", "_set")

    def __init__(self, base: dict, delta: dict) -> None:
        self._base, self._set, self._drop = base, delta.get("set") or {}, frozenset(delta.get("drop") or ())

    def get(self, key: Any, default: Any = None) -> Any:
        if key in self._set:
            return self._set[key]
        if key in self._drop:
            return default
        return self._base.get(key, default)


def _snapshots(doc: dict | None) -> list[dict]:
    """The snapshots of an index document, each with its three lookup maps (format 2 deltas as overlays)."""
    snaps = sorted((doc or {}).get("snapshots", []), key=lambda s: (s["business_date"], s["id"]))
    full = next((x for x in reversed(snaps) if "delta" not in x), None)
    return [x if "delta" not in x else {"id": x["id"], "business_date": x["business_date"],
                                        **{m: _Overlay(full[m] if full else {}, x["delta"].get(m) or {})
                                           for m in LOOKUP_MAPS}} for x in snaps]


class Instruments:
    """``ctx.instruments`` over an index document; ``Instruments(None)`` is the stub (every lookup None)."""

    def __init__(self, doc: dict | None) -> None:
        self._snaps = _snapshots(doc)
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
